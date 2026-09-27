"""Automatic incremental v2 worker. No user-facing write waits for a provider."""
import logging
import threading
import requests

from matchmaker_agent.preference_bootstrap_contract import internal_headers
from matchmaker_agent.preference_embedding_contract import enabled, frozen_contract, MODEL, RUNTIME, PROVENANCE
from matchmaker_agent.concept_identity import stored_concept_identity
from scripts.prepare_related_interest_embeddings import prepare_batch
from scripts.semantic_embedding_pipeline import SafeGeminiEmbedding, provenance, validate_frozen

PREFIX='/api/v2/preferences/embedding-jobs/'
_stop=threading.Event()
_thread=None
_lock=threading.Lock()
LOG=logging.getLogger(__name__)


def graph_call(action, body):
    path=PREFIX+action
    response=requests.post('http://127.0.0.1:9001'+path,json=body,
        headers=internal_headers(path,body),timeout=(3,15),allow_redirects=False)
    response.raise_for_status()
    return response.json()


def process_one(*, call=graph_call, provider_factory=SafeGeminiEmbedding):
    if not enabled():return {'status':'disabled'}
    if not _lock.acquire(blocking=False):return {'status':'busy'}
    job=provider=None
    try:
        frozen=frozen_contract()
        validate_frozen(frozen,provenance(MODEL,{'name':MODEL,'provider_reported_version':'2','supported_methods':['embedContent']},prepare_batch))
        job=call('claim',{}).get('job')
        if job is None:return {'status':'idle'}
        identity=stored_concept_identity(job.get('identity',{}))
        if (not identity or identity.key!=job.get('key') or identity.semantic_input_hash!=job.get('source_hash')
                or job.get('fingerprint')!=RUNTIME or job.get('provenance_fingerprint')!=PROVENANCE):
            raise ValueError('unverified_embedding_job')
        if _stop.is_set():raise RuntimeError('worker_stopping')
        # Existing approved adapter/prepare_batch, same SDK/source/provenance.
        # Credentials are loaded by Social normally and stay in process memory.
        from config import GOOGLE_API_KEYS
        provider=provider_factory(MODEL,frozen,prepare_batch,keys=GOOGLE_API_KEYS[:6])
        batch=prepare_batch([identity.as_dict()],provider.embed,MODEL)
        if len(batch)!=1 or batch[0]['source_hash']!=job['source_hash']:
            raise ValueError('embedding_batch_invalid')
        if _stop.is_set():raise RuntimeError('worker_stopping')
        receipt=call('finish',{'key':job['key'],'lease':job['lease'],'source_hash':job['source_hash'],'vector':batch[0]['vector']})
        return {'status':'complete' if not receipt.get('cancelled') else 'cancelled',
            'written':receipt.get('written',0),'reused':receipt.get('reused',0)}
    except Exception:
        if isinstance(job,dict) and isinstance(job.get('key'),str) and isinstance(job.get('lease'),str):
            try:call('fail',{'key':job['key'],'lease':job['lease']})
            except Exception:pass  # Lease expiry safely recovers an unknown HTTP result.
        return {'status':'unavailable','error_code':'preference_embedding_unavailable'}
    finally:
        try:
            if provider:provider.close()
        finally:
            _lock.release()


def _worker():
    while not _stop.wait(30):
        result=process_one()
        if result.get('status')=='unavailable':LOG.warning('preference_embedding_unavailable')


def start_worker():
    global _thread
    if not enabled() or _thread and _thread.is_alive():return
    _stop.clear()
    _thread=threading.Thread(target=_worker,name='preference-embedding-v2',daemon=True)
    _thread.start()


def stop_worker():
    _stop.set()
    if _thread:_thread.join(timeout=2)
