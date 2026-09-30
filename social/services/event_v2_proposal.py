"""Final Event evidence/qualification proof. Existing lifecycle owns the write."""
import time
import requests
from pymongo import timeout as mongo_timeout

from matchmaker_agent.preference_bootstrap_contract import internal_headers
from services.risk_block_service import risk_block_service

PATH = '/api/events/v2/recheck'


def final_eligible(owner, candidate, receipt, *, profiles, matches, live_query, declined, clock=time.monotonic):
    deadline = clock()+7
    try:
        if not isinstance(receipt, str) or not 40 <= len(receipt) <= 64:
            return False
        if risk_block_service.is_pair_blocked(owner, candidate, deadline=deadline):
            return False
        from routers.match import _candidate_profile_filter
        with mongo_timeout(max(.001, deadline-clock())):
            owners = list(profiles.find({'user_id': owner}, {'_id': 0}).limit(2))
            if len(owners) != 1:
                return False
            people = list(profiles.find(_candidate_profile_filter(owners[0], {owner},
                candidate_ids=[candidate], require_active_context=False), {'_id': 0}).limit(2))
            if len(people) != 1 or people[0].get('user_id') != candidate:
                return False
            if any(matches.count_documents(live_query(uid), limit=1) for uid in (owner,candidate)):
                return False
            if declined(owner, candidate, time.time()):
                return False
        remaining = deadline-clock()
        if remaining <= .1:
            return False
        body = {'owner': owner, 'candidate': candidate, 'receipt': receipt}
        response = requests.post('http://127.0.0.1:9001'+PATH, json=body,
            headers=internal_headers(PATH, body), timeout=(min(1.,remaining), min(5.,remaining)),
            allow_redirects=False)
        response.raise_for_status()
        result = response.json()
        return clock()<deadline and isinstance(result, dict) and result.get('status')=='success' and result.get('eligible') is True
    except Exception:
        return False
