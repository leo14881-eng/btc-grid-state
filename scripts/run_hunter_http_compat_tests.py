"""Manual bounded downstream regression; no network, subprocesses or repo writes."""
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
sys.dont_write_bytecode = True
EXPECTED = {
    "test_hunter_http_evidence": 15,
    "test_hunter_http_compatibility": 11,
    "test_hunter_http_listing": 10,
    "test_hunter_bybit_availability": 13,
    "test_hunter_bybit_worker": 30,
    "test_hunter_bybit_signal_capture": 2,
    "test_hunter_cex_scan": 14,
    "test_hunter_early_signals": 3,
    "test_hunter_chain_consistency": 12,
    "test_hunter_portfolio_integrity": 5,
}
FUNCTION_TESTS = (
    "test_relative_signal_beats_absolute_gain",
    "test_btc_rally_does_not_create_false_alt_strength",
    "test_bybit_only_signal_enters_early_without_becoming_executable",
)


def main():
    previous = Path.cwd()
    previous_tempdir = tempfile.tempdir
    blocked = []
    with tempfile.TemporaryDirectory(prefix="hunter-downstream-offline-") as directory:
        scratch = Path(directory).resolve()

        def block(event):
            blocked.append(event)
            raise RuntimeError("OFFLINE_EFFECT_BLOCKED:" + event)

        def check_write(event, value):
            if isinstance(value, int):
                return  # Existing descriptors, including unittest's stdout.
            candidate = Path(os.fsdecode(value)).resolve()
            if candidate != scratch and scratch not in candidate.parents:
                block(event)

        def guard(event, args):
            if (event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                          "socket.gethostbyaddr", "socket.sendto", "socket.sendmsg",
                          "subprocess.Popen", "os.system", "os.posix_spawn", "os.fork", "os.forkpty"}
                    or event.startswith(("os.exec", "os.spawn"))):
                block(event)
            if event == "open":
                path, mode, flags = args
                if ((isinstance(mode, str) and any(char in mode for char in "wax+"))
                        or (isinstance(flags, int) and flags &
                            (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))):
                    check_write(event, path)
            elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime"}:
                check_write(event, args[0])
            elif event == "os.rename":
                check_write(event, args[0])
                check_write(event, args[1])
            elif event in {"os.link", "os.symlink"}:
                block(event)

        sys.addaudithook(guard)
        try:
            os.chdir(scratch)
            tempfile.tempdir = str(scratch)
            # Preserve the inherited environment, including Windows SystemRoot.
            loader = unittest.TestLoader()
            suites = []
            loaded = {}
            for name, count in EXPECTED.items():
                if name == "test_hunter_early_signals":
                    try:
                        module = importlib.import_module(name)
                        functions = [getattr(module, item) for item in FUNCTION_TESTS]
                        suite = unittest.TestSuite(unittest.FunctionTestCase(item) for item in functions)
                    except Exception as error:
                        # Record an actual failing test and a count mismatch on import failure.
                        def import_failed(cause=error):
                            raise cause
                        suite = unittest.TestSuite([unittest.FunctionTestCase(import_failed)])
                else:
                    suite = loader.loadTestsFromName(name)
                loaded[name] = suite.countTestCases()
                suites.append(suite)
                print(f"{name}: expected={count} loaded={loaded[name]}", flush=True)
            result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(unittest.TestSuite(suites))
            expected = sum(EXPECTED.values())
            passed = (loaded == EXPECTED and result.testsRun == expected and result.wasSuccessful()
                      and not loader.errors and not result.skipped and not result.expectedFailures and not blocked)
            summary = dict(expected=expected, loaded=sum(loaded.values()), actual_run=result.testsRun,
                           errors=len(result.errors), failures=len(result.failures), skipped=len(result.skipped),
                           loader_import_errors=len(loader.errors), expected_failures=len(result.expectedFailures),
                           unexpected_successes=len(result.unexpectedSuccesses),
                           blocked_effects=len(blocked), blocked_event_types=sorted(set(blocked)),
                           passed=passed, exit_code=0 if passed else 1)
            print("DOWNSTREAM_OFFLINE_SUMMARY " + json.dumps(summary, sort_keys=True), flush=True)
            return summary["exit_code"]
        finally:
            os.chdir(previous)
            tempfile.tempdir = previous_tempdir


if __name__ == "__main__":
    raise SystemExit(main())
