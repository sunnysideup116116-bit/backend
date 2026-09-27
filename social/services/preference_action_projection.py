"""Recover only Mongo fact state from current Graph after an owner action.

Never replay a Graph mutation or manufacture a new fact/evidence. An unknown
HTTP outcome remains unknown to the caller; this queue only repairs projections.
"""
import threading
import time
import uuid
from pymongo import ReturnDocument
from database import db, profiles_coll
from services.preference_projection_fence import projection_write
from services.preference_bootstrap_service import graph_call

JOBS=db['preference_action_projection_jobs']
FACTS=db['preference_facts']
_stop=threading.Event()
_thread=None


def stage(owner, key):
    ident=uuid.uuid4().hex;now=time.time()
    JOBS.insert_one({'_id':ident,'owner':owner,'key':key,'state':'waiting_graph',
        'due_at':now+75,'created_at':now,'attempts':0})
    return ident


def settle(ident):
    try:
        JOBS.update_one({'_id':ident,'state':'waiting_graph'},{'$set':{'state':'pending','due_at':time.time()}})
        return process_one(ident)
    except Exception:
        # Graph may already have committed. Never encourage replay of the action.
        return {'status':'pending'}


def process_one(ident=None):
    now=time.time();lease=uuid.uuid4().hex
    query={'attempts':{'$lt':8},'$or':[{'state':{'$in':['pending','waiting_graph']},'due_at':{'$lte':now}},
        {'state':'processing','lease_until':{'$lte':now}}]}
    if ident:query['_id']=ident
    row=JOBS.find_one_and_update(query,{'$set':{'state':'processing','lease':lease,'lease_until':now+90},
        '$inc':{'attempts':1}},sort=[('due_at',1)],return_document=ReturnDocument.AFTER)
    if not row:return {'status':'idle'}
    guard={'_id':row['_id'],'state':'processing','lease':lease}
    try:
        source=graph_call('source',{'owner':row['owner']})
        active={(r['concept']['key'],r['relation']) for r in source['rows']}
        with projection_write(row['owner'],source_created_at=time.time()) as session:
            facts=list(FACTS.find({'user_id':row['owner'],'concept_key':row['key']},session=session).limit(2))
            if len(facts)>1:raise ValueError('ambiguous_fact_projection')
            for fact in facts:
                polarity='PREFERS' if fact.get('stance') in {'like','require'} else 'AVOIDS' if fact.get('stance') in {'dislike','avoid'} else None
                # Preserve all historical source/identity fields and counters.
                # A correction may retire this fact, never rewrite its identity.
                FACTS.update_one({'_id':fact['_id'],'user_id':row['owner']},
                    {'$set':{'active':(row['key'],polarity) in active}},session=session)
        if graph_call('source',{'owner':row['owner']})['snapshot_hash'] != source['snapshot_hash']:
            raise ValueError('projection_source_changed')
        JOBS.update_one(guard,{'$set':{'state':'complete','completed_at':time.time()},'$unset':{'lease':'','lease_until':''}})
        return {'status':'synced'}
    except Exception:
        JOBS.update_one(guard,{'$set':{'state':'failed' if row['attempts']>=8 else 'pending',
            'due_at':time.time()+min(3600,30*2**row['attempts']),'error_code':'preference_projection_unavailable'},
            '$unset':{'lease':'','lease_until':''}})
        return {'status':'pending'}


def _worker():
    while not _stop.wait(15):
        try:process_one()
        except Exception:pass


def start_worker():
    global _thread
    if _thread and _thread.is_alive():return
    _stop.clear();_thread=threading.Thread(target=_worker,name='preference-action-projection',daemon=True);_thread.start()


def stop_worker():
    _stop.set()
    if _thread:_thread.join(timeout=2)
