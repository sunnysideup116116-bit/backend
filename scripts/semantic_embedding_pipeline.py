"""Secret-safe adapter for the frozen, unmodified Gemini embedding function.

Only explicit process-environment keys are accepted. No dotenv, legacy key-pool
logger, generated-vector file, inferred historical provenance or SDK retry.
"""
import ast
import hashlib
import inspect
import json
import os
import time
from contextlib import redirect_stdout, redirect_stderr
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

from matchmaker_agent.concept_identity import FRESH_PREFERENCE_NORMALIZATION_POLICY
from matchmaker_agent.related_interest_contract import embedding_manifest, embedding_fingerprint, unit_vector

ROOT = Path(__file__).resolve().parents[1]


class EmbeddingContractError(ValueError):
    pass


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def source_function(path, name):
    text = path.read_text()
    node = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == name)
    return ast.get_source_segment(text, node), node


def provenance(model, metadata, prepare_batch, *, package_version=version):
    get_source, _ = source_function(ROOT / 'social/services/ai_service.py', 'get_embeddings')
    normalize_source, _ = source_function(ROOT / 'matchmaker_agent/concept_identity.py', 'normalize_preference_text')
    runtime = embedding_manifest(model)
    if model != 'models/gemini-embedding-2' or metadata['name'] != model or 'embedContent' not in metadata['supported_methods']:
        raise ValueError('embedding_model_contract_changed')
    return {'pipeline_version': 'concept-embedding-v2-canary-full-fidelity-v1',
        'provider': 'Google Gemini Developer API', 'request_model': model, 'resolved_model': metadata['name'],
        'provider_reported_model_version': metadata['provider_reported_version'], 'immutable_model_revision': 'unreported',
        'sdk': 'google-generativeai', 'sdk_version': package_version('google-generativeai'),
        'wire_client': 'google-ai-generativelanguage REST', 'wire_client_version': package_version('google-ai-generativelanguage'),
        'logical_task': 'semantic_similarity', 'api_task_type': 'omitted for embedding-2; encoded by the exact prefix',
        'dimension': 768, 'input_prefix': runtime['prefix'], 'input_template': 'prefix + full semantic_text',
        'text_normalization': 'existing normalize_preference_text: NFKC, collapse whitespace, strip; output must equal persisted semantic_text; no truncation or fresh OpenCC conversion',
        'identity_source_version': 'v2', 'identity_source_normalization_policy': FRESH_PREFERENCE_NORMALIZATION_POLICY,
        'vector_normalization': 'L2 via unchanged runtime unit_vector, Python float64; finite/nonzero/768 checked',
        'runtime_manifest': runtime, 'runtime_compatibility_fingerprint': embedding_fingerprint(model),
        'source_functions_sha256': {'get_embeddings': sha(get_source), 'normalize_preference_text': sha(normalize_source),
            'unit_vector': sha(inspect.getsource(unit_vector)), 'prepare_batch': sha(inspect.getsource(prepare_batch))}}


def validate_frozen(frozen, current):
    digest = sha(canonical_json(current))
    if (frozen.get('provenance') != current or frozen.get('provenance_fingerprint') != digest
            or frozen.get('runtime_compatibility_fingerprint') != current['runtime_compatibility_fingerprint']):
        raise ValueError('frozen_embedding_pipeline_drift')
    return digest


class SafeGeminiEmbedding:
    def __init__(self, model, frozen, prepare_batch, *, keys=None):
        validate_frozen(frozen, provenance(model, {'name': model,
            'provider_reported_version': frozen['provenance']['provider_reported_model_version'],
            'supported_methods': ['embedContent']}, prepare_batch))
        self.keys = list(dict.fromkeys(k for k in (keys if keys is not None else
            [os.getenv(f'GOOGLE_API_KEYS{i}', '').strip() for i in range(1, 7)]) if k))
        if not self.keys or len(self.keys) > 6:
            raise ValueError('embedding_provider_keys_unavailable')
        self.model, self.active_key = model, None
        self.failures = dict.fromkeys(('auth_failure', 'quota_exhausted', 'provider_failure'), 0)
        self.attempts = self.retries = 0
        import google.generativeai as genai
        from google.ai import generativelanguage as glm
        from google.api_core.client_options import ClientOptions
        self.genai, self.glm, self.options = genai, glm, ClientOptions
        def metadata(key):
            client = glm.ModelServiceClient(transport='rest', client_options=ClientOptions(api_key=key))
            try:
                record = client.get_model(name=model, timeout=10, retry=None)
                return {'name': record.name, 'provider_reported_version': record.version or 'unreported',
                    'supported_methods': list(record.supported_generation_methods)}
            finally:
                client.transport.close()
        self.manifest = provenance(model, self.execute(metadata), prepare_batch)
        self.provenance_fingerprint = validate_frozen(frozen, self.manifest)
        _, node = source_function(ROOT / 'social/services/ai_service.py', 'get_embeddings')
        from fastapi import HTTPException
        scope = {'time': time, 'GOOGLE_EMBEDDING_MODEL': model,
            'genai': SimpleNamespace(embed_content=self.embed_content),
            'google_key_pool': SimpleNamespace(execute=self.execute), 'HTTPException': HTTPException}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
            str(ROOT / 'social/services/ai_service.py'), 'exec'), scope)
        self.get_embeddings = scope['get_embeddings']
        self.expected_inputs = None

    def execute(self, operation):
        for index, key in enumerate(self.keys):
            self.attempts += 1; self.retries += int(index > 0); self.active_key = key
            try:
                with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
                    return operation(key)
            except EmbeddingContractError:
                raise
            except Exception as exc:
                code = getattr(exc, 'code', None)
                if callable(code): code = code()
                try: code = int(code)
                except (TypeError, ValueError): code = 0
                category = 'auth_failure' if code in (401, 403) else 'quota_exhausted' if code == 429 else 'provider_failure'
                self.failures[category] += 1
                if code in (400, 404, 422):
                    raise ValueError('embedding_provider_contract_rejected') from None
            finally:
                self.active_key = None
        raise ValueError('embedding_provider_pool_unavailable')

    def embed_content(self, **request):
        if (self.active_key is None or request['model'] != self.model or request['content'] != self.expected_inputs
                or request.get('output_dimensionality') != 768 or 'task_type' in request
                or request.get('request_options', {}).get('retry', 'unset') is not None
                or not 0 < request['request_options']['timeout'] <= 10):
            raise EmbeddingContractError('embedding_request_contract_changed')
        client = self.glm.GenerativeServiceClient(transport='rest', client_options=self.options(api_key=self.active_key))
        try:
            return self.genai.embed_content(client=client, **request)
        finally:
            client.transport.close()

    def embed(self, texts, **kwargs):
        self.expected_inputs = [embedding_manifest(self.model)['prefix'] + text for text in texts]
        return self.get_embeddings(texts, **kwargs)

    def close(self):
        self.keys.clear(); self.active_key = None; self.expected_inputs = None
