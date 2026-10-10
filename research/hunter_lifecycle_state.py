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
  identity=(pos.get('execution_venue'),pos.get('market_symbol'),pos.get('market_type'))
  venue=pos.get('execution_venue') or 'BINANCE_SPOT'
  if venue not in ('BINANCE_SPOT','BYBIT_SPOT'):raise ValueError('POSITION_EXECUTION_IDENTITY_MISMATCH')
  if any(identity) and identity!=(venue,pos['asset']+'USDT','spot'):raise ValueError('POSITION_EXECUTION_IDENTITY_MISMATCH')
  if venue=='BYBIT_SPOT':
   if pos.get('execution_fee_bps') is None:raise ValueError('BYBIT_FEE_MODEL_UNKNOWN')
   source_at=float(book.get('source_timestamp'))/1000
   if not math.isfinite(source_at) or not 0<=now.timestamp()-source_at<=600:raise ValueError('BOOK_STALE_OR_MISSING')
  if not math.isfinite(float(fee_bps)) or not 0<=float(fee_bps)<=100:raise ValueError('EXECUTION_FEE_INVALID')
  if pos.get('execution_fee_bps') is not None and pos['execution_fee_bps']!=fee_bps:raise ValueError('POSITION_EXECUTION_FEE_MISMATCH')
  if book.get('exchange')!=('binance' if venue=='BINANCE_SPOT' else 'bybit') or book.get('market')!='spot' or book.get('symbol')!=pos['asset']+'USDT':raise ValueError('BOOK_IDENTITY_MISMATCH')
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
  return {'status':'SHADOW_RECEIPT_ESTIMATE','venue':venue,'net_pnl_usdt':net,'vwap':proceeds/q,'quantity':q,'capital':n,'fee_bps':fee_bps,'book_mid':(bids[0][0]+asks[0][0])/2,'fetched_at':book['fetched_at'],'source_timestamp':book.get('source_timestamp'),'historical_execution_verified':False}
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

def recovery(pos, e, now, pnl, generation, health, source_closed_at_ms=None):
 """Observation only: no new loss SELL, time/price stops or partial reductions."""
 if health=='EVIDENCE_PENDING' or not generation or pos.get('recovery_generation_id')==generation:return
 old=pos.get('recovery_state','NONE');row=pos.setdefault('loss_recovery_lifecycle',{'schema':'hunter_loss_recovery_v1','persistent_invalidation_count':0,'recovery_observations':0,'transitions':[]})
 # Historical recovery confirmation is separate from the active episode counters.
 if not row.get('last_recovered_at_utc'):
  recovered=next((t for t in reversed(row.get('transitions',[])) if t.get('to')=='RECOVERED'),None)
  if recovered:
   row.update(last_recovered_at_utc=recovered['at'],last_recovered_generation_id=recovered['generation_id'])
 if old=='NONE':
  row['persistent_invalidation_count']=0;row['recovery_observations']=0
 if source_closed_at_ms is not None:
  previous_source=row.get('source_closed_at_ms')
  if previous_source is None or source_closed_at_ms-previous_source!=900000:
   row['persistent_invalidation_count']=0;row['recovery_observations']=0
  row['source_closed_at_ms']=source_closed_at_ms
 elif row.get('observed_at_utc') and not fresh(row['observed_at_utc'],now):
  row['persistent_invalidation_count']=0;row['recovery_observations']=0
 if pnl<0 and health=='THESIS_INVALIDATED':
  row['persistent_invalidation_count']+=1;row['recovery_observations']=0
  state='PERSISTENT_INVALIDATION' if row['persistent_invalidation_count']>=3 else 'LOSS_RECOVERY'
 elif old in ('LOSS_RECOVERY','PERSISTENT_INVALIDATION','RECOVERING'):
  row['persistent_invalidation_count']=0  # current generation is not invalidated
  quality=health=='STRONG' and all(e.get(k) is not None and e[k]>0 for k in ('btc_rel_1h','btc_rel_4h','rel_accel'))
  row['recovery_observations']=row['recovery_observations']+1 if quality else 0
  row['persistent_invalidation_count']=0 if quality else row['persistent_invalidation_count']
  state='RECOVERED' if row['recovery_observations']>=3 else ('RECOVERING' if quality else 'LOSS_RECOVERY')
 else:state='NONE'
 if state=='RECOVERED' and state!=old:
  row.update(last_recovered_at_utc=now.isoformat(),last_recovered_generation_id=generation)
 if state=='NONE':
  row['persistent_invalidation_count']=0;row['recovery_observations']=0
 row.update(generation_id=generation,observed_at_utc=now.isoformat(),health_state=health,health_reasons=list(pos.get('health_reasons',[])),score=e.get('score'),independent=e.get('independent'),btc_relative_1h=e.get('btc_rel_1h'),btc_relative_4h=e.get('btc_rel_4h'),relative_acceleration=e.get('rel_accel'),spread_bps=e.get('spread_bps'),bid_depth=e.get('bid_depth_2pct_usdt'),supply_identity_risk=e.get('blockers'),mfe_pct=pos.get('mfe_pct'),mae_pct=pos.get('mae_pct'),capital_locked_usdt=sum(t['notional_usdt'] for t in pos['tranches']) if pnl<0 else 0,risk_reduction_authorized=False,counterfactual={'scope':'OBSERVATION_ONLY','current_net_pnl_usdt':pnl,'future_recovery':'UNKNOWN'})
 if state!=old:row['transitions']=(row['transitions']+[{'from':old,'to':state,'generation_id':generation,'at':now.isoformat()}])[-32:]
 pos.update(recovery_state=state,recovery_generation_id=generation)
 pos['health_lifecycle_snapshot']={'generation_id':generation,'observed_at_utc':now.isoformat(),'health_state':pos.get('health_state'),'health_reasons':list(pos.get('health_reasons',[])),'degraded_cycles':pos.get('degraded_cycles',0),'recovery_state':state}

def mtm(state, now, net_function, fee_bps):
 rows=state.get('open_positions',[]);closed=state.get('closed_positions',[])+state.get('closed_trade_archive',[])
 realized=sum(float(p.get('net_pnl_usdt',0)) for p in closed)
 marks=[];unknown=[];loss=0;costs=[];exits=[];exit_unknown=[];cost_unknown=[]
 for p in rows:
  asset=p.get('asset')
  try:
   position_fee=p.get('execution_fee_bps',fee_bps)
   if p.get('execution_venue')=='BYBIT_SPOT' and p.get('execution_fee_bps') is None:raise ValueError('BYBIT_FEE_MODEL_UNKNOWN')
   position_fee=float(position_fee)
   if not math.isfinite(position_fee) or not 0<=position_fee<=100:raise ValueError('EXECUTION_FEE_INVALID')
   capital=sum(float(t['notional_usdt']) for t in p['tranches'])
   q=sum(float(t['notional_usdt'])/(float(t['price'])*(1+(float(t.get('buy_slippage_bps',0))+position_fee)/10000)) for t in p['tranches'])
   if not math.isfinite(capital*q) or capital<=0 or q<=0:raise ValueError('POSITION_QUANTITY_UNKNOWN')
  except (KeyError,TypeError,ValueError,ZeroDivisionError,OverflowError):
   unknown.append(asset);exit_unknown.append(asset);cost_unknown.append(asset);continue
  try:
   price=float(p.get('last_price'))
   if not math.isfinite(price) or price<=0 or not fresh(p.get('last_marked_at_utc'),now):raise ValueError('MARK_UNKNOWN')
   gross=q*price-capital;marks.append(gross)
   if gross<0:loss+=capital
  except (TypeError,ValueError):unknown.append(asset)
  ex=p.get('last_exit_estimate',{})
  try:
   net=float(ex['net_pnl_usdt']);vwap=float(ex['vwap'])
   if (ex.get('status')!='SHADOW_RECEIPT_ESTIMATE' or not fresh(ex.get('fetched_at'),now) or
       ex.get('fee_bps')!=position_fee or not math.isfinite(net) or not math.isfinite(vwap) or vwap<=0):
    raise ValueError('EXIT_ESTIMATE_UNKNOWN')
   if p.get('execution_venue')=='BYBIT_SPOT' and ex.get('venue')!='BYBIT_SPOT':raise ValueError('EXIT_VENUE_MISMATCH')
   for observed,expected in [(ex['quantity'],q),(ex['capital'],capital),(net,q*vwap*(1-position_fee/10000)-capital)]:
    if not math.isclose(float(observed),expected,rel_tol=1e-9,abs_tol=1e-7):raise ValueError('EXIT_CASHFLOW_MISMATCH')
   exits.append(net)
  except (KeyError,TypeError,ValueError,OverflowError):
   exit_unknown.append(asset);cost_unknown.append(asset);continue
  try:
   # A separate ticker price cannot split depth slippage from price movement.
   mid=float(ex['book_mid'])
   if not math.isfinite(mid) or mid<vwap:raise ValueError('EXIT_COST_REFERENCE_UNKNOWN')
   costs.append(q*mid-q*vwap*(1-position_fee/10000))
  except (KeyError,TypeError,ValueError):cost_unknown.append(asset)
 return {'realized_net_pnl_usdt':round(realized,2),
  'open_unrealized_pnl_usdt':round(sum(marks),2) if not unknown else 'UNKNOWN',
  'estimated_exit_cost_usdt':round(sum(costs),2) if not cost_unknown else 'UNKNOWN',
  'mark_to_market_net_pnl_usdt':round(realized+sum(exits),2) if not exit_unknown else 'UNKNOWN',
  'reference_mark_to_market_net_pnl_usdt':round(realized+sum(marks),2) if not unknown else 'UNKNOWN',
  'closed_win_rate':sum(float(p.get('net_pnl_usdt',0))>0 for p in closed)/len(closed) if closed else None,
  'open_loss_exposure_usdt':round(loss,2) if not unknown else 'UNKNOWN',
  'thesis_invalidated_open_count':sum(p.get('health_state')=='THESIS_INVALIDATED' for p in rows),
  'loss_recovery_open_count':sum(p.get('recovery_state') in ('LOSS_RECOVERY','PERSISTENT_INVALIDATION','RECOVERING') for p in rows),
  'mark_data_unknown_assets':unknown,'exit_data_unknown_assets':exit_unknown,
  'estimated_exit_cost_unknown_assets':cost_unknown,
  'mark_to_market_basis':'FULL_QUANTITY_LIQUIDATION_ESTIMATES',
  'estimated_exit_cost_basis':'SAME_DEPTH_RECEIPT_MID',
  'metric_scope':'SHADOW_MODEL_NOT_ACTUAL_FILLS'}
