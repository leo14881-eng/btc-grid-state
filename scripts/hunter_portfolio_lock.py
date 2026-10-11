"""One host portfolio flock, with bounded blocking admission and diagnostic receipts.

The inode is never unlinked. Metadata is diagnostic, not proof of ownership: an
uncatchable process failure can leave an old receipt after the kernel releases
the lock. Blocking flock avoids the old 200ms polling/barging window; the kernel
does not promise FIFO ordering. Keep the 120s deadline and measure contention.
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
import signal
import time

LOCK_PATH = "/run/lock/hunter-position-monitor.lock"


def emit(event, **fields):
    print("HUNTER_PORTFOLIO_LOCK " + json.dumps(dict(
        event=event, at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
        **fields), sort_keys=True), flush=True)


def read_receipt(file):
    try:
        file.seek(0)
        row = json.loads(file.read(4096))
        # No arbitrary file contents, paths, command arguments or secrets in logs.
        return {k: row[k] for k in ("role", "pid", "state", "phase", "generation",
                "base_sha", "acquired_at_utc", "acquired_monotonic", "phase_monotonic")
                if k in row and isinstance(row[k], (str, int, float))}
    except (OSError, ValueError, TypeError):
        return {}


def write_receipt(file, row):
    file.seek(0)
    file.write(json.dumps(row, sort_keys=True) + "\n")
    file.truncate()
    file.flush()


@contextlib.contextmanager
def deadline(seconds, error="PORTFOLIO_LOCK_WAIT_TIMEOUT"):
    # Both callers run on their main thread, before starting any worker threads.
    # Do not silently replace another caller's timer.
    if seconds <= 0 or signal.getitimer(signal.ITIMER_REAL)[0]:
        raise RuntimeError("INVALID_PORTFOLIO_LOCK_DEADLINE")
    def expired(signum, frame):
        raise TimeoutError(error)
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class PortfolioLock:
    def __init__(self, role, generation="", base_sha="", path=LOCK_PATH, file=None, pid=None):
        self.file = file if file is not None else os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+")
        self.role, self.generation, self.base_sha = role, generation, base_sha
        self.pid = os.getpid() if pid is None else pid
        self.acquired = None
        self.wait_seconds = None

    def acquire(self, timeout=120):
        start = time.monotonic()
        observed = read_receipt(self.file)
        emit("wait", role=self.role, pid=self.pid, generation=self.generation,
             observed_receipt=observed, receipt_is_ownership_proof=False,
             timeout_seconds=timeout, admission="kernel_blocking_no_fifo_guarantee")
        try:
            with deadline(timeout):
                fcntl.flock(self.file, fcntl.LOCK_EX)
        except BaseException as exc:
            emit("wait_failed", role=self.role, pid=self.pid,
                 wait_seconds=round(time.monotonic()-start, 6),
                 error="PORTFOLIO_LOCK_WAIT_TIMEOUT" if isinstance(exc, TimeoutError) else type(exc).__name__,
                 observed_receipt=read_receipt(self.file), receipt_is_ownership_proof=False)
            self.file.close()
            if isinstance(exc, TimeoutError):
                raise RuntimeError("PORTFOLIO_LOCK_WAIT_TIMEOUT") from None
            raise
        self.acquired = time.monotonic()
        self.wait_seconds = self.acquired-start
        row = dict(role=self.role, pid=self.pid, state="HELD", phase="admitted",
                   generation=self.generation, base_sha=self.base_sha,
                   acquired_at_utc=dt.datetime.now(dt.timezone.utc).isoformat(),
                   acquired_monotonic=self.acquired, phase_monotonic=self.acquired)
        try:
            write_receipt(self.file, row)
            emit("acquired", **row, wait_seconds=round(self.wait_seconds, 6))
        except BaseException:
            self.file.close()
            raise
        return self

    def phase(self, name, generation=None, base_sha=None):
        row = read_receipt(self.file)
        now = time.monotonic()
        emit("phase", role=self.role, pid=self.pid, previous_phase=row.get("phase"),
             phase=name, phase_seconds=round(now-row.get("phase_monotonic", now), 6),
             hold_seconds=round(now-self.acquired, 6))
        row.update(phase=name, phase_monotonic=now)
        if generation is not None: row["generation"] = generation
        if base_sha is not None: row["base_sha"] = base_sha
        write_receipt(self.file, row)

    def close(self, status="complete"):
        if self.file.closed: return
        try:
            if self.acquired is not None:
                self.phase("release")
                row = read_receipt(self.file)
                row["state"] = "RELEASED"
                write_receipt(self.file, row)
                emit("released", role=self.role, pid=self.pid, status=status,
                     hold_seconds=round(time.monotonic()-self.acquired, 6),
                     kernel_release_requires_all_inherited_fds_closed=True)
        finally:
            # Close, not LOCK_UN: an in-flight child inherits the same open file
            # description and must keep the lock if its supervisor dies.
            self.file.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=("acquire", "phase", "release"))
    p.add_argument("--fd", type=int, required=True)
    p.add_argument("--role", default="monitor")
    p.add_argument("--pid", type=int, default=os.getppid())
    p.add_argument("--name", default="")
    p.add_argument("--generation", default="")
    p.add_argument("--base-sha", default="")
    p.add_argument("--status", default="complete")
    a = p.parse_args()
    # Bash owns this descriptor. The acquisition survives this helper's exit.
    file = os.fdopen(os.dup(a.fd), "r+")
    lock = PortfolioLock(a.role, a.generation, a.base_sha, file=file, pid=a.pid)
    if a.command == "acquire":
        lock.acquire()
    else:
        row = read_receipt(file)
        if row.get("pid") != a.pid or row.get("state") != "HELD":
            raise RuntimeError("PORTFOLIO_LOCK_RECEIPT_MISMATCH")
        lock.acquired = row["acquired_monotonic"]
        if a.command == "phase":
            lock.phase(a.name, a.generation or None, a.base_sha or None)
        else:
            lock.close(a.status)


if __name__ == "__main__":
    main()
