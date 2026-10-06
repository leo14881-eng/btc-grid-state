import hashlib
import io
import os
import pathlib
import tempfile
import unittest
import urllib.error
import zipfile
from unittest.mock import Mock

from research.hunter_archive_transport import archive_bytes, TTL_SECONDS

URL = 'https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1d/BTCUSDT-1d-2025-01.zip'


class ArchiveCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z:
            z.writestr('data.csv', 'historical-observation\n')
        self.raw = buffer.getvalue()

    def tearDown(self):
        self.temp.cleanup()

    def request(self, fetch, url=URL):
        return archive_bytes(url, fetch, cache_dir=self.temp.name)

    def test_valid_archive_is_reused_byte_for_byte(self):
        fetch = Mock(return_value=self.raw)
        self.assertEqual(self.request(fetch), self.raw)
        self.assertEqual(self.request(fetch), self.raw)
        fetch.assert_called_once_with(URL, 30)

    def test_corrupt_and_expired_cache_refetched(self):
        fetch = Mock(return_value=self.raw)
        self.request(fetch)
        path = pathlib.Path(self.temp.name, hashlib.sha256(URL.encode()).hexdigest() + '.zip')
        path.write_bytes(b'corrupted')
        self.assertEqual(self.request(fetch), self.raw)
        os.utime(path, (0, 0))
        self.assertEqual(self.request(fetch), self.raw)
        self.assertEqual(fetch.call_count, 3)

    def test_verified_404_cached_but_other_http_errors_not_cached(self):
        fetch = Mock(side_effect=urllib.error.HTTPError(URL, 404, 'not found', None, None))
        for _ in range(2):
            with self.assertRaises(urllib.error.HTTPError):self.request(fetch)
        self.assertEqual(fetch.call_count, 1)
        other_url = URL.replace('2025-01', '2025-02')
        failure = Mock(side_effect=urllib.error.HTTPError(other_url, 503, 'outage', None, None))
        for _ in range(2):
            with self.assertRaises(urllib.error.HTTPError):self.request(failure, other_url)
        self.assertEqual(failure.call_count, 2)

    def test_current_or_unrelated_urls_not_cached(self):
        fetch = Mock(return_value=self.raw)
        url = 'https://data-api.binance.vision/api/v3/ticker/24hr'
        self.request(fetch, url); self.request(fetch, url)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(list(pathlib.Path(self.temp.name).iterdir()), [])

    def test_invalid_download_never_becomes_a_successful_cache(self):
        fetch = Mock(return_value=b'invalid archive')
        self.request(fetch); self.request(fetch)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(list(pathlib.Path(self.temp.name).iterdir()), [])
