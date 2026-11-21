"""Independent, read-only full-roster pressure probe; never a Monitor workload."""
import datetime as dt
import json
import os
from pathlib import Path
import sys
import time

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    from scripts.profile_binance_session_topology import (install_guard, read_config, summarize,
        write_json, loaded_source_audit, SQLiteTimeline)
    if sys.platform != 'linux' or len(sys.argv) != 2:
        raise ValueError('SESSION_READER_LINUX_EXACT_ARGUMENTS_REQUIRED')
    scratch = Path(sys.argv[1])
    config = read_config(scratch)
    blocked = install_guard(scratch, 'reader', set())
    before = json.loads((scratch/'source-before.json').read_text(encoding='utf-8'))
    from research import hunter_binance_stream_consumer as consumer
    from research.hunter_binance_health_store import Store
    from research.hunter_binance_control import ControlError
    from tests.fixtures.binance_reconnect_diagnostics import error_detail
    from tests.test_hunter_binance_coordinated_runtime import topics
    from tests.test_hunter_binance_coordinated_soak import LatencyHistogram
    names = topics(['BTCUSDT']+['S%03dUSDT' % i for i in range(408)])
    clock = lambda: config['base_ms']+int((time.monotonic()-config['anchor_mono'])*1000)
    hist = {key: LatencyHistogram('us') for key in ('wall', 'thread_cpu')}
    timeline = SQLiteTimeline(scratch,'reader'); timeline.install()
    reader = Store.reader(scratch/'state.sqlite3')
    transient = {'CONTROL_PUBLICATION_PENDING','CONTROL_READ_FENCE_CHANGED',
        'BINANCE_FINAL_READ_REVOKED','BINANCE_FINAL_TOPIC_REVOKED',
        'BINANCE_CACHE_UNKNOWN:BINANCE_TOPIC_STALE_OR_NOT_READY',
        'BINANCE_CACHE_UNKNOWN:BINANCE_CURRENT_WS_REQUIRED'}
    result = dict(scope='CONTINUOUS_1228_TOPIC_PRESSURE_NOT_MONITOR', topics=len(names),pid=os.getpid(),
        attempts=0, ready=0, rejected={}, errors=[], blocked_effects=blocked,completed_after_load=0,
        transient_reasons=sorted(transient),timing_scope='CALL_START_IN_LOAD_INCLUSIVE_LAST_CALL_MAY_FINISH_AFTER')
    (scratch/'reader.ready').write_text('ready', encoding='utf-8')
    try:
        wait_end = time.monotonic()+120
        while not (scratch/'load.ready').exists():
            if time.monotonic() >= wait_end:
                raise TimeoutError('SESSION_READER_START_TIMEOUT')
            time.sleep(.02)
        load = json.loads((scratch/'load.json').read_text(encoding='utf-8'))
        while time.monotonic() < load['end_mono'] and not (scratch/'reader.stop').exists():
            start, cpu = time.perf_counter_ns(), time.thread_time_ns()
            result['attempts'] += 1
            try:
                frozen = consumer.read(names, dt.datetime.fromtimestamp(clock()/1000, dt.timezone.utc),
                                       reader=reader, clock=clock)
                if set(frozen.generation['windows']) != set(names):
                    raise AssertionError('SESSION_READER_SCOPE_SHRANK')
                frozen.check_delivery()
                result['ready'] += 1
            except (ValueError, ControlError) as error:
                reason = str(error)
                result['rejected'][reason] = result['rejected'].get(reason, 0)+1
                if reason not in transient:
                    result['errors'].append(error_detail('consumer.unexpected_rejection',error))
                    break
            except BaseException as error:
                result['errors'].append(error_detail('consumer.read_and_delivery', error))
                break
            finally:
                hist['wall'].add((time.perf_counter_ns()-start)//1000)
                hist['thread_cpu'].add((time.thread_time_ns()-cpu)//1000)
                result['completed_after_load'] += int(time.monotonic()>load['end_mono'])
    except BaseException as error:
        result['errors'].append(error_detail('reader.lifecycle', error))
    finally:
        result.update(summarize(hist),finished_mono=time.monotonic())
        try:
            result.update(market_transactions=reader.transaction_diagnostics(include_file_state=False),
                control_transactions=None if getattr(reader,'control',None) is None else reader.control.transaction_diagnostics(),
                source_audit=loaded_source_audit(before))
        except BaseException as error:
            result['errors'].append(error_detail('reader.final_evidence',error))
        try:
            result['sqlite_file_state'] = timeline.file_state()
        except BaseException as error:
            result['errors'].append(error_detail('reader.final_file_state',error))
        result['sqlite_timeline'] = timeline.snapshot()
        timeline.patches.close()
        result['errors'].extend(result['sqlite_timeline']['diagnostic_errors'])
        result['diagnostic_completed'] = not result['errors'] and not blocked and result.get('source_audit',{}).get('before_after_equal',False)
        # The guard permits only this one result file, never SQLite mutation.
        write_json(scratch/'reader.result.json', result)
    return 0 if result['diagnostic_completed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
