"""Admit current main research generations, including configured Vultr failover."""
import json
import os
import pathlib
import argparse
import math
import re
import subprocess
import tempfile
import time
import uuid
import datetime as dt

SCAN = "research/results/hunter-cex-universe-run.json"
CLAIM = "research/results/hunter-research-ownership.json"
LEASE_SECONDS = 1800


def require(ok, reason):
    if not ok:
        raise RuntimeError(reason)


def push_rejection_category(result):
    # Log fixed categories, not remote URLs, credentials or arbitrary stderr.
    message = (result.stderr or "").lower()
    if any(value in message for value in ("gh006", "gh013", "protected branch", "repository rule")):
        return "POLICY_REJECTED"
    if any(value in message for value in ("authentication failed", "permission denied", "could not read username", "error: 403")):
        return "AUTH_REJECTED"
    if any(value in message for value in ("non-fast-forward", "fetch first", "cannot lock ref")):
        return "RACE"
    return "TRANSPORT_OR_UNKNOWN"


class ClaimStore:
    """CAS on the existing main ref; no remote filesystem/lock assumptions."""
    def __init__(self, cwd="."):
        self.cwd = pathlib.Path(cwd)

    def git(self, *args, check=True, input=None, env=None):
        return subprocess.run(["git", *args], cwd=self.cwd, check=check,
                              capture_output=True, text=True, encoding="utf-8",
                              input=input, env=env)

    def snapshot(self):
        self.git("fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
        head = self.git("rev-parse", "origin/main").stdout.strip()
        scan = json.loads(self.git("show", head + ":" + SCAN).stdout)
        blob = self.git("rev-parse", head + ":" + SCAN).stdout.strip()
        exists = self.git("ls-tree", head, "--", CLAIM).stdout.strip()
        raw = self.git("show", head + ":" + CLAIM).stdout if exists else None
        return head, scan, blob, decode_claim(raw)

    def cas(self, parent, claim, extra=None):
        # Private index: no checkout/index mutation, no force push, no rebase.
        with tempfile.TemporaryDirectory(prefix="research-claim-") as directory:
            env = dict(os.environ, GIT_INDEX_FILE=str(pathlib.Path(directory, "index")),
                       GIT_AUTHOR_NAME="hunter-research", GIT_AUTHOR_EMAIL="hunter-research@users.noreply.github.com",
                       GIT_COMMITTER_NAME="hunter-research", GIT_COMMITTER_EMAIL="hunter-research@users.noreply.github.com")
            self.git("read-tree", parent, env=env)
            for path, raw in {**(extra or {}), CLAIM: encode_claim(claim)}.items():
                blob = self.git("hash-object", "-w", "--stdin", input=raw, env=env).stdout.strip()
                self.git("update-index", "--add", "--cacheinfo", "100644," + blob + "," + path, env=env)
            tree = self.git("write-tree", env=env).stdout.strip()
            commit = self.git("commit-tree", tree, "-p", parent,
                              input="ops: Research ownership " + claim["state"] + "\n", env=env).stdout.strip()
        # A failed/ambiguous transport is resolved from authoritative readback.
        pushed = self.git("push", "origin", commit + ":refs/heads/main", check=False)
        head, _, _, observed = self.snapshot()
        if self.git("merge-base", "--is-ancestor", commit, head, check=False).returncode == 0 and observed == claim:
            for path, raw in (extra or {}).items():
                require(self.git("show", head + ":" + path).stdout == raw,
                        "RESEARCH_HEALTH_READBACK_MISMATCH")
            return commit
        if pushed.returncode and head == parent:
            raise RuntimeError("RESEARCH_CLAIM_PUSH_REJECTED category=" + push_rejection_category(pushed) +
                               " exit=" + str(pushed.returncode))
        return None


def encode_claim(claim):
    return json.dumps(claim, sort_keys=True, indent=2, allow_nan=False) + "\n"


def decode_claim(raw):
    if raw is None:
        return None
    require(len(raw) <= 16384, "RESEARCH_CLAIM_TOO_LARGE")
    try:
        claim = json.loads(raw)
        require(isinstance(claim, dict) and claim.get("schema") == "hunter_research_ownership_v1",
                "RESEARCH_CLAIM_INVALID")
        require(claim.get("state") in ("RUNNING", "PUBLISHED", "FAILED"), "RESEARCH_CLAIM_INVALID")
        require(isinstance(claim.get("owner"), str) and re.fullmatch(r"[0-9a-f]{32}", claim["owner"]),
                "RESEARCH_CLAIM_INVALID")
        require(isinstance(claim.get("generation"), str) and 0 < len(claim["generation"]) <= 200,
                "RESEARCH_CLAIM_INVALID")
        require(isinstance(claim.get("scan_blob"), str) and re.fullmatch(r"[0-9a-f]{40}", claim["scan_blob"]),
                "RESEARCH_CLAIM_INVALID")
        require(all(type(claim.get(k)) in (int, float) and math.isfinite(claim[k])
                    for k in ("issued", "expires")), "RESEARCH_CLAIM_INVALID")
        require(claim["expires"] - claim["issued"] == LEASE_SECONDS, "RESEARCH_CLAIM_INVALID")
        require(claim.get("real_trading_enabled") is False and
                claim.get("capital_authority") == "NONE_SHADOW_ONLY", "RESEARCH_CLAIM_INVALID")
        if claim["state"] == "FAILED":
            require(isinstance(claim.get("failure"), str) and 0 < len(claim["failure"]) <= 2048,
                    "RESEARCH_CLAIM_INVALID")
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError("RESEARCH_CLAIM_INVALID") from exc
    return claim


def acquire(store, generation, force=False, clock=time.time, owner=None):
    owner = owner or uuid.uuid4().hex
    for attempt in range(5):
        head, scan, blob, current = store.snapshot()
        now = clock()
        require(scan.get("generation_id") == generation and scan.get("binance_complete") is True,
                "RESEARCH_GENERATION_SUPERSEDED")
        if current:
            require(current["issued"] <= now + 300, "RESEARCH_CLAIM_CLOCK_INVALID")
            if current["state"] == "RUNNING" and now < current["expires"]:
                raise RuntimeError("RESEARCH_DEFERRED_OWNER_ACTIVE")
            if (current["state"] == "PUBLISHED" and current["generation"] == generation
                    and current["scan_blob"] == blob and not force):
                return None, "ALREADY_PUBLISHED_NOT_A_READBACK_PROOF"
        claim = dict(schema="hunter_research_ownership_v1", owner=owner, generation=generation,
                     scan_blob=blob, issued=now, expires=now + LEASE_SECONDS, state="RUNNING",
                     real_trading_enabled=False, capital_authority="NONE_SHADOW_ONLY")
        commit = store.cas(head, claim)
        if commit:
            return claim, commit
    raise RuntimeError("RESEARCH_CLAIM_CAS_EXHAUSTED")


def verify_owner(store, generation, owner, clock=time.time, snapshot=None):
    require(isinstance(owner, str) and re.fullmatch(r"[0-9a-f]{32}", owner), "RESEARCH_OWNER_REQUIRED")
    head, scan, blob, claim = snapshot or store.snapshot()
    require(bool(owner) and claim is not None and claim["owner"] == owner and
            claim["state"] == "RUNNING", "RESEARCH_OWNERSHIP_LOST")
    require(claim["generation"] == generation == scan.get("generation_id") and
            claim["scan_blob"] == blob and scan.get("binance_complete") is True,
            "RESEARCH_GENERATION_SUPERSEDED")
    now = clock()
    require(claim["issued"] <= now + 300 and now < claim["expires"], "RESEARCH_LEASE_EXPIRED")
    return head, claim


def fail_owned(store, generation, owner, failure, clock=time.time):
    """Best-effort terminal failure; never write a losing contender's diagnostic."""
    if not owner:
        return False
    state = store.snapshot()
    claim = state[3]
    if claim is None or claim["owner"] != owner or claim["state"] != "RUNNING":
        return False
    head, claim = verify_owner(store, generation, owner, clock, state)
    require(isinstance(failure, str) and bool(failure), "RESEARCH_FAILURE_REASON_REQUIRED")
    return bool(store.cas(head, dict(claim, state="FAILED", failure=failure[:2048])))


def publish_owned_health(store, generation, owner, raw):
    """Old success metadata must not invalidate a replacement's protected inputs."""
    for attempt in range(5):
        head, scan, blob, claim = store.snapshot()
        if (claim is None or claim["owner"] != owner or claim["state"] != "PUBLISHED"
                or claim["generation"] != generation or scan.get("generation_id") != generation
                or claim["scan_blob"] != blob):
            return False
        if store.cas(head, claim, {"research/results/hunter-runtime-research-health.json": raw}):
            return True
    raise RuntimeError("RESEARCH_HEALTH_CAS_EXHAUSTED")


def stage_publication(store, generation, owner, clock=time.time):
    _, claim = verify_owner(store, generation, owner, clock)
    published = dict(claim, state="PUBLISHED")
    pathlib.Path(store.cwd, CLAIM).write_text(encode_claim(published), encoding="utf-8")
    store.git("add", "--", CLAIM)


def push_owned(store, generation, owner, clock=time.time):
    head, claim = verify_owner(store, generation, owner, clock)
    commit = store.git("rev-parse", "HEAD").stdout.strip()
    parent = store.git("rev-parse", "HEAD^").stdout.strip()
    require(parent == head, "RESEARCH_PUSH_PARENT_ADVANCED")
    local = decode_claim(store.git("show", commit + ":" + CLAIM).stdout)
    require(local == dict(claim, state="PUBLISHED"), "RESEARCH_PUBLICATION_CLAIM_MISMATCH")
    # This normal FF push is the fencing point. A takeover after verification
    # advances main, making this result commit a sibling that cannot be pushed.
    result = store.git("push", "origin", commit + ":refs/heads/main", check=False)
    if result.returncode:
        latest, _, _, observed = store.snapshot()
        if observed == local and store.git("merge-base", "--is-ancestor", commit, latest, check=False).returncode == 0:
            return  # Lost acknowledgement, publication is authoritative.
        raise RuntimeError("RESEARCH_OWNED_PUSH_REJECTED category=" + push_rejection_category(result) +
                           " exit=" + str(result.returncode))


def server_acquire():
    os.environ.pop("HUNTER_RESEARCH_OWNER", None)
    store = ClaimStore()
    generation = load(SCAN).get("generation_id")
    claim, reason = acquire(store, generation)
    if claim is None:
        print("HUNTER_RESEARCH_ALREADY_PUBLISHED", generation, reason, flush=True)
        return False
    # Before computation only, in the existing disposable checkout. The workflow
    # refresh still checks that the admitted generation remains authoritative.
    store.git("checkout", "--detach", reason)
    os.environ["HUNTER_RESEARCH_OWNER"] = claim["owner"]
    print("HUNTER_RESEARCH_OWNERSHIP_ACQUIRED", generation, reason, flush=True)
    return True


def admit(scan, health, event, trigger_sha, force=False, primary=False):
    if not scan.get("generation_id") or scan.get("binance_complete") is not True:
        raise RuntimeError("INCOMPLETE_AUTHORITATIVE_DISCOVERY")
    aligned = (health.get("scan_generation_id") == scan["generation_id"] or
               health.get("scan_as_of_utc") == scan.get("as_of_utc")
               and bool(scan.get("as_of_utc")))
    if event == "workflow_dispatch":
        return (force or not aligned), "MANUAL_CURRENT_MAIN"
    if event != "workflow_run":
        return False, "UNSUPPORTED_TRIGGER"
    if aligned:
        return False, "GENERATION_ALREADY_ALIGNED"
    if primary:
        # The preceding runtime gate permits this bind only when current Vultr
        # research lacks verified success. Bind latest main, never a trigger's
        # stale checkout. Final generation guard and CAS remain mandatory.
        return True, "VULTR_PRIMARY_RESEARCH_FAILOVER"
    return bool(trigger_sha) and scan.get("source_head_sha") == trigger_sha, "MATCHED_DISCOVERY_TRIGGER"


def load(path):
    try:
        return json.loads(pathlib.Path(path).read_text())
    except (OSError, ValueError):
        return {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-owner", action="store_true")
    parser.add_argument("--stage-publication", action="store_true")
    parser.add_argument("--push-owned", action="store_true")
    parser.add_argument("--runtime-gate", action="store_true")
    parser.add_argument("--fail-owned", action="store_true")
    args = parser.parse_args()
    if args.fail_owned:
        recorded = fail_owned(ClaimStore(), os.environ["EXPECTED"], os.environ.get("HUNTER_RESEARCH_OWNER"),
                              "GITHUB_RESEARCH_JOB_FAILED_SEE_ACTIONS_LOG")
        print("HUNTER_RESEARCH_FAILED_CLAIM_RECORDED" if recorded else "HUNTER_RESEARCH_FAILURE_NO_OWNED_CLAIM")
        return
    if args.runtime_gate:
        from scripts.hunter_runtime_gate import should_run
        # Once ownership exists, bind must consult it even if an older successful
        # health receipt is aligned (e.g. a subsequent forced run failed).
        run = pathlib.Path(CLAIM).exists() or should_run(
            "research", load(".github/hunter-runtime.json"),
            load("research/results/hunter-runtime-research-health.json"),
            dt.datetime.now(dt.timezone.utc), load(SCAN))
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write("run=" + str(run).lower() + "\n")
        print("HUNTER_RESEARCH_RUNTIME_GATE", "CHECK_SHARED_OWNERSHIP" if run else "VULTR_RECENT_SUCCESS_SKIP")
        return
    if args.check_owner or args.stage_publication or args.push_owned:
        function = (push_owned if args.push_owned else stage_publication if args.stage_publication else verify_owner)
        function(ClaimStore(), os.environ["EXPECTED"], os.environ.get("HUNTER_RESEARCH_OWNER"))
        print("HUNTER_RESEARCH_OWNERSHIP_OK", flush=True)
        return
    scan = load("research/results/hunter-cex-universe-run.json")
    accepted, reason = admit(
        scan, {} if pathlib.Path(CLAIM).exists() else load("research/results/hunter-health-and-queue.json"),
        os.environ.get("TRIGGER_EVENT"), os.environ.get("TRIGGER_SHA"),
        os.environ.get("FORCE", "false").lower() == "true",
        load(".github/hunter-runtime.json").get("primary_jobs", {}).get("research") is True)
    owner = ""
    if accepted:
        claim, reason = acquire(ClaimStore(), scan["generation_id"],
                                force=os.environ.get("FORCE", "false").lower() == "true")
        accepted = claim is not None
        if claim:
            owner = claim["owner"]
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write("should_run=" + str(accepted).lower() + "\n")
        output.write("generation=" + scan["generation_id"] + "\n")
        output.write("owner=" + owner + "\n")
    print("HUNTER_GENERATION_ADMITTED" if accepted else "HUNTER_TRIGGER_SKIPPED",
          scan["generation_id"], reason)


if __name__ == "__main__":
    main()
