"""Global voice assistant for the Folks Flutter app.

Keep package imports lightweight so catalog and schema tooling can run without
booting the live runtime or opening the task database.  The Social service still
gets the same public exports on first access.
"""

from importlib import import_module
from typing import Any


__all__ = ["router", "start_app_voice_task_services", "stop_app_voice_task_services"]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    router_module = import_module(".router", __name__)
    exports = {export: getattr(router_module, export) for export in __all__}
    globals().update(exports)
    return exports[name]
