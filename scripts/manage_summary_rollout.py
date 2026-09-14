"""Local operator control. Dry-run by default; approval never comes from the LLM."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('action',choices=['status','approve-benchmark','pause','queue-existing','run-once'])
parser.add_argument('--report',type=Path)
parser.add_argument('--apply',action='store_true')
parser.add_argument('--limit',type=int,default=20)
parser.add_argument('--config-root',type=Path,default=Path(__file__).resolve().parents[1])
args=parser.parse_args()
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root/'social'))
from dotenv import load_dotenv
load_dotenv(args.config_root/'social/.env',override=False)
load_dotenv(args.config_root/'.env',override=False)
from database import db
from services import conversation_summary_operations as ops
from services import conversation_compaction_service as c

if args.action=='status':
    print(json.dumps(ops.rollout_status(),ensure_ascii=False,indent=2))
elif args.action=='approve-benchmark':
    if args.report is None:
        parser.error('--report is required')
    raw=args.report.read_bytes()
    report=json.loads(raw)
    ops.approve_benchmark(report,hashlib.sha256(raw).hexdigest(),apply=args.apply)
    print(json.dumps({'applied':args.apply,'action':args.action,'policy':c.COMPACTION_POLICY_VERSION,
                     'report_sha256':hashlib.sha256(raw).hexdigest(),'reported_qualified':report.get('qualified')}))
elif args.action=='pause':
    if args.apply:
        ops.pause_rollout()
    print(json.dumps({'applied':args.apply,'action':'pause'}))
elif args.action=='run-once':
    print(json.dumps(ops.run_rebuild_once() if args.apply else {'applied':False}))
else:
    limit=max(1,min(args.limit,500))
    owner_rows=list(db.profiles.find({'user_id':{'$exists':True}}, {'user_id':1}).limit(501))
    owners={str(row['user_id']) for row in owner_rows[:500]}
    rooms={(owner,c._public_room_id(owner)) for owner in owners}
    room_rows=list(db.ai_rooms.find({'user_id':{'$in':list(owners)},'room_kind':'conversation'},
                                    {'user_id':1,'room_id':1}).limit(1001))
    rooms.update((row['user_id'],row['room_id']) for row in room_rows[:1000])
    counts={}
    for owner,room in sorted(rooms)[:limit]:
        try:
            result=(ops.enqueue_rebuild(owner,room) if args.apply else ops.summary_status(owner,room))
            state=result.get('status') or result.get('state')
        except Exception:
            state='unavailable'
        counts[state]=counts.get(state,0)+1
    print(json.dumps({'applied':args.apply,'scanned':min(limit,len(rooms)),
                     'truncated':len(rooms)>limit or len(owner_rows)>500 or len(room_rows)>1000,'status_counts':counts}))
