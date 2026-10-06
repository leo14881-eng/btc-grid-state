"""Persistent sampled shadow lifecycle. No exchange orders or historical fills."""
import datetime as dt
import math

def fresh(at, now, seconds=600):
 try:
  value=dt.datetime.fromisoformat(str(at).replace('Z','+00:00'))
  return value.tzinfo is not None and 0 <= (now-value).total_seconds() <= seconds
 except (ValueError,TypeError):return False

def liquidation(pos, book, now, fee_bps=10):
 """Full held model quantity against observed bids; receipt-only SHADOW estimate."""
 try:
  if not fresh(book.get('fetched_at'),now):raise ValueError('BOOK_STALE_OR_MISSING')
  if book.get('exchange')!='binance' or book.get('market')!='spot' or book.get('symbol')!=pos['asset']+'USDT':raise ValueError('BOOK_IDENTITY_MISMATCH')
  if book.get('price_unit')!='USDT' or book.get('quantity_unit')!='BASE':raise ValueError('BOOK_UNITS_UNKNOWN')
  n=sum(float(t['notional_usdt']) for t in pos['tranches'])
  q=sum(float(t['notional_usdt'])/(float(t['price'])*(1+(float(t.get('buy_slippage_bps',0))+fee_bps)/10000)) for t in pos['tranches'])
  bids=[(float(p),float(q)) for p,q in book['bids']];asks=[(float(p),float(q)) for p,q in book['asks']]
  if not bids or not asks or any(not math.isfinite(p*q) or p<=0 or q<=0 for p,q in bids+asks):raise ValueError('BOOK_INVALID')
  if any(bids[i][0]<=bids[i+1][0] for i in range(len(bids)-1)) or any(asks[i][0]>=asks[i+1][0] for i in range(len(asks)-1)) or bids[0][0]>=asks[0][0]:raise ValueError('BOOK_INVALID')
  remain=q;proceeds=0
  for p,size in bids:
   take=min(size,remain);proceeds+=take*p;remain-=take
   if remain<=1e-10:break
  if remain>1e-10:raise ValueError('FULL_QUANTITY_DEPTH_UNKNOWN')
  net=proceeds*(1-fee_bps/10000)-n
  return {'status':'SHADOW_RECEIPT_ESTIMATE','net_pnl_usdt':net,'vwap':proceeds/q,'quantity':q,'capital':n,'fee_bps':fee_bps,'fetched_at':book['fetched_at'],'source_timestamp':book.get('source_timestamp'),'historical_execution_verified':False}
 except (KeyError,TypeError,ValueError,ZeroDivisionError,OverflowError) as ex:
  return {'status':'UNKNOWN','reason':str(ex),'net_pnl_usdt':None,'historical_execution_verified':False}

def protect(pos, price, reference_net, execution, now, generation, market_at, arm=2, giveback=2, minimum=.35):
 row=pos.setdefault('protection_lifecycle',{'schema':'hunter_profit_lifecycle_v1','state':'UNARMED','historical_arm_status':'UNVERIFIABLE_HISTORICAL_EXECUTION_WINDOW' if pos.get('mfe_pct',0)>=arm else 'NOT_OBSERVED','transitions':[]})
 result={'armed':row['state']!='UNARMED','exit':False,'execution':execution,'state':row['state']}
 if not generation or not fresh(market_at,now):return dict(result,evidence_status='STALE_OR_MISSING')
 if row.get('last_generation_id')==generation or (row.get('last_observed_at_utc') and now<=dt.datetime.fromisoformat(row['last_observed_at_utc'])):return dict(result,evidence_status='DUPLICATE_OR_OLD')
 row.update(last_generation_id=generation,last_observed_at_utc=now.isoformat(),last_reference_price=price,last_reference_net_pnl_usdt=reference_net)
 n=sum(float(t['notional_usdt']) for t in pos['tranches']);entry=sum(float(t['price'])*float(t['notional_usdt']) for t in pos['tranches'])/n
 raw=(price/entry-1)*100;net=execution.get('net_pnl_usdt');old=row['state']
 if row['state']=='UNARMED' and raw>=arm and net is not None and net>0:
  row.update(state='ARMED',armed_at_utc=now.isoformat(),armed_generation_id=generation,armed_reference_price=price,armed_net_pnl_usdt=net,peak_reference_price=price,peak_net_pnl_usdt=net,peak_mfe_pct=raw,protected_floor_usdt=max(n*minimum/100,net-n*giveback/100),capital_basis=n,historical_arm_status='LIVE_SAMPLED_SHADOW_OBSERVATION')
 elif row['state']!='UNARMED':
  if row.get('capital_basis')!=n:
   row.update(capital_basis=n,peak_net_pnl_usdt=net if net is not None else reference_net,protected_floor_usdt=n*minimum/100,cashflow_rebased_at_utc=now.isoformat())
  row['peak_reference_price']=max(row.get('peak_reference_price',price),price)
  row['peak_mfe_pct']=max(row.get('peak_mfe_pct',raw),raw)
  if net is not None:
   row['peak_net_pnl_usdt']=max(row.get('peak_net_pnl_usdt',net),net)
   row['protected_floor_usdt']=max(row.get('protected_floor_usdt',0),n*minimum/100,row['peak_net_pnl_usdt']-n*giveback/100)
   breach=net<=row['protected_floor_usdt']
   row['state']='EXIT_TRIGGERED' if breach else 'PROTECTED'
   if breach and net<=0:row['incident']='GAPPED_THROUGH_PROTECTION_WINDOW'
   elif breach:result['exit']=True
  else:row['execution_gap']='EXIT_COST_UNKNOWN'
 if row['state']!=old:
  row['transitions']=(row.get('transitions',[])+[{'from':old,'to':row['state'],'at':now.isoformat(),'generation_id':generation,'net_pnl_usdt':net}])[-32:]
 return dict(result,armed=row['state']!='UNARMED',state=row['state'],evidence_status='FRESH',protected_floor_usdt=row.get('protected_floor_usdt'),incident=row.get('incident'))

def recovery(pos, e, now, pnl, generation, health):
 """Observation only: no new loss SELL, time/price stops or partial reductions."""
 if health=='EVIDENCE_PENDING' or not generation or pos.get('recovery_generation_id')==generation:return
 old=pos.get('recovery_state','NONE');row=pos.setdefault('loss_recovery_lifecycle',{'schema':'hunter_loss_recovery_v1','persistent_invalidation_count':0,'recovery_observations':0,'transitions':[]})
 if row.get('observed_at_utc') and not fresh(row['observed_at_utc'],now):
  row['persistent_invalidation_count']=0;row['recovery_observations']=0
 if pnl<0 and health=='THESIS_INVALIDATED':
  row['persistent_invalidation_count']+=1;row['recovery_observations']=0
  state='PERSISTENT_INVALIDATION' if row['persistent_invalidation_count']>=3 else 'LOSS_RECOVERY'
 elif old in ('LOSS_RECOVERY','PERSISTENT_INVALIDATION','RECOVERING'):
  quality=health=='STRONG' and all(e.get(k) is not None and e[k]>0 for k in ('btc_rel_1h','btc_rel_4h','rel_accel'))
  row['recovery_observations']=row['recovery_observations']+1 if quality else 0
  row['persistent_invalidation_count']=0 if quality else row['persistent_invalidation_count']
  state='RECOVERED' if row['recovery_observations']>=3 else ('RECOVERING' if quality else 'LOSS_RECOVERY')
 else:state='NONE'
 row.update(generation_id=generation,observed_at_utc=now.isoformat(),health_state=health,health_reasons=list(pos.get('health_reasons',[])),score=e.get('score'),independent=e.get('independent'),btc_relative_1h=e.get('btc_rel_1h'),btc_relative_4h=e.get('btc_rel_4h'),relative_acceleration=e.get('rel_accel'),spread_bps=e.get('spread_bps'),bid_depth=e.get('bid_depth_2pct_usdt'),supply_identity_risk=e.get('blockers'),mfe_pct=pos.get('mfe_pct'),mae_pct=pos.get('mae_pct'),capital_locked_usdt=sum(t['notional_usdt'] for t in pos['tranches']) if pnl<0 else 0,risk_reduction_authorized=False,counterfactual={'scope':'OBSERVATION_ONLY','current_net_pnl_usdt':pnl,'future_recovery':'UNKNOWN'})
 if state!=old:row['transitions']=(row['transitions']+[{'from':old,'to':state,'generation_id':generation,'at':now.isoformat()}])[-32:]
 pos.update(recovery_state=state,recovery_generation_id=generation)
 pos['health_lifecycle_snapshot']={'generation_id':generation,'observed_at_utc':now.isoformat(),'health_state':pos.get('health_state'),'health_reasons':list(pos.get('health_reasons',[])),'degraded_cycles':pos.get('degraded_cycles',0),'recovery_state':state}

def mtm(state, now, net_function, fee_bps):
 rows=state.get('open_positions',[]);closed=state.get('closed_positions',[])+state.get('closed_trade_archive',[])
 realized=sum(float(p.get('net_pnl_usdt',0)) for p in closed);marks=[];unknown=[];loss=0;costs=[]
 for p in rows:
  at=p.get('last_marked_at_utc');price=p.get('last_price')
  if not price or not fresh(at,now):unknown.append(p.get('asset'));continue
  q=sum(t['notional_usdt']/(t['price']*(1+(t.get('buy_slippage_bps',0)+fee_bps)/10000)) for t in p['tranches'])
  gross=q*price-sum(t['notional_usdt'] for t in p['tranches']);marks.append(gross)
  if gross<0:loss+=sum(t['notional_usdt'] for t in p['tranches'])
  ex=p.get('last_exit_estimate',{})
  if ex.get('net_pnl_usdt') is not None and fresh(ex.get('fetched_at'),now) and gross>=ex['net_pnl_usdt']:costs.append(gross-ex['net_pnl_usdt'])
 reliable=len(costs)==len(rows) and not unknown
 return {'realized_net_pnl_usdt':round(realized,2),'open_unrealized_pnl_usdt':round(sum(marks),2) if not unknown else 'UNKNOWN','estimated_exit_cost_usdt':round(sum(costs),2) if reliable else 'UNKNOWN','mark_to_market_net_pnl_usdt':round(realized+sum(marks)-sum(costs),2) if reliable else 'UNKNOWN','reference_mark_to_market_net_pnl_usdt':round(realized+sum(marks),2) if not unknown else 'UNKNOWN','closed_win_rate':sum(float(p.get('net_pnl_usdt',0))>0 for p in closed)/len(closed) if closed else None,'open_loss_exposure_usdt':round(loss,2) if not unknown else 'UNKNOWN','thesis_invalidated_open_count':sum(p.get('health_state')=='THESIS_INVALIDATED' for p in rows),'loss_recovery_open_count':sum(p.get('recovery_state') in ('LOSS_RECOVERY','PERSISTENT_INVALIDATION','RECOVERING') for p in rows),'mark_data_unknown_assets':unknown,'metric_scope':'SHADOW_MODEL_NOT_ACTUAL_FILLS'}
