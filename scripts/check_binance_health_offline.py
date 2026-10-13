"""Guarded health contract suite; public network and non-scratch writes forbidden."""
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.check_binance_public_transport import main

if __name__=='__main__':raise SystemExit(main(pattern='test_hunter_binance_health*.py',expected=37))
