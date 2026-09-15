"""Dry-run-first recovery of failed jobs with confirmed system-event ID inputs.

Run only after deploying/restarting the matching code through start_all.sh and
approving its benchmark. Never promotes review results or changes source data.
"""
import argparse
import json
from pathlib import Path
import sys

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--apply', action='store_true')
parser.add_argument('--config-root', type=Path, default=Path(__file__).resolve().parents[1])
args = parser.parse_args()
root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / 'social'))
from dotenv import load_dotenv
load_dotenv(args.config_root / 'social/.env', override=False)
load_dotenv(args.config_root / '.env', override=False)
from services import conversation_compaction_service as c
from services import conversation_summary_operations as ops

if args.apply and (ops.rollout_decision() is not True or c.conversation_compaction_mode() != 'shadow'):
    raise SystemExit('matching_rollout_approval_required')

rows = list(ops.JOBS.find({'policy': c.COMPACTION_POLICY_VERSION,
                         'state': 'failed', 'result_code': 'source_unavailable'}).limit(501))
if len(rows) > 500:
    raise SystemExit('recovery_inventory_exceeds_limit')
results = []
for job in rows:
    try:
        selected = c._select_compaction_batch(job['owner'], job['room'])
        ids = selected.get('message_ids') or []
        if selected['status'] != 'ready' or not any(x.startswith('system-event:') for x in ids):
            results.append({'status': 'not_confirmed', 'selection': selected['status']})
            continue
        loaded = c._load_exact_batch(job['owner'], job['room'], ids)
        if len(loaded) != len(ids):
            results.append({'status': 'source_changed'})
            continue
        result = ops.enqueue_rebuild(job['owner'], job['room'], retry=True) if args.apply else {'status': 'verified_dry_run'}
        results.append({'status': result['status'], 'batch_count': len(loaded)})
    except Exception as exc:
        results.append({'status': 'verification_failed', 'error_type': type(exc).__name__})
print(json.dumps({'applied': args.apply, 'candidate_jobs': len(rows), 'results': results}, indent=2))
