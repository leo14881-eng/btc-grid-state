import datetime as dt
import time
import unittest
from unittest.mock import patch
from research import hunter_market as m
from research import hunter_position_monitor as monitor
from research import hunter_shadow_trader_v2 as eng
from tests import test_hunter_position_monitor as fixtures

BOOK={"bids":[["99","1000"],["98","1000"]],"asks":[["101","1000"],["102","1000"]]}

class MarketAdapterTests(unittest.TestCase):
 def test_binding_is_pinned_even_if_another_exchange_lists_same_ticker(self):
  pos={"asset":"FLUID","execution_venue":"bybit","execution_pair":"FLUIDUSDT"}
  coin={"venues":["binance","bybit"],"reference_price":999,"venue_prices":{"binance":999,"bybit":2}}
  self.assertEqual(m.binding(position=pos),("bybit","FLUIDUSDT"))
  self.assertEqual(m.mark(coin,pos),2)
  self.assertIsNone(m.mark({"venues":["binance"],"reference_price":999},pos))

 def test_legacy_positions_remain_binance(self):
  self.assertEqual(m.binding(position={"asset":"X"}),("binance","XUSDT"))

 def test_unknown_venue_and_mismatched_pair_are_blocked(self):
  with self.assertRaises(ValueError):m.adapter("unknown")
  with self.assertRaises(ValueError):m.mark({}, {"asset":"X","execution_venue":"bybit","execution_pair":"WRONGUSDT"})

 def test_bybit_books_are_batched_and_wrong_symbol_is_explicit(self):
  calls=[]
  def fetch(url,body):
   calls.append(body)
   return {"retCode":0,"result":{"category":"spot","books":{p:{"retCode":0,"result":{"category":"spot","s":("WRONGUSDT" if p=="ALT0USDT" else p),"ts":time.time()*1000,"b":BOOK["bids"],"a":BOOK["asks"]}} for p in body["symbols"]}}}
  pairs=["ALT"+str(i)+"USDT" for i in range(41)]
  found,errors=m.Bybit(fetch).books(pairs)
  self.assertEqual(len(calls),3);self.assertTrue(all(len(x["symbols"])<=20 for x in calls))
  self.assertEqual(len(found),40);self.assertIn("ALT0USDT",errors)

 def test_stale_and_crossed_books_never_become_executable(self):
  for invalid in ("stale","crossed"):
   def fetch(url,body):return {"retCode":0,"result":{"category":"spot","books":{"XUSDT":{"retCode":0,"result":{"category":"spot","s":"XUSDT","ts":0 if invalid=="stale" else time.time()*1000,"b":[["102","1"]] if invalid=="crossed" else BOOK["bids"],"a":BOOK["asks"]}}}}}
   found,errors=m.Bybit(fetch).books(["XUSDT"])
   self.assertFalse(found);self.assertIn("XUSDT",errors)

 def test_failed_bybit_quote_batch_does_not_stop_binance_management(self):
  states=[{"open_positions":[{"asset":"X","execution_venue":"bybit","execution_pair":"XUSDT"}]}]
  class Failed:
   def quotes(self):raise TimeoutError("bounded timeout")
  with patch.object(m,"adapter",return_value=Failed()):
   market,errors=monitor.merge_bound_markets(states,{"BTC":{"reference_price":100,"change_24h_pct":0}})
  self.assertEqual(market["BTC"]["venue_prices"]["binance"],100)
  self.assertIn("bybit",errors)
  self.assertIsNone(m.mark(market.get("X",{}),states[0]["open_positions"][0]))

 def test_registry_extension_does_not_require_monitor_strategy_changes(self):
  class Added:
   def quotes(self):return {"XUSDT":{"reference_price":2,"change_24h_pct":0}}
  with patch.dict(m.REGISTRY,{"newvenue":Added}):
   states=[{"open_positions":[{"asset":"X","execution_venue":"newvenue"}]}]
   market,errors=monitor.merge_bound_markets(states,{})
   self.assertEqual(market["X"]["venue_prices"]["newvenue"],2);self.assertFalse(errors)

 def test_closed_history_uses_pinned_venue_and_bounded_requests(self):
  class History:
   def __init__(self):self.calls=[]
   def candles(self,pair,interval,limit,start=None,end=None):
    self.calls.append((pair,start,end))
    return [[start,"10","12","9","11","1",start+299999,"10"]]
  source=History();start=dt.datetime(2026,10,5,tzinfo=dt.timezone.utc)
  old=eng._opportunity_backfill_requests
  try:
   eng._opportunity_backfill_requests=0
   with patch.object(m,"adapter",return_value=source):bars=eng.venue_kline_bars("bybit","XUSDT",start,start+dt.timedelta(minutes=10))
   self.assertTrue(bars);self.assertEqual(source.calls[0][0],"XUSDT")
   eng._opportunity_backfill_requests=eng.OPPORTUNITY_BACKFILL_BUDGET
   self.assertIsNone(eng.venue_kline_bars("bybit","XUSDT",start,start+dt.timedelta(hours=1)))
   self.assertEqual(len(source.calls),1)
  finally:eng._opportunity_backfill_requests=old

class BybitLifecycleTests(unittest.TestCase):
 def fixture(self,price=100):
  state,market,review,liq,supply=fixtures.PositionMonitorTests().fixture(price)
  pos=state["open_positions"][0]
  pos.update(execution_venue="bybit",execution_pair="XUSDT")
  market["X"].update(venues=["bybit"],venue_prices={"bybit":price})
  review["candidates"][0]["signal"]["source_venue"]="bybit"
  liq["snapshots"]["X"].update(venue="bybit",pair="XUSDT")
  return state,market,review,liq,supply

 def test_shadow_buy_add_sell_and_closed_observation_stay_on_same_venue(self):
  state,market,review,liq,supply=self.fixture(90)
  pos=state["open_positions"][0];now=dt.datetime(2026,10,4,tzinfo=dt.timezone.utc)
  eng.trade_event(state,pos,"BUY",now,100,"TEST_SHADOW_BUY")
  eng.manage_existing_positions(state,{"coins":market},review,liq,supply,now,100)
  self.assertEqual(len(pos["tranches"]),2)
  market["X"]["venue_prices"]["bybit"]=120;market["X"]["reference_price"]=999
  review["candidates"][0]["signal"].update(btc_relative_1h_pct=-1,btc_relative_4h_pct=-1,relative_acceleration_pct=-1)
  eng.manage_existing_positions(state,{"coins":market},review,liq,supply,now,100)
  self.assertEqual(state["open_positions"],[])
  self.assertEqual(state["closed_positions"][0]["exit_reference_price"],120)
  self.assertTrue(all(x["execution_venue"]=="bybit" for x in state["events"]))
  self.assertTrue(any(x["type"].endswith("_SELL") for x in state["events"]))

 def test_wrong_venue_evidence_cannot_trigger_add_or_exit(self):
  state,market,review,liq,supply=self.fixture(120)
  liq["snapshots"]["X"]["venue"]="binance"
  now=dt.datetime(2026,10,4,tzinfo=dt.timezone.utc)
  eng.manage_existing_positions(state,{"coins":market},review,liq,supply,now,100)
  self.assertEqual(len(state["open_positions"]),1);self.assertFalse(state["events"])
  self.assertIn("POSITION_MARKET_EVIDENCE_VENUE_MISMATCH",state["decisions"][-1]["reasons"])

 def test_missing_quote_cannot_reuse_another_exchange_or_generate_trade(self):
  state,market,review,liq,supply=self.fixture(120)
  market["X"]={"venues":["binance"],"reference_price":999}
  eng.manage_existing_positions(state,{"coins":market},review,liq,supply,dt.datetime(2026,10,4,tzinfo=dt.timezone.utc),100)
  self.assertEqual(len(state["open_positions"]),1);self.assertFalse(state["events"])
  self.assertIn("CURRENT_PRICE_MISSING",state["decisions"][-1]["reasons"])
