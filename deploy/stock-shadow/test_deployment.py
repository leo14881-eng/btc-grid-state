"""Offline checks for stock-only templates/launcher. Never contacts a live host."""
import configparser
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
LAUNCHER = HERE / "run-job.sh"
CALENDARS = {
    "main": "*-*-* *:23:00 UTC",
    "monitor": "Mon..Fri *-*-* *:02/5:00 UTC",
    "replay": "Mon..Fri *-*-* 22:30:00 UTC",
}
STUB = '''import argparse, json, os, pathlib, subprocess, sys
p = argparse.ArgumentParser()
p.add_argument("job")
g = p.add_mutually_exclusive_group(required=True)
g.add_argument("--preview", action="store_true")
g.add_argument("--publish", action="store_true")
p.add_argument("--runtime-dir", required=True)
a = p.parse_args()
git = lambda *v: subprocess.check_output(["git", *v], text=True).strip()
data = {"job": a.job, "mode": "preview" if a.preview else "publish",
        "cwd": os.getcwd(), "source": git("rev-parse", "HEAD"),
        "clean_start": not git("status", "--porcelain"),
        "detached": subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], capture_output=True).returncode != 0,
        "tmpdir": os.environ.get("TMPDIR"), "bytecode": os.environ.get("PYTHONDONTWRITEBYTECODE"),
        "pythonpath": os.environ.get("PYTHONPATH"), "pythonhome": os.environ.get("PYTHONHOME")}
pathlib.Path(a.runtime_dir, "stub-run.json").write_text(json.dumps(data))
pathlib.Path("preview-output.json").write_text('{"shadow_only": true}')
sys.exit(int(os.environ.get("TEST_STOCK_FAILURE", "0")))
'''


def git(directory, *args):
    return subprocess.check_output(["git", "-C", str(directory), *args], text=True,
                                   stderr=subprocess.PIPE).strip()


@pytest.fixture
def fixture_repo(tmp_path):
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    install = tmp_path / "install"
    state = tmp_path / "state"
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(origin)],
                   check=True, capture_output=True)
    subprocess.run(["git", "init", "--initial-branch=main", str(seed)],
                   check=True, capture_output=True)
    git(seed, "config", "user.name", "Offline Stock Test")
    git(seed, "config", "user.email", "offline@example.invalid")
    module = seed / "research/stock_shadow/server/runner.py"
    module.parent.mkdir(parents=True)
    module.write_text(STUB)
    git(seed, "add", ".")
    git(seed, "commit", "-m", "Offline runner fixture")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "origin", "main")
    install.mkdir()
    subprocess.run(["git", "clone", "--mirror", str(origin), str(install / "source")],
                   check=True, capture_output=True)
    env = os.environ.copy()
    env.update(STOCK_SHADOW_INSTALL_ROOT=str(install), STOCK_SHADOW_STATE_ROOT=str(state),
               STOCK_SHADOW_PYTHON=sys.executable, PYTHONDONTWRITEBYTECODE="1")
    return {"origin": origin, "seed": seed, "install": install, "state": state, "env": env}


def launch(f, job="main", mode="--preview", **extra_env):
    return subprocess.run(["bash", str(LAUNCHER), job, mode],
                          env={**f["env"], **extra_env}, text=True,
                          capture_output=True, timeout=20)


def receipts(f):
    return [dict(line.split("=", 1) for line in p.read_text().splitlines())
            for p in (f["state"] / "runtime/launches").glob("*/launcher-receipt.txt")]


def test_preview_fetches_latest_clean_detached_and_cannot_push(fixture_repo):
    f = fixture_repo
    # Mirror is deliberately stale; the invocation must fetch the new main.
    (f["seed"] / "latest-marker").write_text("latest main\n")
    git(f["seed"], "add", ".")
    git(f["seed"], "commit", "-m", "Advance upstream")
    git(f["seed"], "push", "origin", "main")
    expected = git(f["origin"], "rev-parse", "main")
    result = launch(f, PYTHONHOME="/nonexistent/forbidden-python-home",
                    PYTHONPATH=str(HERE.parents[1]))
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads((f["state"] / "runtime/stub-run.json").read_text())
    checkout = Path(record["cwd"])
    assert record["source"] == expected
    assert record["clean_start"] and record["detached"]
    assert record["mode"] == "preview" and record["bytecode"] == "1"
    assert record["pythonpath"] is None and record["pythonhome"] is None
    assert Path(record["tmpdir"]).is_relative_to(f["state"])
    assert checkout.is_relative_to(f["state"] / "runtime/preview-checkouts")
    assert (checkout / "preview-output.json").is_file()
    assert git(checkout, "remote", "get-url", "--push", "origin") == "disabled://stock-shadow-preview"
    assert git(checkout, "config", "gc.auto") == "0"
    assert git(checkout, "config", "maintenance.auto") == "false"
    # No policy is persisted on the source mirror or the user's global config.
    for key in ('gc.auto', 'maintenance.auto'):
        unchanged = subprocess.run(['git', '--git-dir=' + str(f['install'] / 'source'),
                                    'config', '--local', '--get', key], capture_output=True)
        assert unchanged.returncode == 1
    # Even an explicit local push URL is rejected by the installed preview hook.
    rejected = subprocess.run(["git", "-C", str(checkout), "push", str(f["origin"]), "HEAD:refs/heads/preview-test"],
                              capture_output=True, text=True)
    assert rejected.returncode != 0
    assert "Stock preview pushes are disabled" in rejected.stderr
    assert git(f["origin"], "rev-parse", "main") == expected
    assert receipts(f)[0]["checkout_retained"] == "true"


@pytest.mark.parametrize("job", ["main", "monitor", "replay"])
def test_publish_cleans_work_and_keeps_receipt(fixture_repo, job):
    f = fixture_repo
    result = launch(f, job, "--publish")
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads((f["state"] / "runtime/stub-run.json").read_text())
    assert record["mode"] == "publish" and record["job"] == job
    assert record["clean_start"] and record["detached"]
    assert not Path(record["cwd"]).exists()
    assert list((f["state"] / "work").iterdir()) == []
    receipt = receipts(f)[0]
    assert receipt["status"] == "completed" and receipt["exit_code"] == "0"
    assert receipt["checkout_retained"] == "false"


@pytest.mark.parametrize("mode,retained", [("--publish", False), ("--preview", True)])
def test_failures_remain_failed_and_logged(fixture_repo, mode, retained):
    f = fixture_repo
    result = launch(f, "monitor", mode, TEST_STOCK_FAILURE="7")
    assert result.returncode == 7
    receipt = receipts(f)[0]
    assert receipt["status"] == "failed" and receipt["exit_code"] == "7"
    assert Path(receipt["checkout"]).exists() is retained
    assert list((f["state"] / "runtime/launches").glob("*/launcher.log"))


def test_monitor_skips_busy_common_lock(fixture_repo):
    f = fixture_repo
    f["state"].mkdir()
    with (f["state"] / "writer.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = launch(f, "monitor", "--publish")
        assert result.returncode == 0
    assert not (f["state"] / "runtime/stub-run.json").exists()
    assert len(receipts(f)) == 1
    assert {r["status"] for r in receipts(f)} == {"skipped_lock_busy"}


@pytest.mark.parametrize("job,seconds", [("main", 600), ("replay", 1200)])
def test_main_replay_use_bounded_common_lock_and_fail_explicitly(fixture_repo, job, seconds):
    f = fixture_repo
    fakebin = f["state"].parent / "fakebin"
    fakebin.mkdir()
    fakeflock = fakebin / "flock"
    fakeflock.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$STOCK_SHADOW_STATE_ROOT/flock-args"\nexit 1\n')
    fakeflock.chmod(0o755)
    result = launch(f, job, "--publish", PATH=str(fakebin) + os.pathsep + f["env"]["PATH"])
    assert result.returncode == 75
    assert (f["state"] / "flock-args").read_text().strip() == f"-w {seconds} 9"
    assert (f["state"] / "writer.lock").is_file()
    assert receipts(f)[0]["status"] == "lock_wait_timeout"
    assert not (f["state"] / "runtime/stub-run.json").exists()


def test_explicit_mode_and_no_strategy_force(fixture_repo):
    for args in (["main"], ["main", "--force"], ["crypto", "--publish"],
                 ["monitor", "--publish", "--force"]):
        result = subprocess.run(["bash", str(LAUNCHER), *args], env=fixture_repo["env"],
                                capture_output=True, text=True)
        assert result.returncode == 2
    assert not fixture_repo["state"].exists()


def test_rejects_secret_bearing_remote_without_printing_it(fixture_repo):
    f = fixture_repo
    secret = "secret-fixture-token"
    git(f["install"] / "source", "remote", "set-url", "origin", f"https://user:{secret}@example.invalid/repo.git")
    result = launch(f)
    assert result.returncode == 2
    assert secret not in result.stdout + result.stderr
    assert receipts(f)[0]["status"] == "failed"


@pytest.mark.parametrize("job,calendar", CALENDARS.items())
def test_service_and_timer_contract(job, calendar):
    timer = configparser.ConfigParser(interpolation=None)
    timer.read(HERE / f"systemd/stock-shadow-{job}.timer")
    assert timer["Timer"]["OnCalendar"] == calendar
    assert timer["Timer"]["Persistent"] == "false"
    assert timer["Timer"]["RandomizedDelaySec"] == "0"
    assert timer["Timer"]["AccuracySec"] == "1s"
    assert timer["Timer"]["Unit"] == f"stock-shadow-{job}.service"
    service = configparser.ConfigParser(interpolation=None)
    service.read(HERE / f"systemd/stock-shadow-{job}.service")
    s = service["Service"]
    assert s["ExecStart"] == f"/opt/stock-shadow/bin/run-job.sh {job} --publish"
    assert s["TimeoutStartSec"] == {"main": "40min", "monitor": "10min", "replay": "45min"}[job]
    assert s["User"] == "stock-shadow" and s["Group"] == "stock-shadow"
    assert s["ReadWritePaths"] == "/opt/stock-shadow/source /var/lib/stock-shadow"
    assert s["NoNewPrivileges"] == "true" and s["ProtectSystem"] == "strict"
    assert s["KillMode"] == "control-group" and s["Restart"] == "no"
    assert "ExecStartPost" not in s


def test_preview_template_cannot_be_scheduled_or_publish():
    service = configparser.ConfigParser(interpolation=None)
    service.read(HERE / "systemd/stock-shadow-preview@.service")
    assert service["Service"]["ExecStart"] == "/opt/stock-shadow/bin/run-job.sh %i --preview"
    assert "Install" not in service
    assert service["Service"]["User"] == "stock-shadow"
    assert not list((HERE / "systemd").glob("stock-shadow-preview*.timer"))


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd analyzer unavailable")
@pytest.mark.parametrize("job,calendar", CALENDARS.items())
def test_systemd_accepts_utc_calendars(job, calendar):
    result = subprocess.run(["systemd-analyze", "calendar", "--iterations=3", calendar],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd analyzer unavailable")
def test_systemd_template_syntax_with_local_executable(tmp_path):
    # Validate directives without pretending the production paths/user are installed.
    # The real host must verify the unchanged installed files before activation.
    paths = []
    for template in (HERE / "systemd").glob("stock-shadow-*"):
        text = template.read_text()
        if template.suffix == ".service":
            text = "\n".join("ExecStart=/usr/bin/true" if line.startswith("ExecStart=") else line
                             for line in text.splitlines()) + "\n"
        target = tmp_path / template.name
        target.write_text(text)
        paths.append(str(target))
    # User mode keeps the syntax check entirely in this temporary test directory;
    # it is not a production system-manager or sandbox acceptance test.
    runtime = tmp_path / "xdg-runtime"
    runtime.mkdir(mode=0o700)
    result = subprocess.run(["systemd-analyze", "--user", "verify", "--man=no", *paths],
                            env={**os.environ, "XDG_RUNTIME_DIR": str(runtime)},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
