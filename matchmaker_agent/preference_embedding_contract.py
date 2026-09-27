"""Approved metadata only; no key-pool/provider initialization at import."""
import hashlib
import json
import os
from pathlib import Path
from .preference_bootstrap_contract import require
from .related_interest_contract import embedding_fingerprint

PROVENANCE = '53a1d22fd63bcde9be0fa632df532929a05dcb5aa60ded6879b47ad0c2c1c8af'
RUNTIME = '89cc12c59af783aa2953d856c15ba34625e2db129cf5d8bf5712e53af017b640'
MODEL = 'models/gemini-embedding-2'


def enabled():
    return os.getenv('PREFERENCE_EMBEDDING_V2_ENABLED', 'off').strip().lower() == 'on'


def frozen_contract():
    value = json.loads(Path(__file__).with_name('embedding_v2_frozen.json').read_text())
    digest = hashlib.sha256(json.dumps(value['provenance'], sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    require(digest == value['provenance_fingerprint'] == PROVENANCE, 'embedding_provenance_mismatch', 503)
    require(value['runtime_compatibility_fingerprint'] == embedding_fingerprint(MODEL) == RUNTIME,
            'embedding_fingerprint_mismatch', 503)
    require(os.getenv('GOOGLE_EMBEDDING_MODEL', MODEL) == MODEL, 'embedding_model_mismatch', 503)
    return value
