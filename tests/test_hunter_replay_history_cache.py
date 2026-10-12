import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

from research import hunter_replay_history_cache as cache
from research import hunter_leading_risk_replay as leading
from research import hunter_tail_risk_replay as tail

FIXTURES = Path(__file__).parent / "fixtures"
spec = importlib.util.spec_from_file_location("replay_experiment", FIXTURES / "hunter_replay_history_experiment.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
NOW = fixture.NOW
URL = ("https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=5m"
       "&startTime=1760097600000&endTime=1760184000000&limit=1000")


def request(url=URL, **headers):
    return urllib.request.Request(url, headers={"User-Agent": "hunter-tail-risk-replay/1.0", "Accept": "application/json", **headers})


class CacheCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "cache"
        self.root.mkdir(mode=0o700)
        self.env = {"HUNTER_REPLAY_CACHE": "1", "HUNTER_REPLAY_CACHE_DIR": str(self.root),
                    "HUNTER_REPLAY_CACHE_GENERATION": "scan-generation-1",
                    "HUNTER_REPLAY_CACHE_CYCLE": "a" * 32,
                    "HUNTER_REPLAY_CACHE_STARTED_AT": str(NOW - 1)}
        self.environment = mock.patch.dict(os.environ, self.env)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.clock = mock.patch.object(cache.time, "time", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.transport = mock.Mock(side_effect=lambda req, timeout: fixture.Response(fixture.body_for(req.full_url), req.full_url))

    def load(self, req=None):
        return cache.load_json(req or request(), self.transport)

    def files(self):
        return list(self.root.glob("*/*.json"))

    def identity(self):
        return cache._identity(request(), NOW)


class EligibilityTests(CacheCase):
    def test_default_off_does_not_touch_cache_or_change_request(self):
        os.environ.pop("HUNTER_REPLAY_CACHE")
        with mock.patch.object(cache, "_directory", side_effect=AssertionError("disabled")) as disk:
            self.assertEqual(self.load(), self.load())
        self.assertEqual(self.transport.call_count, 2)
        self.assertEqual(disk.call_count, 0)
        self.assertFalse(self.files())

    def test_missing_or_invalid_explicit_context_falls_back(self):
        for field in self.env:
            with self.subTest(field=field), mock.patch.dict(os.environ, {field: ""}):
                before = self.transport.call_count
                self.load(); self.load()
                self.assertEqual(self.transport.call_count - before, 2)
        for value in (str(NOW + 1), str(NOW - 900), "nan", "inf", "bad"):
            with mock.patch.dict(os.environ, {"HUNTER_REPLAY_CACHE_STARTED_AT": value}):
                self.load(); self.load()
        self.assertFalse(self.files())

    def test_unapproved_or_ambiguous_requests_never_reuse(self):
        variants = [URL.replace("data-api.binance.vision", "other.example"),
                    URL.replace("https:", "http:"), URL.replace("/api/v3/klines", "/api/v3/account"),
                    URL + "&signature=secret", URL + "&symbol=BTCUSDT", URL + "&timeZone=8",
                    URL + "#fragment", URL.replace("1000", "999"), URL.replace("5m", "1m"),
                    URL.replace("BTCUSDT", "OTHERUSDT"), URL.replace("https://", "https://user:pass@"),
                    URL.replace("1760184000000", "1760184000001")]
        for url in variants:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    cache._identity(request(url), NOW)
        for headers in ({"Authorization": "secret"}, {"Cookie": "secret"}, {"Accept": "text/html"}, {"User-Agent": "unknown"}):
            with self.assertRaises(ValueError):
                cache._identity(request(**headers), NOW)
        post = request(); post.data = b"secret"
        with self.assertRaises(ValueError):
            cache._identity(post, NOW)

    def test_closed_window_and_all_rows_required_including_boundary(self):
        identity = self.identity()
        raw = fixture.body_for(URL)
        self.assertTrue(cache._rows_valid(raw, identity, NOW))
        rows = json.loads(raw)
        invalid = [rows[:-1], rows[1:], rows + rows[-1:], rows[::-1], [], {"code": -1003}]
        for index, value in ((0, True), (6, rows[0][6] + 1), (4, "NaN"), (2, "1"), (5, "-1"), (8, "10"), (11, "secret")):
            mutated = copy.deepcopy(rows); mutated[0][index] = value; invalid.append(mutated)
        for value in invalid:
            self.assertFalse(cache._rows_valid(fixture.encoded(value), identity, NOW))
        self.assertFalse(cache._rows_valid(b" " * (cache.MAX_BODY + 1), identity, NOW))
        with self.assertRaises(ValueError):
            cache._identity(request(), int(identity["request"]["query"]["endTime"]) / 1000 + 1)

    def test_cache_io_faults_do_not_add_attempts(self):
        with mock.patch.object(cache, "_directory", side_effect=PermissionError("denied")):
            self.load(); self.load()
        self.assertEqual(self.transport.call_count, 2)

    def test_network_errors_keep_original_leading_retry_schedule_and_tail_once(self):
        for status in (403, 429, 500):
            error = urllib.error.HTTPError(URL, status, "fixture failure", {}, None)
            with (mock.patch("urllib.request.urlopen", side_effect=error) as http,
                  mock.patch.object(leading.time, "sleep") as sleep):
                with self.assertRaises(urllib.error.HTTPError):
                    leading.get(URL)
                self.assertEqual(http.call_count, 4)
                self.assertEqual(sleep.call_args_list, [mock.call(.5), mock.call(1.0), mock.call(2.0)])
                http.reset_mock()
                with self.assertRaises(urllib.error.HTTPError):
                    tail.get(URL)
                self.assertEqual(http.call_count, 1)
        self.assertFalse(self.files())


@unittest.skipUnless(os.name == "posix", "secure dirfd cache intentionally disabled on Windows")
class PosixCacheTests(CacheCase):
    def test_first_exact_repeat_query_order_and_both_user_agents(self):
        a = self.load()
        path = self.files()[0]; before = path.read_bytes(); stamp = path.stat().st_mtime_ns
        a[0][4] = "changed by consumer"
        reordered = URL.split("?")[0] + "?" + "&".join(reversed(URL.split("?")[1].split("&")))
        with mock.patch.object(cache.time, "time", return_value=NOW + 100):
            b = self.load(request(reordered, **{"User-Agent": "hunter-leading-replay/1.0"}))
        self.assertNotEqual(a, b)
        self.assertEqual(b, json.loads(fixture.body_for(URL)))
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertEqual(json.loads(before)["fetched_at"], NOW)

    def test_generation_cycle_origin_and_material_params_do_not_collide(self):
        self.load()
        for field, value in (("HUNTER_REPLAY_CACHE_GENERATION", "scan-2"), ("HUNTER_REPLAY_CACHE_CYCLE", "b" * 32)):
            with mock.patch.dict(os.environ, {field: value}):
                self.load()
        self.load(request(URL.replace("data-api.binance.vision", "api.binance.com")))
        self.load(request(URL.replace("BTCUSDT", "ETHUSDT")))
        self.load(request(URL.replace("1760184000000", "1760184300000")))
        self.assertEqual(self.transport.call_count, 6)

    def test_failed_request_retries_then_success_can_be_reused(self):
        error = urllib.error.HTTPError(URL, 429, "limit", {}, None)
        self.transport.side_effect = [error, fixture.Response(fixture.body_for(URL), URL)]
        with self.assertRaises(urllib.error.HTTPError):
            self.load()
        self.assertFalse(self.files())
        self.load(); self.load()
        self.assertEqual(self.transport.call_count, 2)

    def test_partial_error_payload_redirect_and_non_json_are_not_stored(self):
        raw = fixture.body_for(URL)
        cases = [(fixture.encoded(json.loads(raw)[:-1]), URL, 200, "application/json"),
                 (b'{"code":-1003}', URL, 200, "application/json"),
                 (raw, URL, 429, "application/json"), (raw, URL, 200, "text/html"),
                 (raw, URL.replace("data-api.binance.vision", "api.binance.com"), 200, "application/json")]
        for body, url, status, content_type in cases:
            self.transport.side_effect = lambda req, timeout: fixture.Response(body, url, status, content_type)
            self.load(); self.load()
            self.assertFalse(self.files())
        def cookie(req, timeout):
            response = fixture.Response(raw, URL)
            response.headers["Set-Cookie"] = "secret"
            return response
        self.transport.side_effect = cookie
        self.load(); self.load()
        self.assertFalse(self.files())

    def test_corrupt_oversized_wrong_identity_or_future_envelope_misses(self):
        self.load(); path = self.files()[0]; valid = path.read_bytes()
        invalid = [b"{", b" " * (cache.MAX_ENTRY + 1)]
        for field, value in (("fetched_at", NOW + 1), ("fetched_at", NOW - 1000), ("body_sha256", "0" * 64)):
            doc = json.loads(valid); doc[field] = value
            doc.pop("record_sha256")
            doc["record_sha256"] = cache._hash(cache._encoded(doc))
            invalid.append(cache._encoded(doc))
        doc = json.loads(valid); doc["identity"]["generation"] = "other"
        doc.pop("record_sha256"); doc["record_sha256"] = cache._hash(cache._encoded(doc))
        invalid.append(cache._encoded(doc))
        for body in invalid:
            path.write_bytes(body)
            before = self.transport.call_count
            self.load()
            self.assertEqual(self.transport.call_count, before + 1)
            self.assertEqual(path.read_bytes(), body)  # No unsafe overwrite/repair.

    def test_expired_and_clock_rollback_never_hit(self):
        self.load()
        for at in (NOW + cache.TTL_SECONDS, NOW - 2):
            with mock.patch.object(cache.time, "time", return_value=at):
                self.load()
        self.assertEqual(self.transport.call_count, 3)

    def test_oversized_raw_response_passes_to_original_parser_without_storage(self):
        body = fixture.body_for(URL) + b" " * cache.MAX_BODY
        self.transport.side_effect = lambda req, timeout: fixture.Response(body, URL)
        self.assertEqual(self.load(), json.loads(body))
        self.load()
        self.assertEqual(self.transport.call_count, 2)
        self.assertFalse(self.files())

    def test_interrupted_atomic_publish_never_exposes_partial_entry(self):
        with mock.patch.object(cache.os, "link", side_effect=OSError("interrupted before publish")):
            self.load()
        self.assertFalse(self.files())
        namespace = next(self.root.iterdir())
        (namespace / ".partial-crashed-process").write_bytes(b"{")
        self.load(); self.load()
        self.assertEqual(self.transport.call_count, 2)
        self.assertEqual(len(self.files()), 1)

    def test_symlink_parent_namespace_leaf_and_hardlink_safely_fall_back(self):
        self.load(); path = self.files()[0]; raw = path.read_bytes()
        path.unlink()
        outside = Path(self.temp.name) / "outside"
        outside.write_bytes(raw); outside.chmod(0o600)
        path.symlink_to(outside)
        self.load(); self.assertEqual(self.transport.call_count, 2)
        self.assertEqual(outside.read_bytes(), raw)
        path.unlink(); os.link(outside, path)
        self.load(); self.assertEqual(self.transport.call_count, 3)
        parent_link = Path(self.temp.name) / "link"
        parent_link.symlink_to(self.root, target_is_directory=True)
        with mock.patch.dict(os.environ, {"HUNTER_REPLAY_CACHE_DIR": str(parent_link)}):
            self.load(); self.assertEqual(self.transport.call_count, 4)
        path.unlink(); namespace = path.parent; namespace.rmdir()
        other = Path(self.temp.name) / "other"; other.mkdir(mode=0o700)
        namespace.symlink_to(other, target_is_directory=True)
        self.load(); self.assertEqual(self.transport.call_count, 5)
        self.assertFalse(list(other.iterdir()))

    def test_insecure_permissions_and_permission_errors_fall_back(self):
        self.load(); path = self.files()[0]
        for target in (path, path.parent, self.root):
            mode = target.stat().st_mode & 0o777
            target.chmod(mode | 0o020)
            before = self.transport.call_count
            self.load(); self.assertEqual(self.transport.call_count, before + 1)
            target.chmod(mode)
        path.unlink()
        with mock.patch.object(cache.os, "fsync", side_effect=PermissionError("denied")):
            self.load()
        self.assertFalse(self.files())

    def test_namespace_and_file_budgets_fall_back_without_cleanup_or_extra_requests(self):
        with mock.patch.object(cache, "MAX_NAMESPACES", 0):
            self.load()
        self.assertFalse(self.files())
        with mock.patch.object(cache, "MAX_FILES", 0):
            self.load()
        self.assertFalse(self.files())
        self.assertEqual(self.transport.call_count, 2)

    def test_hits_do_not_leak_descriptors(self):
        self.load()
        before = len(os.listdir("/proc/self/fd"))
        for _ in range(30):
            self.load()
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)
        self.assertEqual(self.transport.call_count, 1)

    def test_concurrent_processes_same_key_return_valid_original_results(self):
        command = [sys.executable, "-B", str(FIXTURES / "hunter_replay_history_experiment.py"), "--stage", "tail", self.temp.name]
        env = dict(os.environ, HUNTER_BINANCE_API="https://data-api.binance.vision")
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) for _ in range(2)]
        results = []
        try:
            for process in processes:
                out, err = process.communicate(timeout=60)
                self.assertEqual(process.returncode, 0, err)
                results.append(json.loads(out))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill(); process.wait()
        self.assertEqual(results[0]["report"], results[1]["report"])
        self.assertLessEqual(sum(len(x["requests"]) for x in results), 16)
        self.assertGreaterEqual(sum(len(x["requests"]) for x in results), 8)
        self.assertEqual(len(self.files()), 8)
        self.load()
        self.assertEqual(self.transport.call_count, 0)


class FixedInputExperimentTests(unittest.TestCase):
    def test_original_outputs_decisions_financial_proxies_source_hashes_and_request_count(self):
        baseline = json.loads((FIXTURES / "hunter_replay_history_expected.json").read_text())
        result = fixture.experiment()
        for stage in ("tail", "leading"):
            old = baseline["stages"][stage]
            self.assertEqual(result["0"][stage], old)
            self.assertEqual(result["1"][stage]["report"], old["report"])
            self.assertEqual(result["1"][stage]["consumed"], old["consumed"])
        original = sum(len(x["requests"]) for x in result["0"].values())
        enabled = sum(len(x["requests"]) for x in result["1"].values())
        self.assertEqual(original, 32)
        self.assertEqual(enabled, 24 if os.name == "posix" else 32)
        original_hashes = {r["url"]: r["raw_sha256"] for stage in result["0"].values() for r in stage["requests"]}
        self.assertEqual(len(original_hashes), 24)
        for stage in result["1"].values():
            for row in stage["requests"]:
                self.assertEqual(row["raw_sha256"], original_hashes[row["url"]])
        summary = {"original_requests": original, "enabled_requests": enabled,
                   "identical_reports_and_consumed_source_hashes": True,
                   "tail_report_sha256": fixture.digest(result["1"]["tail"]["report"]),
                   "leading_report_sha256": fixture.digest(result["1"]["leading"]["report"]),
                   "synthetic_offline_only": True, "timing_claim": None}
        print("HUNTER_REPLAY_DEDUPE_EXPERIMENT " + json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
