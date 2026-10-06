"""Rebind a disposable research checkout to fresh monitor state before execution."""
import argparse
import datetime as dt
import json
import os
import pathlib
from scripts.hunter_monitor_persist import PATHS, git, require, snapshot, validate
from scripts.hunter_monitor_runtime import PROOF_PATH, HEALTH_PATH, should_run

SCAN = "research/results/hunter-cex-universe-run.json"
RULES = "research/results/hunter-shadow-rules.json"


def protected(path):
    return (path.startswith("research/hunter") or
            path.startswith("research/results/hunter-") or
            path.startswith("scripts/hunter_") or
            path.startswith("tests/test_hunter") or
            path.startswith(".github/workflows/hunter-") or
            path.startswith("deploy/systemd/hunter-"))


def research_output(path):
    return (path.startswith("research/results/hunter-") and
            path.endswith((".json", ".jsonl")) and path not in (RULES, PROOF_PATH))


def verified_monitor_proof(latest):
    """Allow only genuine read-back metadata, validated against its own snapshot.

    The newest monitor state may precede publication of its proof. Validate the
    proof's historical snapshot and independently validate latest state below.
    Do not reinterpret a previous-cycle proof as proof of the latest portfolio.
    """
    proof = json.loads(git("show", latest + ":" + PROOF_PATH).stdout)
    config = json.loads(git("show", latest + ":.github/hunter-runtime.json").stdout)
    commit = proof.get("main_readback_head_sha")
    require(isinstance(commit, str) and len(commit) == 40 and
            all(c in "0123456789abcdef" for c in commit), "RESEARCH_MONITOR_PROOF_INVALID_COMMIT")
    def ancestor(sha):
        return git("merge-base", "--is-ancestor", sha, latest, check=False).returncode == 0
    require(ancestor(commit), "RESEARCH_MONITOR_PROOF_NOT_AUTHORITATIVE")
    raw = git("show", commit + ":" + HEALTH_PATH).stdout
    require(not should_run(config, proof, raw, dt.datetime.now(dt.timezone.utc), ancestor),
            "RESEARCH_MONITOR_PROOF_INVALID_OR_STALE")
    validate(snapshot(commit), proof.get("monitor_generation_id"))


def rebind(expected, isolated=False):
    # This reset is permitted only in the disposable CI checkout, never production.
    require(isolated and (os.environ.get("GITHUB_ACTIONS") == "true" or
                         os.environ.get("HUNTER_RESEARCH_ISOLATED_CHECKOUT") == "1"),
            "DISPOSABLE_CI_CHECKOUT_REQUIRED")
    base = git("rev-parse", "HEAD").stdout.strip()
    dirty = git("diff", "--name-only", "HEAD").stdout.splitlines()
    require(all(research_output(p) for p in dirty), "UNEXPECTED_RESEARCH_MUTATION")
    require(not git("diff", "--cached", "--name-only").stdout.strip(),
            "STAGED_RESEARCH_MUTATION")
    git("fetch", "origin", "main")
    latest = git("rev-parse", "origin/main").stdout.strip()
    changed = git("diff", "--name-only", base, latest).stdout.splitlines()
    conflicts = [p for p in changed if protected(p) and p not in PATHS and p != PROOF_PATH]
    require(not conflicts, "RESEARCH_INPUT_CHANGED " + " ".join(conflicts))
    if PROOF_PATH in changed:
        verified_monitor_proof(latest)
    for raw in (pathlib.Path(SCAN).read_text(), git("show", latest + ":" + SCAN).stdout):
        scan = json.loads(raw)
        require(scan.get("generation_id") == expected and
                scan.get("binance_complete") is True, "RESEARCH_GENERATION_SUPERSEDED")
    remote = snapshot(latest)
    health = json.loads(remote["hunter-scheduler-health.json"])
    validate(remote, health.get("last_successful_monitor_generation_id"))
    untracked = git("ls-files", "--others", "--exclude-standard",
                    "research/results").stdout.splitlines()
    keep = {p: pathlib.Path(p).read_bytes() for p in dirty + untracked
            if research_output(p) and p not in PATHS and pathlib.Path(p).is_file()}
    git("reset", "--hard", latest)
    for p, raw in keep.items():
        pathlib.Path(p).write_bytes(raw)
    # Both ledgers, recovery/quarantine, health and leading-risk now come from main.
    require(snapshot() == remote, "RESEARCH_REBIND_SNAPSHOT_MISMATCH")
    print("HUNTER_RESEARCH_REBOUND", latest, expected)
    return latest


def check_current(base, expected):
    git("fetch", "origin", "main")
    changes = git("diff", "--name-only", base, "origin/main").stdout.splitlines()
    conflicts = [p for p in changes if protected(p)]
    require(not conflicts, "HUNTER_STATE_CAS_REJECTED_STALE_WRITER " + " ".join(conflicts))
    scan = json.loads(git("show", "origin/main:" + SCAN).stdout)
    require(scan.get("generation_id") == expected, "RESEARCH_GENERATION_SUPERSEDED")
    print("HUNTER_RESEARCH_EXECUTION_CAS_OK", base, expected)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--generation", required=True)
    parser.add_argument("--isolated-checkout", action="store_true")
    parser.add_argument("--check-base")
    a = parser.parse_args()
    if a.check_base:
        check_current(a.check_base, a.generation)
        return
    base = rebind(a.generation, a.isolated_checkout)
    if os.environ.get("GITHUB_ENV"):
        with open(os.environ["GITHUB_ENV"], "a") as f:
            f.write("HUNTER_EXECUTION_BASE_SHA=" + base + "\n")


if __name__ == "__main__":
    main()
