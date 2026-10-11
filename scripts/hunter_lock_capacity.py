#!/usr/bin/env python3
"""Offline, disposable Git publication capacity model; never a strategy replay."""
import argparse
import contextlib
import datetime as dt
import hashlib
import io
import json
import multiprocessing
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import traceback

if not __package__:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts import hunter_monitor_persist as publisher

HISTORY_KEYS = ("closed_positions", "events", "decisions")
LANES = ("hunter-shadow", "hunter-shadow-v2")


def digest(raw):
    return hashlib.sha256(raw if isinstance(raw, bytes) else raw.encode("utf-8")).hexdigest()


def error_detail(exc):
    detail = str(exc)
    if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        detail += "\n" + stderr.strip()
    return detail


def git_at(directory, *args, check=True, text=True, input=None):
    # No hooks, credential helpers, user URL rewrites or network protocols. Only
    # local file transport is needed by this harness, including subprocess Git.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0")
    command = ["git", "-c", "protocol.allow=never", "-c", "protocol.file.allow=always",
               "-c", "core.hooksPath=" + os.devnull, "-c", "core.autocrlf=false",
               "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args]
    return subprocess.run(command, cwd=directory, env=env, check=check,
                          capture_output=True, text=text, encoding="utf-8" if text else None,
                          input=input)


def parse(raw):
    return {name: json.loads(raw[name]) for name in publisher.NAMES}


def serialize(docs):
    return {name: json.dumps(docs[name], ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            for name in publisher.NAMES}


def validate_shadow(raw):
    docs = parse(raw)
    for doc in docs.values():
        if "real_trading_enabled" in doc:
            publisher.require(doc["real_trading_enabled"] is False, "CAPACITY_REAL_TRADING_FORBIDDEN")
    generation = docs["hunter-scheduler-health.json"]["last_successful_monitor_generation_id"]
    publisher.validate(raw, generation)
    return generation


def amplify(raw, scale):
    """Duplicate historical payload only. This is not valid trading history.

    Original records remain an identical prefix. Open positions, policy, capital
    and individual history records are never rewritten or enlarged. The normal
    publication validator checks counts and exposure, not history uniqueness.
    """
    publisher.require(type(scale) is int and scale >= 1, "INVALID_CAPACITY_SCALE")
    validate_shadow(raw)
    if scale == 1:
        return dict(raw)
    docs = parse(raw)
    for prefix in LANES:
        portfolio = docs[prefix + "-portfolio.json"]
        for key in HISTORY_KEYS:
            if key in portfolio:
                publisher.require(isinstance(portfolio[key], list), "INVALID_HISTORY_LIST " + key)
                portfolio[key] = portfolio[key] * scale
        docs[prefix + "-summary.json"]["closed_positions"] = len(portfolio["closed_positions"])
    expanded = serialize(docs)
    validate_shadow(expanded)
    return expanded


def manifest(raw):
    docs = parse(raw)
    return dict(
        total_bytes=sum(len(value.encode("utf-8")) for value in raw.values()),
        files={name: dict(bytes=len(value.encode("utf-8")), sha256=digest(value))
               for name, value in raw.items()},
        lanes={prefix: {key: len(docs[prefix + "-portfolio.json"].get(key, []))
                        for key in ("open_positions", *HISTORY_KEYS)} for prefix in LANES})


def write_snapshot(directory, raw):
    for name, value in raw.items():
        target = pathlib.Path(directory, publisher.RESULTS, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value.encode("utf-8"))


def working_manifest(source):
    return {name: digest(path.read_bytes()) if path.is_file() else None
            for name in publisher.NAMES
            for path in [pathlib.Path(source, publisher.RESULTS, name)]}


def read_source(source, ref):
    sha = git_at(source, "rev-parse", "--verify", ref + "^{commit}").stdout.strip()
    raw = {name: git_at(source, "show", sha + ":" + publisher.RESULTS + name,
                        text=False).stdout.decode("utf-8") for name in publisher.NAMES}
    validate_shadow(raw)
    return raw, dict(ref=ref, commit_sha=sha, snapshot=manifest(raw))


def stable_payload(raw):
    """All fields except the explicitly simulated generation/timestamps."""
    docs = parse(raw)
    health = docs["hunter-scheduler-health.json"]
    for key in ("current_generation_id", "last_successful_monitor_generation_id"):
        health.pop(key, None)
    for prefix in LANES:
        docs[prefix + "-portfolio.json"].pop("updated_at_utc", None)
        docs[prefix + "-summary.json"].pop("as_of_utc", None)
    return digest(json.dumps(docs, ensure_ascii=False, sort_keys=True))


def next_snapshot(docs, generation):
    # No strategy, price lookup, order creation or decision evaluation occurs.
    health = docs["hunter-scheduler-health.json"]
    health["current_generation_id"] = health["last_successful_monitor_generation_id"] = generation
    for prefix in LANES:
        docs[prefix + "-portfolio.json"]["updated_at_utc"] = generation
        docs[prefix + "-summary.json"]["as_of_utc"] = generation
    return docs


def _worker(root, index, role, barrier, output, lock_timeout):
    """One process per writer so cwd, flock deadline and publisher globals isolate."""
    from scripts.hunter_portfolio_lock import PortfolioLock
    root = pathlib.Path(root).resolve()
    writer = root / ("writer-" + str(index))
    origin = root / "origin.git"
    result = dict(role=role, writer_index=index, phase_seconds={}, git_seconds={}, git_calls={})
    log = io.StringIO()
    lock = None
    cycle_start = None
    status = "failed"
    original_git, original_readback = publisher.git, publisher.readback

    def measure(name, fn, *args, **kwargs):
        start = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            result["phase_seconds"][name] = result["phase_seconds"].get(name, 0) + time.perf_counter() - start

    def timed_git(*args, **kwargs):
        start = time.perf_counter()
        try:
            return git_at(writer, *args, **kwargs)
        finally:
            key = args[0]
            result["git_seconds"][key] = result["git_seconds"].get(key, 0) + time.perf_counter() - start
            result["git_calls"][key] = result["git_calls"].get(key, 0) + 1

    def readback(*args, **kwargs):
        return measure("readback_including_fetch_parse_validate", original_readback, *args, **kwargs)

    try:
        publisher.require(pathlib.Path(git_at(writer, "remote", "get-url", "origin").stdout.strip()).resolve()
                          == origin and origin.is_dir(), "CAPACITY_REQUIRES_TEMP_LOCAL_ORIGIN")
        os.chdir(writer)
        os.environ["GITHUB_ACTIONS"] = "true"  # Never publish a primary runtime proof.
        publisher.git, publisher.readback = timed_git, readback
        barrier.wait(timeout=60)
        cycle_start = time.perf_counter()
        with contextlib.redirect_stdout(log):
            lock = PortfolioLock(role, path=str(root / "portfolio.lock"))
            lock.acquire(timeout=lock_timeout)
            result["wait_seconds"] = lock.wait_seconds
            lock.phase("capacity_bind_latest")
            start = time.perf_counter()
            timed_git("fetch", "origin", "main")
            timed_git("reset", "--hard", "origin/main")
            base = timed_git("rev-parse", "HEAD").stdout.strip()
            result["phase_seconds"]["bind_latest"] = time.perf_counter() - start
            result["base_sha"] = base
            raw = measure("read_worktree", publisher.snapshot)
            docs = measure("parse", parse, raw)
            current = docs["hunter-scheduler-health.json"]["last_successful_monitor_generation_id"]
            measure("validate_latest_including_parse", publisher.validate, raw, current)
            result["bound_generation"] = current
            generation = dt.datetime.now(dt.timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
            lock.phase("capacity_serialize", generation=generation, base_sha=base)
            expected = measure("serialize", serialize, next_snapshot(docs, generation))
            measure("write_worktree", write_snapshot, writer, expected)
            lock.phase("capacity_publish")
            measure("persist_including_cas_commit_push_readback", publisher.persist, base, generation)
            result["published_sha"] = timed_git("rev-parse", "HEAD").stdout.strip()
            result["published_generation"] = generation
            result["history_and_capital_unchanged"] = stable_payload(raw) == stable_payload(expected)
            publisher.require(result["history_and_capital_unchanged"], "CAPACITY_UNEXPECTED_PAYLOAD_MUTATION")
            result["hold_seconds"] = time.monotonic() - lock.acquired
            lock.close()
            result["total_seconds"] = time.perf_counter() - cycle_start
            status = "success"
    except BaseException as exc:
        result.update(error=error_detail(exc), error_type=type(exc).__name__, traceback=traceback.format_exc())
    finally:
        if lock is not None and not lock.file.closed:
            with contextlib.redirect_stdout(log):
                if lock.acquired is not None:
                    result["hold_seconds"] = time.monotonic() - lock.acquired
                lock.close(status=status)
        if cycle_start is not None:
            result["total_seconds"] = time.perf_counter() - cycle_start
        publisher.git, publisher.readback = original_git, original_readback
        result["status"] = status
        result["audit_log"] = log.getvalue().splitlines()
        pathlib.Path(output).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_scale(raw, scale, iterations=1, workers=2, lock_timeout=120):
    start = time.perf_counter()
    preparation_start = time.perf_counter()
    expanded = amplify(raw, scale)
    result = dict(scale=scale, payload_model="original" if scale == 1 else "synthetic_duplicate_history",
                  snapshot=manifest(expanded), cycles=[])
    result["preparation_seconds"] = time.perf_counter() - preparation_start
    context = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="hunter-capacity-") as directory:
        root = pathlib.Path(directory)
        origin, seed = root / "origin.git", root / "seed"
        setup_start = time.perf_counter()
        git_at(root, "init", "--bare", "--initial-branch=main", str(origin))
        git_at(root, "clone", str(origin), str(seed))
        git_at(seed, "config", "user.name", "isolated-capacity")
        git_at(seed, "config", "user.email", "isolated-capacity@localhost")
        write_snapshot(seed, expanded)
        git_at(seed, "add", ".")
        git_at(seed, "commit", "-m", "isolated capacity baseline")
        git_at(seed, "push", "origin", "main")
        initial = git_at(seed, "rev-parse", "HEAD").stdout.strip()
        for index in range(workers):
            writer = root / ("writer-" + str(index))
            git_at(root, "clone", str(origin), str(writer))
            git_at(writer, "config", "user.name", "isolated-capacity")
            git_at(writer, "config", "user.email", "isolated-capacity@localhost")
        result["setup_seconds"] = time.perf_counter() - setup_start
        for iteration in range(iterations):
            barrier = context.Barrier(workers)
            processes = []
            for index in range(workers):
                output = root / ("result-%s-%s.json" % (iteration, index))
                process = context.Process(target=_worker, args=(str(root), index,
                    ("monitor", "research")[index % 2], barrier, str(output), lock_timeout))
                processes.append((process, output))
                process.start()
            # One shared deadline bounds the harness; it is not a performance SLA.
            deadline = time.monotonic() + 600 + workers * lock_timeout
            for process, output in processes:
                process.join(timeout=max(0, deadline - time.monotonic()))
                if process.is_alive():
                    process.terminate()
                    process.join()
                    row = dict(status="failed", error="CAPACITY_WORKER_DEADLINE", writer_index=process.pid)
                elif output.is_file():
                    row = json.loads(output.read_text(encoding="utf-8"))
                else:
                    row = dict(status="failed", error="CAPACITY_WORKER_NO_RESULT", exitcode=process.exitcode)
                row["iteration"] = iteration
                result["cycles"].append(row)
            if any(row["status"] != "success" for row in result["cycles"]):
                break
        final = {name: git_at(origin, "show", "main:" + publisher.RESULTS + name,
                             text=False).stdout.decode("utf-8") for name in publisher.NAMES}
        validate_shadow(final)
        result["history_and_capital_unchanged"] = stable_payload(expanded) == stable_payload(final)
        result["initial_sha"] = initial
        result["final_sha"] = git_at(origin, "rev-parse", "main").stdout.strip()
        result["published_commit_count"] = int(git_at(origin, "rev-list", "--count", initial + "..main").stdout)
        result["status"] = "success" if (all(row["status"] == "success" for row in result["cycles"])
            and result["history_and_capital_unchanged"]
            and result["published_commit_count"] == iterations * workers) else "failed"
    result["scenario_total_seconds"] = time.perf_counter() - start
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-repo", type=pathlib.Path, default=pathlib.Path.cwd())
    p.add_argument("--source-ref", default="HEAD")
    p.add_argument("--scales", type=int, nargs="+", default=[1, 2, 5])
    p.add_argument("--iterations", type=int, default=1)
    p.add_argument("--workers", type=int, choices=[1, 2], default=2)
    p.add_argument("--output", type=pathlib.Path, default=pathlib.Path("hunter-lock-capacity.json"))
    args = p.parse_args()
    if os.name != "posix":
        p.error("POSIX flock is required; run on the Linux CI runner")
    publisher.require(args.iterations >= 1 and all(x >= 1 for x in args.scales), "INVALID_CAPACITY_ARGUMENTS")
    source = args.source_repo.resolve()
    output = args.output.resolve()
    # Reports may not overwrite any tracked source file or any authoritative state.
    tracked = {source / name for name in git_at(source, "ls-files", "-z").stdout.split("\0") if name}
    publisher.require(output not in tracked and not output.is_relative_to(source / publisher.RESULTS),
                      "CAPACITY_OUTPUT_MUST_NOT_OVERWRITE_SOURCE")
    before = working_manifest(source)
    raw, metadata = read_source(source, args.source_ref)
    report = dict(schema="hunter_lock_capacity_v1", generated_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                  mode="ISOLATED_PUBLICATION_HARNESS", real_trading_enabled=False,
                  capital_authority="NONE_SHADOW_ONLY", source=metadata, scenarios=[],
                  limitations=["No strategy, market API, real order or complete Research/Monitor cycle is executed.",
                    "Roles name two contending publication processes, not full production entrypoints.",
                    "Scales above 1 duplicate history payload; they are not valid trade histories or strategy replays.",
                    "Publication validation checks counts, generation and exposure; it does not validate history uniqueness.",
                    "Phase and Git timings overlap: persist includes CAS, commit, push and readback.",
                    "Temporary local Git transport excludes production network, server load and remote history size.",
                    "Kernel flock does not guarantee FIFO fairness; this run is a contention sample, not a fairness proof."])
    try:
        for scale in args.scales:
            row = run_scale(raw, scale, args.iterations, args.workers)
            report["scenarios"].append(row)
            print("HUNTER_LOCK_CAPACITY " + json.dumps(dict(scale=scale, status=row["status"],
                bytes=row["snapshot"]["total_bytes"], seconds=row["scenario_total_seconds"])), flush=True)
        report["status"] = "success" if all(r["status"] == "success" for r in report["scenarios"]) else "failed"
    except Exception as exc:
        report.update(status="failed", error=error_detail(exc), error_type=type(exc).__name__)
    finally:
        report["source_worktree_before"] = before
        report["source_worktree_after"] = working_manifest(source)
        report["source_worktree_unchanged"] = before == report["source_worktree_after"]
        if not report["source_worktree_unchanged"]:
            report["status"] = "failed"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
