"""Bounded bridges for blocking SDKs and ordered in-process state updates."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, suppress
from contextvars import copy_context
from functools import partial, wraps
from weakref import WeakKeyDictionary


IO_WORKERS = 8
_executor = ThreadPoolExecutor(max_workers=IO_WORKERS, thread_name_prefix="risk-io")
_loop_limits = WeakKeyDictionary()
_loop_locks = WeakKeyDictionary()


async def run_blocking(function, *args, **kwargs):
    """Offload one SDK operation with backpressure before executor submission.

    Cancellation waits for an already running write to finish. Otherwise an
    enclosing conversation lock could be released while that write is still
    running, and a later request would read or overwrite incomplete state.
    """
    loop = asyncio.get_running_loop()
    limiter = _loop_limits.setdefault(loop, asyncio.Semaphore(IO_WORKERS))
    async with limiter:
        operation = partial(function, *args, **kwargs)
        future = loop.run_in_executor(_executor, copy_context().run, operation)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            # Retrieve any worker exception, but preserve cancellation semantics.
            with suppress(Exception):
                future.result()
            raise


@asynccontextmanager
async def keyed_lock(namespace, key):
    """FIFO lock per key; remove idle entries so conversations do not leak."""
    loop = asyncio.get_running_loop()
    locks = _loop_locks.setdefault(loop, {})
    identity = (namespace, key)
    entry = locks.setdefault(identity, [asyncio.Lock(), 0])
    entry[1] += 1
    try:
        async with entry[0]:
            yield
    finally:
        entry[1] -= 1
        if not entry[1]:
            locks.pop(identity, None)


def serialized(namespace, key):
    """Serialize a logical read/modify/write without blocking other keys."""
    def decorate(function):
        @wraps(function)
        async def wrapped(*args, **kwargs):
            async with keyed_lock(namespace, key(*args, **kwargs)):
                return await function(*args, **kwargs)
        return wrapped
    return decorate
