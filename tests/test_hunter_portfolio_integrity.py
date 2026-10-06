import json
import tempfile
import unittest
from pathlib import Path
from research import hunter_shadow_trader_v2 as engine
from research import hunter_position_monitor as monitor


def empty():
    return {"schema": "hunter_shadow_v2_portfolio_v2", "mode": "SIMULATION_ONLY_NO_REAL_ORDERS",
            "open_positions": [], "closed_positions": [], "events": [], "decisions": []}


class PortfolioIntegrityTests(unittest.TestCase):
    def test_missing_and_corrupt_ssot_never_use_empty_default(self):
        with tempfile.TemporaryDirectory() as d:
            for name in engine.PORTFOLIO_NAMES:
                p = Path(d) / name
                for reader in (lambda path: engine.load(path, empty()), monitor.load):
                    with self.assertRaisesRegex(RuntimeError, "SHADOW_PORTFOLIO_READ_FAILED"):
                        reader(p)
                p.write_text('{"open_positions":[')
                before = p.read_bytes()
                for reader in (engine.load, monitor.load):
                    with self.assertRaisesRegex(RuntimeError, "SHADOW_PORTFOLIO_READ_FAILED"):
                        reader(p)
                self.assertEqual(p.read_bytes(), before)

    def test_invalid_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "hunter-shadow-portfolio.json"
            for bad in ({}, [], {**empty(), "events": None}, {**empty(), "mode": "LIVE"}):
                p.write_text(json.dumps(bad))
                with self.assertRaisesRegex(RuntimeError, "SHADOW_PORTFOLIO_SCHEMA_INVALID"):
                    engine.load(p)

    def test_history_reset_rejected_without_replacing_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            for name in engine.PORTFOLIO_NAMES:
                p = Path(d) / name
                for field in ("closed_positions", "closed_trade_archive", "events", "excluded_non_crypto_positions"):
                    prior = {**empty(), field: [{"asset": "AXS", "shadow_id": "historic"}]}
                    p.write_text(json.dumps(prior))
                    before = p.read_bytes()
                    with self.assertRaisesRegex(RuntimeError, "SHADOW_PORTFOLIO_EMPTY_RESET_REJECTED"):
                        engine.atomic_json_write(p, empty())
                    self.assertEqual(p.read_bytes(), before)
                    self.assertFalse(p.with_suffix(".json.tmp").exists())

    def test_normal_close_archive_and_quarantine_preserve_history(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "hunter-shadow-portfolio.json"
            row = {"asset": "AXS", "shadow_id": "historic"}
            for dest in ("closed_positions", "closed_trade_archive", "excluded_non_crypto_positions"):
                p.write_text(json.dumps({**empty(), "open_positions": [row]}))
                engine.atomic_json_write(p, {**empty(), dest: [row]})
                self.assertEqual(engine.load(p)[dest], [row])
            p.write_text(json.dumps(empty()))
            engine.atomic_json_write(p, empty())
            self.assertEqual(engine.load(p), empty())

    def test_optional_research_cache_fallback_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "hunter-api-cooldown.json"
            self.assertEqual(engine.load(p, {"cache": []}), {"cache": []})
            self.assertEqual(monitor.load(p), {})


if __name__ == "__main__":
    unittest.main()
