import unittest
from research import hunter_bybit_signal_capture as capture

class BybitSignalCaptureTests(unittest.TestCase):
 def test_official_candles_produce_venue_matched_research_signal(self):
  def fetch(symbol,interval,limit):
   rows=[]
   for i in range(limit):
    price=100 if symbol=="BTCUSDT" else 2+i*.08
    rows.append([str(i),str(price),str(price*1.01),str(price*.99),str(price),"1","100"])
   return rows
  rows=[{"base":"FLUID","pair":"FLUIDUSDT","venue":"bybit"}]
  signals,failures=capture.capture(rows,fetch)
  self.assertEqual(failures,{})
  self.assertEqual(signals["FLUID"]["source_venue"],"bybit")
  self.assertFalse(signals["FLUID"]["execution_supported"])
  self.assertEqual(signals["FLUID"]["stage"],"EARLY")

 def test_missing_candles_never_create_an_early_signal(self):
  def fetch(symbol,interval,limit):
   if symbol=="FLUIDUSDT":raise ValueError("missing")
   return [[str(i),"100","101","99","100","1","100"] for i in range(limit)]
  signals,failures=capture.capture([{"base":"FLUID","pair":"FLUIDUSDT","venue":"bybit"}],fetch)
  self.assertNotIn("FLUID",signals)
  self.assertIn("FLUIDUSDT",failures)
