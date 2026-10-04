"""Single checked-in policy shared by signals, review, shadow lanes and reports."""
import json,pathlib,datetime as dt,hashlib
PATH=pathlib.Path(__file__).parent/'results/hunter-shadow-rules.json'
POLICY=json.loads(PATH.read_text())
if POLICY.get('schema')!='hunter_shadow_rules_v2': raise RuntimeError('HUNTER_POLICY_SCHEMA_INVALID')
C=POLICY['common']; LANES=POLICY['lanes']
VERSION=POLICY['active_version']
def fresh(timestamp,now):
 try:
  age=(now-dt.datetime.fromisoformat(str(timestamp).replace('Z','+00:00'))).total_seconds()
  return 0<=age<=C['MAX_EVIDENCE_AGE_SECONDS']
 except (TypeError,ValueError):return False

def stamp(asset,generation,observed_at):
 return {'generation_id':generation,'observed_at_utc':observed_at,
         'evidence_id':hashlib.sha256(f'{asset}|{generation}|{observed_at}'.encode()).hexdigest()}

def chase_blockers(change,current,anchor,rr,rel1,rel4,accel):
 out=[]
 if not anchor or anchor<=0:out.append('DISCOVERY_ANCHOR_MISSING')
 elif current and (current/anchor-1)*100>C['MAX_CHASE_FROM_DISCOVERY_PCT'] and (rr is None or rr<C['MIN_CHASE_RR']):
  out.append('TOO_FAR_ABOVE_DISCOVERY_FOR_REMAINING_RR')
 if change is not None and change>C['MAX_CHASE_24H_PCT']:
  if not (rr is not None and rr>=C['MIN_CHASE_RR'] and rel1 is not None and rel1>=C['MIN_CHASE_REL_1H'] and rel4 is not None and rel4>=C['MIN_CHASE_REL_4H'] and accel is not None and accel>0):
   out.append('CHASE_NOT_COMPENSATED_BY_EDGE')
 return out
