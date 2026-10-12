"""Opt-in receipt observations. No mode in this module skips production tests.

The trust boundary is the current OS account and a separately provisioned key.
Receipts authenticate a small, reviewed, isolated test closure, never live state.
Unsupported or uncertain inputs fail back to the unchanged full validation.
"""
import contextlib
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys
import sysconfig
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "scripts/hunter_validation_manifest.json"
SCHEMA = "hunter_validation_receipt_v1"
TTL_NS = 24 * 3600 * 1_000_000_000
LIMIT = 4096
SLOTS = 16
CHILD_ENV = {"TZ": "UTC", "LC_ALL": "C.UTF-8"}
REVIEWED_FILES = {"tests/test_hunter_execution_cost.py", "research/hunter_execution_cost.py",
                  "tests/test_hunter_v1_coverage.py", "research/hunter_v1_coverage.py"}
MODULES = ["tests.test_hunter_execution_cost", "tests.test_hunter_v1_coverage"]


class Unavailable(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def file_digest(path):
    if path.is_symlink() or not path.is_file():
        raise Unavailable("INPUT_NOT_REGULAR")
    with path.open("rb") as stream:
        h = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
        return h.hexdigest()


def runtime_identity():
    # -I -S excludes user site, installed packages and PYTHON* injection. Hash
    # the actual standard library, interpreter and loaded native libraries too.
    if sys.platform != "linux" or platform.python_implementation() != "CPython":
        raise Unavailable("UNSUPPORTED_RUNTIME")
    lib = Path(sysconfig.get_path("stdlib"))
    paths = {Path(sys.executable).resolve()}
    for base, dirs, files in os.walk(lib, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in ("site-packages", "__pycache__"))
        if any((Path(base) / d).is_symlink() for d in dirs):
            raise Unavailable("RUNTIME_SYMLINK")
        paths.update(Path(base) / f for f in files if f.endswith((".py", ".so", ".zip")))
    for line in Path("/proc/self/maps").read_text().splitlines():
        parts = line.split()
        if len(parts) >= 6 and parts[-1].startswith("/") and ".so" in parts[-1]:
            paths.add(Path(parts[-1]).resolve())
    for value in sys.path:
        if value.endswith(".zip") and Path(value).exists(): paths.add(Path(value))
    if sum(p.stat().st_size for p in paths) > 256 * 1024 * 1024:
        raise Unavailable("RUNTIME_TOO_LARGE")
    return dict(python=sys.version, implementation=sys.implementation.cache_tag,
                machine=platform.machine(), kernel=platform.release(), libc=platform.libc_ver(),
                files={str(p): file_digest(p) for p in sorted(paths)},
                command_flags=["-I", "-S", "-B"], environment=CHILD_ENV)


def fingerprint(root=ROOT):
    file_digest(root / MANIFEST)  # Reject missing, symlink or nonregular manifest.
    manifest = json.loads((root / MANIFEST).read_text())
    if (not isinstance(manifest, dict) or manifest.get("schema") != "hunter_reviewed_pure_tests_v1" or
            manifest.get("third_party_dependencies") != [] or manifest.get("modules") != MODULES or
            manifest.get("test_count") != 7 or not isinstance(manifest.get("reviewed_files"), dict) or
            set(manifest["reviewed_files"]) != REVIEWED_FILES):
        raise Unavailable("INCOMPLETE_DEPENDENCIES")
    reviewed = manifest["reviewed_files"]
    files = {}
    for name, expected in reviewed.items():
        if Path(name).is_absolute() or ".." in Path(name).parts:
            raise Unavailable("INVALID_INPUT_PATH")
        p = root / name
        if any(parent.is_symlink() for parent in [p, *p.parents]):
            raise Unavailable("INPUT_SYMLINK")
        actual = file_digest(p)
        if actual != expected: raise Unavailable("REQUIRES_DEPENDENCY_REVIEW")
        files[name] = actual
    # A package initializer added later could introduce unreviewed imports.
    for package in ("research", "tests", "scripts"):
        if (root / package / "__init__.py").exists():
            raise Unavailable("UNREVIEWED_PACKAGE_INITIALIZER")
    # Fixtures are inline today. Hash the fixture tree conservatively so even a
    # newly introduced fixture invalidates old receipts; never hash live ledgers.
    for base, dirs, names in os.walk(root / "tests/fixtures", followlinks=False):
        if any((Path(base) / d).is_symlink() for d in dirs): raise Unavailable("FIXTURE_SYMLINK")
        for name in sorted(names):
            p = Path(base) / name
            files[p.relative_to(root).as_posix()] = file_digest(p)
    files[MANIFEST] = file_digest(root / MANIFEST)
    files["scripts/hunter_validation_receipt.py"] = file_digest(root / "scripts/hunter_validation_receipt.py")
    return digest(canonical(dict(schema=SCHEMA, files=files, runtime=runtime_identity())))


@contextlib.contextmanager
def secure_directory(path):
    """Open each component without following links; never repair permissions."""
    if sys.platform != "linux": raise Unavailable("UNSUPPORTED_PERMISSIONS")
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts: raise Unavailable("ABSOLUTE_PATH_REQUIRED")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
            info = os.fstat(fd)
            sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if info.st_uid not in (0, os.getuid()) or (info.st_mode & 0o022 and not sticky_root):
                raise Unavailable("UNSAFE_PARENT")
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise Unavailable("PRIVATE_DIRECTORY_REQUIRED")
        yield fd
    finally:
        os.close(fd)


def private_read(fd, name, limit=LIMIT):
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        info = os.fstat(handle)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > limit):
            raise Unavailable("UNSAFE_FILE")
        raw = os.read(handle, limit + 1)
        if len(raw) > limit: raise Unavailable("OVERSIZE_FILE")
        return raw
    finally:
        os.close(handle)


def load_key(path):
    path = Path(path)
    with secure_directory(path.parent) as fd:
        key = private_read(fd, path.name, 32)
    if len(key) != 32: raise Unavailable("INVALID_KEY")
    return key


class Store:
    def __init__(self, fd, key):
        self.fd, self.key = fd, key

    def sign(self, doc):
        return canonical(dict(payload=doc, mac=hmac.new(self.key, canonical(doc), hashlib.sha256).hexdigest()))

    def read(self, name):
        doc = json.loads(private_read(self.fd, name))
        expected = hmac.new(self.key, canonical(doc["payload"]), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, doc["mac"]): raise Unavailable("UNTRUSTED_RECEIPT")
        return doc["payload"]

    def owned(self):
        return self.read("owner.json") == {"schema": SCHEMA, "purpose": "bounded_validation_observations"}

    def slot(self, fp):
        if not re.fullmatch("[0-9a-f]{64}", fp): raise Unavailable("INVALID_FINGERPRINT")
        return "receipt-%02d.json" % (int(fp[:2], 16) % SLOTS)

    def lookup(self, fp, now=None):
        if not self.owned(): return False
        doc = self.read(self.slot(fp))
        now = time.monotonic_ns() if now is None else now
        return (set(doc) == {"schema", "fingerprint", "boot", "issued_ns", "tests", "success"} and
                doc["schema"] == SCHEMA and doc["fingerprint"] == fp and
                doc["boot"] == Path("/proc/sys/kernel/random/boot_id").read_text().strip() and
                type(doc["issued_ns"]) is int and 0 <= now - doc["issued_ns"] <= TTL_NS and
                doc["tests"] == 7 and doc["success"] is True)

    def write_success(self, fp):
        # Caller holds the nonblocking OS lock across validation and publication.
        # Fixed slots bound storage without scanning/deleting unrelated files.
        name = self.slot(fp)
        try:
            old = self.read(name)
            if old.get("schema") != SCHEMA: raise Unavailable("NOT_OWNED")
        except FileNotFoundError:
            pass
        doc = dict(schema=SCHEMA, fingerprint=fp, tests=7, success=True,
                   boot=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                   issued_ns=time.monotonic_ns())
        self.atomic_write(name, self.sign(doc))

    def atomic_write(self, name, raw):
        if len(raw) > LIMIT: raise Unavailable("OVERSIZE_RECEIPT")
        # O_EXCL: an interrupted write leaves .pending and makes subsequent
        # observations miss. We never delete a possibly user-owned orphan.
        fd = os.open(".pending", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        os.replace(".pending", name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
        os.fsync(self.fd)


@contextlib.contextmanager
def open_store(directory, key_path, writable=False):
    import fcntl
    key = load_key(key_path)
    with secure_directory(directory) as fd:
        store = Store(fd, key)
        names = os.listdir(fd)
        if len(names) > SLOTS + 2 or ".pending" in names: raise Unavailable("STORE_INCOMPLETE_OR_FULL")
        # No adoption of existing arbitrary directory contents.
        if not names and writable:
            store.atomic_write("owner.json", store.sign(dict(schema=SCHEMA, purpose="bounded_validation_observations")))
        if not store.owned(): raise Unavailable("NOT_OWNED")
        allowed = {"owner.json", ".lock", *("receipt-%02d.json" % i for i in range(SLOTS))}
        if set(names) - allowed: raise Unavailable("FOREIGN_FILES")
        lock = os.open(".lock", os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if writable else 0), 0o600, dir_fd=fd)
        try:
            private_read(fd, ".lock", 0)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield store
        finally:
            os.close(lock)


def run_candidate(root=ROOT):
    result = subprocess.run([sys.executable, "-I", "-S", "-B", str(root / "scripts/hunter_validation_receipt.py"), "--child"],
                            cwd=root, env=CHILD_ENV, capture_output=True, timeout=30)
    # Do not log subprocess text, which could include input data or paths.
    return result.returncode == 0 and result.stdout.strip() == b'HUNTER_PURE_TESTS_OK 7'


def observe(env, root=ROOT):
    mode = env.get("HUNTER_VALIDATION_RECEIPTS", "off")
    row = dict(mode=mode if mode in ("off", "read-only", "compare") else "unsupported",
               hit=False, tests_skipped=0, production_health_evidence=False)
    if mode == "off": return row
    started = time.monotonic()
    try:
        if mode not in ("read-only", "compare"): raise Unavailable("UNSUPPORTED_MODE")
        fp = fingerprint(root)
        with open_store(env["HUNTER_VALIDATION_RECEIPT_DIR"], env["HUNTER_VALIDATION_RECEIPT_KEY"], mode == "compare") as store:
            try: row["hit"] = store.lookup(fp)
            except (OSError, ValueError, KeyError, TypeError, Unavailable): row["hit"] = False
            if mode == "compare":
                try:
                    success = run_candidate(root)
                except subprocess.SubprocessError:
                    success = False
                if not success:
                    # Do not leave a successful receipt usable after an actual
                    # test failure. A private tombstone blocks further use.
                    try:
                        store.atomic_write("owner.json", store.sign(dict(schema=SCHEMA, purpose="failed_validation")))
                    finally:
                        raise RuntimeError("HUNTER_RECEIPT_COMPARISON_FAILED")
                if fingerprint(root) != fp: raise Unavailable("INPUTS_CHANGED_DURING_VALIDATION")
                store.write_success(fp)
            row["status"] = "OBSERVED_ONLY"
    except (OSError, ValueError, KeyError, TypeError, Unavailable, subprocess.SubprocessError):
        row.update(hit=False, status="FALLBACK_FULL_VALIDATION")
    finally:
        row["seconds"] = round(time.monotonic() - started, 6)
    return row


def child():
    # No site initialization, environment-based imports, package initializers or
    # editable third-party dependencies. The reviewed files are the full closure.
    sys.path.insert(0, str(ROOT))
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name) for name in MODULES)
    result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    if result.wasSuccessful() and result.testsRun == 7 and not result.skipped:
        print("HUNTER_PURE_TESTS_OK 7")
        return 0
    return 1


if __name__ == "__main__":
    if sys.argv[1:] != ["--child"]: raise SystemExit("OBSERVATION_ONLY_NO_SKIP_MODE")
    raise SystemExit(child())
