"""Synthetic-only end-to-end writer benchmark, including saturated summaries."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import types

from validate_summary_rollout import corpus

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--live',action='store_true')
parser.add_argument('--output',type=Path,required=True)
parser.add_argument('--config-root',type=Path,default=Path(__file__).resolve().parents[1])
args=parser.parse_args()
cases=corpus()
for i in range(5):
    facts=[f'Fictional traveller name is Nori{i}.',f'Traveller lives in Eastfield{i}.',
           f'Traveller was born in Northport{i}.',f'Traveller reads Moonbook{i}.',
           f'Traveller carries Copperkey{i}.',f'Traveller drinks Greentea{i}.']
    tokens=[f'Nori{i}',f'Eastfield{i}',f'Northport{i}',f'Moonbook{i}',f'Copperkey{i}',f'Greentea{i}']
    prior={'known_continuity':facts,'unresolved_questions':['Which game does the traveller play?',
        'Which character does the traveller use?','What is the highest rank?',
        'What is the current rank?','Where will the traveller go tomorrow?']}
    for family,rows,required,forbidden in [
        ('dense_answers',[('owner','This is fiction: the game is Orbitstrike, character is Reed, highest rank Diamond, current rank Platinum. Tomorrow is undecided.')],tokens+['Orbitstrike','Reed','Diamond','Platinum'],[]),
        ('dense_correction',[('owner',f'This is fiction: correction, the traveller moved to Westfield{i}; Eastfield{i} is the old residence, no longer current.')],[t for t in tokens if not t.startswith('Eastfield')]+[f'Westfield{i}'],[]),
        ('dense_topic_switch',[('owner',f'This is fiction: new topic, paper planes called Kite{i}. Keep prior traveller information and unanswered questions.')],tokens+[f'Kite{i}'],[]),
        ('dense_excluded',[('owner','This is fiction: the traveller plays Orbitstrike and uses Reed.'),('owner','SECRET_MARKER do not remember','no_memory')],tokens+['Orbitstrike','Reed'],['SECRET_MARKER']),
    ]:
        cases.append({'name':f'{family}_{i}','family':family,'prior':prior,'rows':rows,'required':required,'forbidden':forbidden})
if not args.live:
    print(json.dumps({'cases':len(cases),'families':len({c['family'] for c in cases}),'live':False}));raise SystemExit(0)

from dotenv import load_dotenv
load_dotenv(args.config_root/'social/.env',override=False);load_dotenv(args.config_root/'.env',override=False)
os.environ['AYUE_SKIP_DOTENV']='1';os.environ['MONGO_URI']='mongodb://127.0.0.1:27017';os.environ['AYUE_LLM_PROVIDER']='ollama'
os.environ['AYUE_CONVERSATION_COMPACTION_MODE']='shadow'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'social'))
import mongomock
database=types.ModuleType('database');database.db=mongomock.MongoClient().synthetic_review_benchmark
for name in ['profiles','matches','messages','semantic_plans','calendar_events','ai_rooms','notification_threads','notification_presence','google_calendar_connections','google_calendar_oauth_states']:
    setattr(database,name+'_coll',database.db[name])
sys.modules['database']=database
from bson import ObjectId
from services import conversation_compaction_service as c
from services.conversation_summary_operations import algorithm_fingerprint
from services.ai_service import _resolve_chat_model
from services.message_use_service import metadata_for_use
report={'version':'summary-benchmark-v1','policy':c.COMPACTION_POLICY_VERSION,'model':_resolve_chat_model(),
    'provider':'ollama','algorithm_sha256':algorithm_fingerprint(),'synthetic':True,'production_writes':False,
    'started_at':time.time(),'cases':[],'corpus_sha256':hashlib.sha256(json.dumps(cases,sort_keys=True).encode()).hexdigest(),
    'scope':'Actual writer pipeline; independently checked anchors plus original evaluator. Synthetic prior summaries only.'}

def evaluate(case):
    owner='bench_'+case['name'];room=c._public_room_id(owner);now=time.time()
    baseline=None
    if case['prior']:
        seed={'_id':ObjectId(),'room_id':room,'sender_id':owner,'content':'synthetic prior checkpoint','timestamp':0.}
        database.db.messages.insert_one(seed)
        c._store_compaction_record(user_id=owner,room_id=room,current=None,baseline=None,messages=[seed],
            source_hash=hashlib.sha256(owner.encode()).hexdigest(),summary=c.ConversationSummaryV1.model_validate(case['prior']),
            evaluation=c._excluded_only_evaluation(),observability=c.ConversationCompactionObservabilityV1(
                policy_version=c.COMPACTION_POLICY_VERSION,input_message_count=1,input_char_count=0,summary_item_count=0,
                summary_char_count=0,generation_latency_ms=0,evaluation_latency_ms=0,profile_coverage_status='fixture',
                profile_requeued_count=0,generation_result_code='excluded_only'),current_revision=0,prior_source_hash=None)
        baseline=c._load_current_compaction(owner,room)
    messages=[{'_id':ObjectId(),'room_id':room,'sender_id':owner if row[0]=='owner' else 'ai_assistant',
        'content':row[1],'timestamp':float(i+1),'metadata':{'message_use':metadata_for_use(row[2] if len(row)>2 else 'ordinary')}} for i,row in enumerate(case['rows'])]
    database.db.messages.insert_many(messages)
    outcome=c.run_conversation_compaction_shadow(owner,room,[str(m['_id']) for m in messages],
        baseline['revision'] if baseline else 0,baseline['source_hash'] if baseline else None)
    digest=c._source_hash(baseline['source_hash'] if baseline else None,messages)
    run=c.CONVERSATION_COMPACTION_RUNS.find_one({'source_hash':digest}) or {}
    saved=c._load_current_compaction(owner,room) if outcome['status']=='stored' else None
    payload=(saved or {}).get('summary')
    text=json.dumps(payload or {},ensure_ascii=False).casefold()
    missing=[x for x in case['required'] if x.casefold() not in text]
    leaked=[x for x in case['forbidden'] if x.casefold() in text]
    # An explicitly answered character question must not remain unresolved.
    if case['family']=='dense_answers' and any('character' in q.casefold() for q in (payload or {}).get('unresolved_questions',[])):
        missing.append('resolved_character_question_removed')
    evaluation=run.get('evaluation');content_pass=bool(saved and not missing and not leaked)
    return {'name':case['name'],'family':case['family'],'outcome':outcome['status'],'summary':payload,
        'evaluation':evaluation,'observability':run.get('observability'),'missing':missing,'leaked':leaked,
        'content_pass':content_pass,'passed':bool(content_pass and evaluation and evaluation['status']=='pass'),
        'seconds':round(time.time()-now,2)}

with ThreadPoolExecutor(max_workers=2) as pool:
    for future in as_completed([pool.submit(evaluate,case) for case in cases]):
        row=future.result();report['cases'].append(row)
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps({'completed':len(report['cases']),'name':row['name'],'passed':row['passed'],'outcome':row['outcome']}),flush=True)
report['finished_at']=time.time();report['case_count']=len(cases)
report['pass_count']=sum(r['passed'] for r in report['cases'])
report['critical_failures']=[r['name'] for r in report['cases'] if r['leaked'] or (r.get('evaluation') or {}).get('status')=='pass' and not r['content_pass']]
report['unavailable_count']=sum(not r.get('evaluation') or r['evaluation']['status']=='unavailable' for r in report['cases'])
report['review_count']=sum((r.get('evaluation') or {}).get('status')=='review' for r in report['cases'])
report['qualified']=bool(report['pass_count']/len(cases)>=.95 and report['unavailable_count']/len(cases)<=.02 and report['review_count']/len(cases)<=.05 and not report['critical_failures'])
args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2))
print(json.dumps({k:v for k,v in report.items() if k!='cases'}),flush=True)
raise SystemExit(0 if report['qualified'] else 1)
