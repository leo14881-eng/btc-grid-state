"""Frozen cache provenance, source isolation and historical confirmation; offline."""
import copy
import datetime as dt
import unittest

from research import hunter_signal_provenance as provenance


OBSERVED = dt.datetime(2026, 10, 11, 12, 10, tzinfo=dt.timezone.utc)
GENERATION = int(OBSERVED.timestamp()*1000)
EPOCH = '1'*32
OLD_EPOCH = '2'*32


def fresh(stamp, now):
    observed = dt.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    return 0 <= (now-observed).total_seconds() <= 900


def packet(symbol='ALTUSDT', interval='15m', count=25):
    period = {'1h': 3600000, '15m': 900000}[interval]
    terminal = GENERATION//period*period
    bars, transport = [], []
    for start in range(terminal-(count-1)*period, terminal+1, period):
        current = start == terminal
        end = start+period-1
        source = GENERATION-1000 if current else end+1
        bars.append(dict(open_ms=start, close_ms=end,
                         ohlcv_quote=[100., 102., 99., 101., 10., 1000.],
                         confirmed=not current))
        transport.append(dict(source='OFFICIAL_BYBIT_V5_SPOT_WS',
                              received_at_ms=source, source_time_ms=source,
                              confirm=not current, confirmation='WS_EXPLICIT',
                              session=EPOCH if current else OLD_EPOCH))
    return dict(schema=provenance.SCHEMA, venue='bybit', market='spot',
                symbol=symbol, interval=interval, source=provenance.CACHE_SOURCE,
                observed_at_utc=OBSERVED.isoformat(), bars=bars,
                content_hash=provenance.hash_value(bars), transport=transport,
                cache=dict(schema='bybit_stream_generation_v1', generation_end_ms=GENERATION,
                           roster_hash='2'*64, manifest_sha256='3'*64, epoch=EPOCH,
                           generation_sha256='4'*64))


def signal():
    return dict(source_provenance={
        'asset_1h': packet('ALTUSDT', '1h', 2),
        'asset_4h': packet('ALTUSDT', '1h', 5),
        'btc_1h': packet('BTCUSDT', '1h', 2),
        'btc_4h': packet('BTCUSDT', '1h', 5),
        'micro': packet(), 'btc_micro': packet('BTCUSDT')})


def rehash(value):
    value['content_hash'] = provenance.hash_value(value['bars'])


class CacheProvenanceTests(unittest.TestCase):
    def sources(self, value):
        return provenance.signal_sources(value, 'ALT', OBSERVED+dt.timedelta(seconds=1),
                                         OBSERVED-dt.timedelta(minutes=30), fresh, 'bybit')

    def test_complete_generation_preserves_existing_window_contract(self):
        value = signal()
        self.assertIsNone(provenance.validate_cache_packet(value['source_provenance']['micro'],
                                                          'ALTUSDT', '15m', 25))
        result = self.sources(value)
        self.assertEqual(set(result), set(value['source_provenance']))
        for key, result_packet in result.items():
            self.assertEqual(set(result_packet), {'window', 'hash', 'observed_at_utc'})
            self.assertEqual(result_packet['hash'], value['source_provenance'][key]['content_hash'])

    def test_actual_rest_and_ws_transport_can_share_one_generation(self):
        value = packet()
        for item in value['transport'][:-1]:
            item.update(source='OFFICIAL_BYBIT_V5_SPOT_REST', confirmation='REST_INFERRED')
        provenance.validate_cache_packet(value, 'ALTUSDT', '15m', 25)

    def test_old_rest_validation_stays_unchanged(self):
        cached = packet()
        rows = [[bar['open_ms'], *bar['ohlcv_quote']] for bar in cached['bars']]
        legacy = provenance.capture(rows, 'ALTUSDT', '15m', OBSERVED, 'bybit')
        self.assertEqual(legacy['source'], '/v5/market/kline')
        provenance.validate(legacy, 'ALTUSDT', '15m', 25, OBSERVED,
                            OBSERVED-dt.timedelta(minutes=30), fresh, 'bybit')
        legacy['bars'][-2]['confirmed'] = False
        rehash(legacy)
        with self.assertRaisesRegex(ValueError, 'SOURCE_WINDOW_CONFIRMATION_INVALID'):
            provenance.validate(legacy, 'ALTUSDT', '15m', 25, OBSERVED,
                                OBSERVED-dt.timedelta(minutes=30), fresh, 'bybit')

    def test_each_generation_binding_field_must_match_asset_and_btc(self):
        for field, other in [('generation_end_ms', GENERATION-1), ('roster_hash', '5'*64),
                             ('manifest_sha256', '5'*64), ('epoch', '5'*32),
                             ('generation_sha256', '5'*64)]:
            with self.subTest(field=field):
                value = signal()
                value['source_provenance']['btc_micro']['cache'][field] = other
                with self.assertRaisesRegex(ValueError, 'SOURCE_CACHE_GENERATION_MISMATCH'):
                    self.sources(value)

    def test_mixed_rest_cache_packets_are_rejected(self):
        value = signal()
        value['source_provenance']['btc_micro']['source'] = '/v5/market/kline'
        with self.assertRaisesRegex(ValueError, 'SOURCE_CACHE_MIXED_SOURCE'):
            self.sources(value)

    def test_cache_requires_btc_micro_even_when_old_rest_did_not(self):
        value = signal()
        del value['source_provenance']['btc_micro']
        with self.assertRaisesRegex(ValueError, 'SOURCE_CACHE_BTC_MICRO_MISSING'):
            self.sources(value)

    def test_hourly_overlap_requires_identical_bars_and_transport(self):
        for field in ('bars', 'transport'):
            with self.subTest(field=field):
                value = signal()
                short = value['source_provenance']['asset_1h']
                if field == 'bars':
                    short['bars'][0]['ohlcv_quote'][3] = 100.5
                    rehash(short)
                else:
                    short['transport'][0]['received_at_ms'] += 1
                with self.assertRaisesRegex(ValueError, 'SOURCE_CACHE_LOOKBACK_MISMATCH'):
                    self.sources(value)

    def test_metadata_is_strict_and_generation_time_is_not_receipt_time(self):
        for field, bad in [('generation_end_ms', True), ('generation_end_ms', GENERATION+1),
                           ('schema', 'other'), ('roster_hash', 'G'*64), ('epoch', '1'*31),
                           ('manifest_sha256', None), ('generation_sha256', 'short')]:
            with self.subTest(field=field, bad=bad):
                value = packet()
                value['cache'][field] = bad
                with self.assertRaisesRegex(ValueError, 'SOURCE_CACHE_GENERATION_INVALID'):
                    provenance.validate_cache_packet(value, 'ALTUSDT', '15m', 25)
        value = packet()
        value['cache']['extra'] = 'unreviewed'
        with self.assertRaisesRegex(ValueError, 'SOURCE_CACHE_GENERATION_INVALID'):
            provenance.validate_cache_packet(value, 'ALTUSDT', '15m', 25)

    def test_transport_shape_identity_and_future_observations_fail_closed(self):
        for field, bad in [('source', '/v5/market/kline'), ('source_time_ms', GENERATION+1),
                           ('received_at_ms', GENERATION+1), ('received_at_ms', True),
                           ('confirm', 0), ('session', 'bad')]:
            with self.subTest(field=field):
                value = packet()
                value['transport'][-1][field] = bad
                with self.assertRaisesRegex(ValueError, 'SOURCE_CACHE_TRANSPORT_INVALID'):
                    provenance.validate_cache_packet(value, 'ALTUSDT', '15m', 25)
        value = packet()
        value['transport'].pop()
        with self.assertRaisesRegex(ValueError, 'SOURCE_CACHE_TRANSPORT_INCOMPLETE'):
            provenance.validate_cache_packet(value, 'ALTUSDT', '15m', 25)

    def test_current_bar_requires_fresh_source_and_current_session(self):
        for changes in ({'source_time_ms': GENERATION-90001},
                        {'source_time_ms': GENERATION-90001, 'received_at_ms': GENERATION-90001},
                        {'session': OLD_EPOCH}):
            with self.subTest(changes=changes):
                value = packet()
                value['transport'][-1].update(changes)
                with self.assertRaisesRegex(ValueError, 'SOURCE_CACHE_CURRENT_WINDOW_NOT_FRESH'):
                    provenance.validate_cache_packet(value, 'ALTUSDT', '15m', 25)

    def test_ws_confirmation_must_be_received_after_its_close(self):
        value = packet()
        value['bars'][-1]['confirmed'] = True
        value['transport'][-1]['confirm'] = True
        rehash(value)
        with self.assertRaisesRegex(ValueError, 'SOURCE_CACHE_CONFIRMATION_INVALID'):
            provenance.validate_cache_packet(value, 'ALTUSDT', '15m', 25)

    def test_rest_confirmation_is_inferred_from_actual_source_time(self):
        value = packet()
        bar, item = value['bars'][-2], value['transport'][-2]
        item.update(source='OFFICIAL_BYBIT_V5_SPOT_REST', confirmation='REST_INFERRED',
                    source_time_ms=bar['close_ms'], received_at_ms=bar['close_ms'])
        with self.assertRaisesRegex(ValueError, 'SOURCE_CACHE_CONFIRMATION_INVALID'):
            provenance.validate_cache_packet(value, 'ALTUSDT', '15m', 25)

    def test_source_and_confirmation_kind_cannot_be_relabelled(self):
        value = packet()
        value['transport'][0]['confirmation'] = 'REST_INFERRED'
        with self.assertRaisesRegex(ValueError, 'SOURCE_CACHE_CONFIRMATION_INVALID'):
            provenance.validate_cache_packet(value, 'ALTUSDT', '15m', 25)

    def test_internal_validation_rejects_bad_window_hash_and_market_identity(self):
        for field, bad, code in [('content_hash', '0'*64, 'SOURCE_WINDOW_HASH_MISMATCH'),
                                 ('symbol', 'BTCUSDT', 'SOURCE_WINDOW_IDENTITY_UNKNOWN'),
                                 ('venue', 'binance', 'SOURCE_WINDOW_IDENTITY_UNKNOWN')]:
            with self.subTest(field=field):
                value = packet()
                value[field] = bad
                with self.assertRaisesRegex(ValueError, code):
                    provenance.validate_cache_packet(value, 'ALTUSDT', '15m', 25)

    def test_later_consumer_time_does_not_confirm_an_unconfirmed_closed_bar(self):
        value = signal()
        quarter = value['source_provenance']['micro']
        quarter['bars'][-2]['confirmed'] = False
        quarter['transport'][-2]['confirm'] = False
        rehash(quarter)
        original = copy.deepcopy(value)
        with self.assertRaisesRegex(ValueError, 'SOURCE_CACHE_CLOSED_BAR_UNCONFIRMED'):
            self.sources(value)
        with self.assertRaisesRegex(ValueError, 'SOURCE_CACHE_CLOSED_BAR_UNCONFIRMED'):
            provenance.reconstruct_trend(value, 'ALT', OBSERVED-dt.timedelta(minutes=30))
        self.assertEqual(value, original)

    def test_complete_confirmed_history_reconstructs_without_using_forming_bar(self):
        value = signal()
        self.sources(value)
        expected = provenance.reconstruct_trend(value, 'ALT', OBSERVED-dt.timedelta(minutes=30))
        forming = value['source_provenance']['micro']['bars'][-1]
        forming['ohlcv_quote'] = [100., 2000., 1., 1000., 100000., 1000000.]
        rehash(value['source_provenance']['micro'])
        self.sources(value)
        actual = provenance.reconstruct_trend(value, 'ALT', OBSERVED-dt.timedelta(minutes=30))
        self.assertEqual(actual['price'], expected['price'])
        self.assertEqual(actual['signal'], expected['signal'])
        self.assertEqual(actual['market_at_utc'], '2026-10-11T11:59:59.999000+00:00')

    def test_reconstruction_checks_transport_even_without_prior_validation(self):
        value = signal()
        value['source_provenance']['micro']['transport'][-2]['confirm'] = False
        with self.assertRaisesRegex(ValueError, 'SOURCE_CACHE_TRANSPORT_INVALID'):
            provenance.reconstruct_trend(value, 'ALT', OBSERVED-dt.timedelta(minutes=30))


if __name__ == '__main__':
    unittest.main()
