"""Real SQLite hot-path equivalence, not a replay of an entire old deployment.

The small oracle preserves ec3d49b's three changed methods. Everything else is
shared with the candidate; the separate pinned-module runner can call replay()
with the actual old Store class to compare both production modules instead.
No exchange, socket, subprocess, workflow or trading-ledger access is used.
"""
import copy
import json
from pathlib import Path
import re
import tempfile
import unittest
import uuid
from unittest.mock import patch

from research.hunter_binance_health_store import Store, payload_hash
from research.hunter_binance_stream_store import (
    FRESH_MS, HISTORY_MS, MAX_REVISIONS, LIMITS, STEPS, WS_SOURCE, topic,
    split_topic, valid_row)
from research.hunter_bybit_stream_store import Store as SharedStore

NOW = 1791858120000
EPOCH = '1' * 32
NAMES = sorted(topic('BTCUSDT', interval) for interval in LIMITS)
SCENARIOS = (
    'all_intervals', 'duplicate_conflict_old', 'same_topic_batch', 'invalid_envelopes',
    'attempted_unclose', 'post_write_rollback', 'unacked', 'stale_epoch',
    'closed_boundary', 'missing_close', 'daily_history_floor',
    'revision_pruning', 'legacy_default_state', 'legacy_batch_rollback',
    'missing_history', 'off_grid_anchor', 'floor_absent', 'floor_above_anchor',
    'floor_off_grid', 'floor_invalid', 'confirmed_storage_types',
    'start_real', 'start_text', 'start_blob', 'start_null')


def rows(interval, stamp=NOW, count=None):
    step = STEPS[interval]
    return [[(stamp//step-i)*step, '100', '103', '99', '101', '10',
             (stamp//step-i+1)*step-1, '1000', 7, '5', '500', '0']
            for i in reversed(range(count or LIMITS[interval]))]


def message(interval='15m', stamp=NOW+1, start=None, closed=False):
    row = rows(interval, stamp, 1)[0]
    if start is not None:
        row[0] = start
        row[6] = start+STEPS[interval]-1
    return dict(e='kline', E=stamp, s='BTCUSDT', k=dict(
        s='BTCUSDT', i=interval, x=closed,
        **dict(zip(('t', 'o', 'h', 'l', 'c', 'v', 'T', 'q', 'n', 'V', 'Q', 'B'), row))))


class Ec3dHotPathOracle(Store):
    """Old hot-path algorithm only; inherited non-hot-path code is not pinned."""
    _put = SharedStore._put

    def _ingest_ws(self, db, epoch, message, received):
        k = message['k']; name = topic(message['s'], k['i'])
        source = message['E']; step = STEPS[k['i']]
        if (message.get('e') != 'kline' or k.get('s') != message['s']
                or type(source) is not int or type(received) is not int
                or not 0 <= received-source <= FRESH_MS or type(k.get('x')) is not bool):
            raise ValueError('BINANCE_WS_ENVELOPE_INVALID')
        row = valid_row([k['t'],k['o'],k['h'],k['l'],k['c'],k['v'],k['T'],k['q'],k['n'],k['V'],k['Q'],'0'], k['i'])
        if row[0] > source or (k['x'] and row[6] > source):
            raise ValueError('BINANCE_WS_CONFIRMATION_INVALID')
        self.check_epoch(db, epoch)
        state = db.execute('SELECT * FROM topics WHERE topic=?',(name,)).fetchone()
        if not state or not state['ack']: raise ValueError('BINANCE_TOPIC_NOT_ACKED')
        if source <= state['ws_ts']: return False
        changed = self._put(db,name,row,received,source,k['x'],WS_SOURCE,epoch)
        db.execute('UPDATE topics SET ws_rx=?,ws_ts=? WHERE topic=?',(received,source,name))
        self._trim(db,name,source)
        bars = {r['start']:r for r in db.execute('SELECT * FROM bars WHERE topic=?',(name,))}
        anchor = max(bars)
        floor = db.execute('SELECT payload FROM metadata WHERE kind=?',('history_floor:'+name,)).fetchone()
        needed = {anchor-i*step for i in range(LIMITS[k['i']])
                  if floor is None or anchor-i*step >= int(floor[0])}
        missing = not needed <= set(bars)
        unclosed = any(s+step+FRESH_MS <= source and not bars[s]['confirmed'] for s in needed & bars.keys())
        if missing or unclosed:
            db.execute("UPDATE topics SET gap=1,error='MISSING_HISTORY_OR_CLOSE' WHERE topic=?",(name,))
        self._record_readiness(db,name,received)
        return changed

    def ingest_accounted(self, epoch, observations):
        results=[]
        with self.db(write_kind='ws_receipts') as db:
            self.check_epoch(db,epoch)
            for message,received in observations:
                name=topic(message['s'],message['k']['i']);k=message['k']
                try:
                    if type(k.get('t')) is not int or type(message.get('E')) is not int:
                        raise ValueError('BINANCE_EVENT_KEY_INVALID')
                    key=(epoch,name,k['t'],message['E']);digest=payload_hash(message)
                    prior=db.execute('SELECT payload_sha256 FROM bn_keys WHERE epoch=? AND topic=? AND open_time=? AND event_time=?',key).fetchone()
                    if prior:
                        if prior[0]!=digest:raise ValueError('BINANCE_EVENT_KEY_CONFLICT')
                        db.execute('UPDATE bn_counts SET duplicate=duplicate+1 WHERE epoch=? AND topic=?',(epoch,name))
                        results.append('duplicate');continue
                    state=db.execute('SELECT ws_ts FROM topics WHERE topic=?',(name,)).fetchone()
                    if state is None or message['E']<=state[0]:raise ValueError('BINANCE_OLD_UNVERIFIABLE_EVENT')
                    db.execute('SAVEPOINT observation')
                    try:changed=self._ingest_ws(db,epoch,message,received)
                    except (ValueError,KeyError,TypeError,OverflowError):
                        db.execute('ROLLBACK TO observation');raise
                    finally:db.execute('RELEASE observation')
                    db.execute('INSERT INTO bn_keys VALUES(?,?,?,?,?)',(*key,digest))
                    db.execute('DELETE FROM bn_keys WHERE epoch=? AND topic=? AND event_time<?',
                               (epoch,name,message['E']-180000))
                    db.execute('UPDATE bn_counts SET committed=committed+1,revisions_changed=revisions_changed+? WHERE epoch=? AND topic=?',
                               (int(changed),epoch,name))
                    results.append('committed')
                except (ValueError,KeyError,TypeError,OverflowError) as error:
                    reason=str(error) if re.fullmatch('[A-Z0-9_]+',str(error)) else 'BINANCE_INVALID_MARKET_FRAME'
                    self._record(db,epoch,name,message,received,reason,'invalid');results.append('invalid')
        return results


def table_snapshot(store):
    """All durable rows including counters, hashes, receipts and pruned history."""
    with store.db(readonly=True) as db:
        tables = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        result = {}
        for table in tables:
            columns = [row[1] for row in db.execute('PRAGMA table_info('+table+')')]
            result[table] = [dict(row) for row in db.execute(
                'SELECT * FROM '+table+' ORDER BY '+','.join(columns))]
        return result


def replay(store_class, path, scenario):
    """Stable fixture API for the separately pinned old-production runner."""
    if scenario not in SCENARIOS:
        raise ValueError(scenario)

    class ScenarioStore(store_class):
        def _record_readiness(self, db, name, observed):
            super()._record_readiness(db, name, observed)
            if scenario == 'post_write_rollback' and observed == NOW+2:
                raise ValueError('INJECTED_POST_WRITE_FAILURE')

    store = ScenarioStore(Path(path))
    store.enable_health()
    with patch('uuid.uuid4', return_value=uuid.UUID(hex=EPOCH)):
        epoch = store.begin_session(NAMES, 'a'*64)
    store.begin_health(epoch, NAMES, NOW-1)
    store.ack(epoch, NAMES)
    directory = dict(symbols=[dict(symbol='BTCUSDT', baseAsset='BTC', quoteAsset='USDT',
                                  status='TRADING', isSpotTradingAllowed=True)])
    store.instrument_batch(epoch, NAMES, directory, NOW, NOW)
    for name in NAMES:
        _, interval = split_topic(name)
        count = 15 if scenario == 'daily_history_floor' and interval == '1d' else None
        store.ingest_rest(epoch, name, rows(interval, count=count), NOW, NOW)
    observations = [(message(interval), NOW+1) for interval in LIMITS]
    result = dict(outcomes=[store.ingest_accounted(epoch, observations)], errors=[],
                  frozen=[store.freeze(NAMES, NOW+1, NOW+1)], checkpoints=[])
    end = NOW+1

    def ingest(values):
        result['outcomes'].append(store.ingest_accounted(epoch, [(v, v['E']) for v in values]))

    if scenario == 'duplicate_conflict_old':
        duplicate = message()
        conflict = copy.deepcopy(duplicate); conflict['k']['c'] = '102'
        ingest([duplicate, conflict, message(stamp=NOW), message(stamp=NOW+3)])
        end = NOW+3
    elif scenario == 'same_topic_batch':
        ingest([message(stamp=NOW+2), message(stamp=NOW+4), message(stamp=NOW+3),
                message(stamp=NOW+2), message(stamp=NOW+5)])
        end = NOW+5
    elif scenario == 'invalid_envelopes':
        wrong_event = message(stamp=NOW+2); wrong_event['e'] = 'trade'
        wrong_bool = message(stamp=NOW+3); wrong_bool['k']['x'] = 1
        bad_row = message(stamp=NOW+4); bad_row['k']['h'] = '0'
        early_close = message(stamp=NOW+5, closed=True)
        ingest([wrong_event, wrong_bool, bad_row, early_close, message(stamp=NOW+6)])
        end = NOW+6
    elif scenario == 'attempted_unclose':
        closed = (NOW//STEPS['15m']-1)*STEPS['15m']
        ingest([message(stamp=NOW+2, start=closed, closed=True),
                message(stamp=NOW+3, start=closed), message(stamp=NOW+4)])
        end = NOW+4
    elif scenario == 'post_write_rollback':
        rejected = message(stamp=NOW+2); rejected['k']['c'] = '102'
        ingest([rejected, message(stamp=NOW+3)])
        end = NOW+3
    elif scenario == 'unacked':
        with store.db() as db:
            db.execute('UPDATE topics SET ack=0 WHERE topic=?', (topic('BTCUSDT','15m'),))
        ingest([message(stamp=NOW+2)])
        end = NOW+2
    elif scenario == 'stale_epoch':
        for token in ('2'*32, epoch):
            if token == epoch:
                store.disconnect(epoch)
            try:
                store.ingest_accounted(token, [(message(stamp=NOW+2), NOW+2)])
            except ValueError as error:
                result['errors'].append(str(error))
        end = NOW+2
    elif scenario in ('closed_boundary', 'missing_close'):
        step = STEPS['5m']; boundary = (NOW//step+1)*step
        if scenario == 'closed_boundary':
            ingest([message('5m', boundary, boundary-step, True), message('5m', boundary+1)])
            end = boundary+1
        else:
            end = boundary+FRESH_MS+1
            ingest([message('5m', end)])
        store.instrument_batch(epoch, NAMES, directory, end, end)
        result['frozen'].append(store.freeze([topic('BTCUSDT','5m')], end, end))
    elif scenario == 'revision_pruning':
        updates = [message(stamp=NOW+2+i) for i in range(MAX_REVISIONS+3)]
        for offset in range(0, len(updates), 100):
            ingest(updates[offset:offset+100])
        result['checkpoints'].append(table_snapshot(store))
        end = NOW+HISTORY_MS+500
        ingest([message(stamp=end)])
    elif scenario == 'legacy_default_state':
        result['outcomes'].append(store.ingest_ws_batch(epoch, [
            (message(stamp=NOW+2), NOW+2), (message(stamp=NOW+3), NOW+3),
            (message(stamp=NOW+2), NOW+2)]))
        end = NOW+3
    elif scenario == 'legacy_batch_rollback':
        invalid = message(stamp=NOW+3); invalid['k']['h'] = '0'
        try:
            store.ingest_ws_batch(epoch, [(message(stamp=NOW+2), NOW+2), (invalid, NOW+3)])
        except ValueError as error:
            result['errors'].append(str(error))
        end = NOW+3
    elif scenario in ('missing_history', 'off_grid_anchor', 'floor_absent',
                      'floor_above_anchor', 'floor_off_grid', 'floor_invalid'):
        name = topic('BTCUSDT','15m'); step = STEPS['15m']; anchor = NOW//step*step
        with store.db() as db:
            if scenario == 'missing_history':
                db.execute('DELETE FROM bars WHERE topic=? AND start=?',(name,anchor-step))
            elif scenario == 'off_grid_anchor':
                db.execute('''INSERT INTO bars SELECT topic,start+1,row_json,observed,
                    source_time,confirmed,origin,epoch FROM bars WHERE topic=? AND start=?''',
                    (name,anchor))
            elif scenario == 'floor_absent':
                db.execute('DELETE FROM metadata WHERE kind=?',('history_floor:'+name,))
            else:
                floor = {'floor_above_anchor':anchor+1,
                         'floor_off_grid':anchor-10*step+1,
                         'floor_invalid':'not an integer'}[scenario]
                db.execute('UPDATE metadata SET payload=? WHERE kind=?',
                           (str(floor),'history_floor:'+name))
        end = NOW+2; ingest([message(stamp=end)])
    elif scenario == 'confirmed_storage_types':
        name = topic('BTCUSDT','15m'); old = (NOW//STEPS['15m']-2)*STEPS['15m']
        values = (None,0,0.0,False,1,2,-1,.5,'','false','0notnumber','\0','\0value',
                  b'',b'\0',b'0',b'false')
        for index,value in enumerate(values):
            with store.db() as db:
                db.execute('UPDATE topics SET gap=0,error=NULL WHERE topic=?',(name,))
                db.execute('UPDATE bars SET confirmed=? WHERE topic=? AND start=?',(value,name,old))
            end = NOW+index+2; ingest([message(stamp=end)])
            result['checkpoints'].append(table_snapshot(store))
    elif scenario in ('start_real','start_text','start_blob','start_null'):
        name = topic('BTCUSDT','15m'); anchor = NOW//STEPS['15m']*STEPS['15m']
        value = {'start_real':anchor+.5,'start_text':'unexpected',
                 'start_blob':b'noninteger','start_null':None}[scenario]
        with store.db() as db:
            db.execute('''INSERT INTO bars SELECT topic,?,row_json,observed,
                source_time,confirmed,origin,epoch FROM bars WHERE topic=? AND start=?''',
                (value,name,anchor))
        end = NOW+2; ingest([message(stamp=end)])
    result['frozen'].append(store.freeze(NAMES, end, end))
    result['tables'] = table_snapshot(store)
    return result


class IngestEquivalenceTests(unittest.TestCase):
    def compare(self, scenario):
        with tempfile.TemporaryDirectory() as root:
            current = replay(Store, Path(root)/'current.sqlite3', scenario)
            original = replay(Ec3dHotPathOracle, Path(root)/'oracle.sqlite3', scenario)
        self.assertEqual(current, original)
        self.assertEqual(current['frozen'][0]['status'], 'READY')
        return current

    def test_all_actual_intervals_and_frozen_transport_unchanged(self):
        value = self.compare('all_intervals')
        self.assertEqual(set(value['frozen'][-1]['windows']), set(NAMES))

    def test_duplicate_conflict_and_old_event_receipts_unchanged(self):
        value = self.compare('duplicate_conflict_old')
        self.assertEqual(value['outcomes'][-1], ['duplicate','invalid','invalid','committed'])
        self.assertEqual([r['reason'] for r in value['tables']['bn_unprocessed']],
                         ['BINANCE_EVENT_KEY_CONFLICT','BINANCE_OLD_UNVERIFIABLE_EVENT'])

    def test_same_topic_batch_reads_fresh_state_for_each_frame(self):
        value = self.compare('same_topic_batch')
        self.assertEqual(value['outcomes'][-1], ['committed','committed','invalid','duplicate','committed'])
        self.assertEqual(value['tables']['bn_unprocessed'][0]['reason'], 'BINANCE_OLD_UNVERIFIABLE_EVENT')

    def test_invalid_envelopes_rows_and_early_close_retain_receipts(self):
        value = self.compare('invalid_envelopes')
        self.assertEqual(value['outcomes'][-1], ['invalid']*4+['committed'])
        self.assertEqual([r['reason'] for r in value['tables']['bn_unprocessed']], [
            'BINANCE_WS_ENVELOPE_INVALID','BINANCE_WS_ENVELOPE_INVALID',
            'BINANCE_BAR_VALUES_INVALID','BINANCE_WS_CONFIRMATION_INVALID'])
        self.assertFalse(any(NOW+2<=r['source_time']<=NOW+5 for r in value['tables']['revisions']))

    def test_confirmed_bar_cannot_be_unclosed_but_receipt_still_commits(self):
        value = self.compare('attempted_unclose')
        self.assertEqual(value['outcomes'][-1], ['committed']*3)
        counts = next(row for row in value['tables']['bn_counts'] if row['topic']==topic('BTCUSDT','15m'))
        self.assertEqual((counts['committed'], counts['revisions_changed']), (4,3))
        self.assertEqual(value['frozen'][-1]['status'], 'READY')

    def test_error_after_bar_write_rolls_back_only_that_frame(self):
        value = self.compare('post_write_rollback')
        self.assertEqual(value['outcomes'][-1], ['invalid','committed'])
        self.assertFalse(any(row['source_time']==NOW+2 for row in value['tables']['revisions']))
        self.assertFalse(any(row['event_time']==NOW+2 for row in value['tables']['bn_keys']))
        self.assertEqual(value['tables']['bn_unprocessed'][0]['reason'], 'INJECTED_POST_WRITE_FAILURE')

    def test_same_frame_state_still_requires_ack(self):
        value = self.compare('unacked')
        self.assertEqual(value['outcomes'][-1], ['invalid'])
        self.assertEqual(value['tables']['bn_unprocessed'][0]['reason'], 'BINANCE_TOPIC_NOT_ACKED')

    def test_epoch_and_disconnect_remain_fail_closed(self):
        value = self.compare('stale_epoch')
        self.assertEqual(value['errors'], ['STALE_SESSION','STALE_SESSION'])
        self.assertEqual(value['frozen'][-1]['status'], 'UNKNOWN')

    def test_closed_native_boundary_requires_both_close_and_current(self):
        value = self.compare('closed_boundary')
        self.assertEqual(value['frozen'][1]['status'], 'READY')
        bars = value['frozen'][1]['windows'][topic('BTCUSDT','5m')]
        self.assertTrue(bars['transport'][-2]['confirm'])
        self.assertFalse(bars['transport'][-1]['confirm'])

    def test_missing_close_keeps_gap_and_unknown(self):
        value = self.compare('missing_close')
        state = next(row for row in value['tables']['topics'] if row['topic']==topic('BTCUSDT','5m'))
        self.assertEqual((state['gap'],state['error']), (1,'MISSING_HISTORY_OR_CLOSE'))
        self.assertEqual(value['frozen'][1]['status'], 'UNKNOWN')

    def test_daily_short_history_floor_remains_usable(self):
        value = self.compare('daily_history_floor')
        self.assertEqual(len(value['frozen'][-1]['windows'][topic('BTCUSDT','1d')]['rows']),15)

    def test_revision_key_and_readiness_pruning_unchanged(self):
        value = self.compare('revision_pruning')
        name = topic('BTCUSDT','15m'); opened = NOW//STEPS['15m']*STEPS['15m']
        selected = lambda snapshot: [r for r in snapshot['revisions'] if r['topic']==name and r['start']==opened]
        self.assertEqual(len(selected(value['checkpoints'][0])), MAX_REVISIONS)
        self.assertEqual(len(selected(value['tables'])), 2)
        self.assertEqual(len([r for r in value['tables']['bn_keys'] if r['topic']==name]),1)

    def test_default_state_legacy_calls_preserve_returns_and_frozen_data(self):
        value = self.compare('legacy_default_state')
        self.assertEqual(value['outcomes'][-1], [True,True,False])
        self.assertEqual(value['frozen'][-1]['status'], 'READY')

    def test_legacy_batch_error_still_rolls_back_the_whole_batch(self):
        value = self.compare('legacy_batch_rollback')
        self.assertEqual(value['errors'], ['BINANCE_BAR_VALUES_INVALID'])
        self.assertFalse(any(row['source_time']>NOW+1 for row in value['tables']['revisions']))

    def test_accepted_frames_keep_individual_epoch_checks(self):
        with tempfile.TemporaryDirectory() as root:
            store = Store(Path(root)/'state.sqlite3'); store.enable_health()
            epoch = store.begin_session(NAMES,'a'*64)
            store.begin_health(epoch,NAMES,NOW); store.ack(epoch,NAMES)
            with patch.object(store,'check_epoch',wraps=store.check_epoch) as checked:
                self.assertEqual(store.ingest_accounted(epoch,[(message(stamp=NOW+i),NOW+i) for i in (1,2)]),
                                 ['committed','committed'])
            self.assertEqual(checked.call_count,3)  # Admission plus every accepted frame.

    def test_aggregate_keeps_exact_membership_and_history_floor(self):
        for scenario in ('missing_history','off_grid_anchor','floor_absent',
                         'floor_above_anchor','floor_off_grid','floor_invalid'):
            with self.subTest(scenario=scenario):
                value = self.compare(scenario)
                state = next(r for r in value['tables']['topics'] if r['topic']==topic('BTCUSDT','15m'))
                self.assertEqual(value['outcomes'][-1],
                                 ['invalid' if scenario=='floor_invalid' else 'committed'])
                self.assertEqual(state['gap'],int(scenario in ('missing_history','off_grid_anchor','floor_invalid')))

    def test_aggregate_matches_python_confirmation_truthiness_for_sqlite_types(self):
        value = self.compare('confirmed_storage_types')
        name = topic('BTCUSDT','15m'); old = (NOW//STEPS['15m']-2)*STEPS['15m']
        for snapshot in value['checkpoints']:
            confirmed = next(r['confirmed'] for r in snapshot['bars'] if r['topic']==name and r['start']==old)
            state = next(r for r in snapshot['topics'] if r['topic']==name)
            self.assertEqual(state['gap'],int(not confirmed))
        self.assertTrue(all(outcomes==['committed'] for outcomes in value['outcomes'][1:]))

    def test_malformed_start_fallback_preserves_original_outcome_and_receipts(self):
        for scenario in ('start_real','start_text','start_blob','start_null'):
            with self.subTest(scenario=scenario):
                value = self.compare(scenario)
                self.assertEqual(value['outcomes'][-1],['committed' if scenario=='start_real' else 'invalid'])
                state = next(r for r in value['tables']['topics'] if r['topic']==topic('BTCUSDT','15m'))
                self.assertEqual(state['gap'],1)

    def test_history_check_returns_bounded_sqlite_rows_with_identical_durable_result(self):
        measurements=[];snapshots=[]
        with tempfile.TemporaryDirectory() as root:
            for index,store_class in enumerate((Store,Ec3dHotPathOracle)):
                store=store_class(Path(root)/('store%d.sqlite3'%index));store.enable_health()
                name=topic('BTCUSDT','1d')
                with patch('uuid.uuid4',return_value=uuid.UUID(hex=EPOCH)):
                    epoch=store.begin_session([name],'a'*64)
                store.begin_health(epoch,[name],NOW);store.ack(epoch,[name])
                store.ingest_rest(epoch,name,rows('1d'),NOW,NOW)
                returned=[]
                with store.db() as db:
                    factory=db.row_factory
                    def measured_row(cursor,row):
                        returned.append(len(row))
                        return factory(cursor,row)
                    db.row_factory=measured_row
                    self.assertTrue(store._ingest_ws(db,epoch,message('1d'),NOW+1))
                measurements.append(len(returned));snapshots.append(table_snapshot(store))
        self.assertEqual(snapshots[0],snapshots[1])
        self.assertLessEqual(measurements[0],8)
        self.assertGreaterEqual(measurements[1],LIMITS['1d'])


if __name__ == '__main__':
    unittest.main()
