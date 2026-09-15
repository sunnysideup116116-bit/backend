"""Owner-scoped, dry-run-first recovery after deploying an approved repair strategy.

Applying queues real private conversation for the configured model. Obtain the
owner's consent and deploy/restart through start_all.sh before using --apply.
This never changes review to pass or resets other owners' jobs.
"""
import argparse
import json
from pathlib import Path
import sys

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--owner',required=True)
parser.add_argument('--apply',action='store_true')
parser.add_argument('--config-root',type=Path,default=Path(__file__).resolve().parents[1])
args=parser.parse_args()
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'social'))
from dotenv import load_dotenv
load_dotenv(args.config_root/'social/.env',override=False);load_dotenv(args.config_root/'.env',override=False)
from services import conversation_compaction_service as c
from services import conversation_summary_operations as ops

if args.apply and (not ops.rollout_decision() or c.conversation_compaction_mode()!='shadow'):
    raise SystemExit('matching_approved_repair_required')
rows=list(ops.JOBS.find({'owner':args.owner,'policy':c.COMPACTION_POLICY_VERSION,'state':'failed','result_code':'review'}).limit(101))
if len(rows)>100:raise SystemExit('owner_job_limit_exceeded')
results=[]
for row in rows:
    selected=c._select_compaction_batch(args.owner,row['room'])
    if selected['status']!='ready':
        results.append({'status':selected['status'],'applied':False});continue
    loaded=c._load_exact_batch(args.owner,row['room'],selected['message_ids'])
    if len(loaded)!=len(selected['message_ids']):
        results.append({'status':'source_changed','applied':False});continue
    result=ops.enqueue_rebuild(args.owner,row['room'],retry=True) if args.apply else {'status':'verified_dry_run'}
    results.append({'status':result['status'],'applied':args.apply,'messages':len(loaded)})
print(json.dumps({'candidate_jobs':len(rows),'results':results},indent=2))
