"""Manual, isolated entry point for exactly the two Hunter HTTP Python suites."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
# Explicit paths are necessary with Python -I; never depend on the caller's cwd.
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
sys.dont_write_bytecode = True
EXPECTED = {"test_hunter_http_evidence": 11, "test_hunter_bybit_worker": 30}


def deny_external_effects(event, args):
    if (event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname",
                  "socket.gethostbyaddr", "socket.sendto", "socket.sendmsg",
                  "subprocess.Popen", "os.system", "os.posix_spawn", "os.fork", "os.forkpty"}
            or event.startswith(("os.exec", "os.spawn"))):
        raise RuntimeError("OFFLINE_TEST_EXTERNAL_EFFECT_BLOCKED:" + event)


def main():
    # Keep the inherited environment, including SystemRoot. No credentials are read.
    # Guard imports as well as tests; mocks cannot accidentally reach a real service.
    sys.addaudithook(deny_external_effects)
    previous = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="hunter-http-offline-") as directory:
        try:
            # Any relative output is confined to disposable test scratch space.
            os.chdir(directory)
            loader = unittest.TestLoader()
            suites = []
            actual = {}
            for module, expected in EXPECTED.items():
                suite = loader.loadTestsFromName(module)
                actual[module] = suite.countTestCases()
                suites.append(suite)
                print(f"{module}: expected={expected} loaded={actual[module]}", flush=True)
            result = unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(unittest.TestSuite(suites))
            expected_total = sum(EXPECTED.values())
            passed = (actual == EXPECTED and not loader.errors and result.wasSuccessful()
                      and result.testsRun == expected_total and not result.skipped
                      and not result.expectedFailures)
            summary = dict(expected=expected_total, loaded=sum(actual.values()), actual_run=result.testsRun,
                           import_errors=len(loader.errors), errors=len(result.errors),
                           failures=len(result.failures), skipped=len(result.skipped),
                           expected_failures=len(result.expectedFailures),
                           unexpected_successes=len(result.unexpectedSuccesses), passed=passed,
                           exit_code=0 if passed else 1)
            print("OFFLINE_TEST_SUMMARY " + json.dumps(summary, sort_keys=True), flush=True)
            return summary["exit_code"]
        finally:
            os.chdir(previous)


if __name__ == "__main__":
    raise SystemExit(main())
