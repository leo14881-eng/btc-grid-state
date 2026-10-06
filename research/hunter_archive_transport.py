"""Public archive transport cache; contains no portfolio or decision state."""
import hashlib
import io
import json
import os
import pathlib
import tempfile
import time
import urllib.error
import zipfile

TTL_SECONDS = 86400


def valid_zip(raw):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            return bool(archive.namelist()) and archive.testzip() is None
    except (ValueError, OSError, zipfile.BadZipFile):
        return False


def atomic(path, raw):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as target:
        target.write(raw)
        temporary = target.name
    os.replace(temporary, path)


def archive_bytes(url, fetch, timeout=30, cache_dir=None):
    directory = cache_dir or os.environ.get('HUNTER_ARCHIVE_CACHE_DIR')
    if not directory or not url.startswith('https://data.binance.vision/data/spot/monthly/klines/'):
        return fetch(url, timeout)
    root = pathlib.Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode()).hexdigest()
    data, absent = root / (key + '.zip'), root / (key + '.404.json')
    now = time.time()
    if data.exists() and 0 <= now - data.stat().st_mtime < TTL_SECONDS:
        raw = data.read_bytes()
        if valid_zip(raw):
            return raw
    if absent.exists():
        try:
            metadata = json.loads(absent.read_text())
            if metadata['url'] == url and 0 <= now - metadata['checked_at'] < TTL_SECONDS:
                raise urllib.error.HTTPError(url, 404, 'Cached official archive 404', None, None)
        except (ValueError, KeyError, TypeError):
            pass
    try:
        raw = fetch(url, timeout)
    except urllib.error.HTTPError as exc:
        # Only an explicit official 404 is cached; throttles and outages retry.
        if exc.code == 404:
            atomic(absent, json.dumps({'url': url, 'checked_at': now}).encode())
        raise
    if valid_zip(raw):
        atomic(data, raw)
        absent.unlink(missing_ok=True)
    return raw
