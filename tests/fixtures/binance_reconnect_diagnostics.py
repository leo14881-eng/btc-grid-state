"""Bounded failure evidence for the loopback collector fixture only."""
import traceback


MAX_TRACEBACK = 16384
MAX_ERRORS = 16


def error_detail(operation, error):
    trace = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
    return dict(operation=operation, type=type(error).__name__, message=str(error),
        traceback=trace[-MAX_TRACEBACK:], traceback_truncated=len(trace) > MAX_TRACEBACK,
        error_market_phase=getattr(error, 'market_phase', None),
        error_control_phase=getattr(error, 'control_phase', None),
        sqlite_errorcode=getattr(error, 'sqlite_errorcode', 'unknown'),
        sqlite_errorname=getattr(error, 'sqlite_errorname', 'unknown'),
        sqlite_context=getattr(error, 'sqlite_context', None))


def append_error(errors, detail):
    # The caller separately counts every failure; truncation never makes a run
    # successful. Keep the first contexts, which precede cascading shutdown.
    if len(errors) < MAX_ERRORS:
        errors.append(detail)


def final_state(store, scratch):
    """Snapshot each fact independently; cleanup reads never hide the primary.

    Transaction snapshots are pure in-memory copies and still available when
    either SQLite file remains locked. Later DB reads are explicitly separate.
    Unknown counts remain None, never a fabricated clean shutdown.
    """
    value = dict(active_owners=None, requests=None, calls=None, terminals=None,
        attempts=None, retired=None, orphan_files=None, connected=None,
        market_transactions=None, control_transactions=None, final_read_errors=[],
        final_observation='BEST_EFFORT_AFTER_COLLECT_AND_WATCHER_JOIN',
        transaction_observation='IN_MEMORY_LIFETIME_PHASE_METRICS')

    def attempt(operation, action):
        try:
            action()
        except BaseException as error:
            append_error(value['final_read_errors'], error_detail(operation, error))

    def market_metrics():
        value['market_transactions'] = store.transaction_diagnostics(include_file_state=False)

    def control_metrics():
        value['control_transactions'] = store.control.transaction_diagnostics()

    def counts():
        with store.control._db(readonly=True) as db:
            value['active_owners'] = db.execute('SELECT COUNT(*) FROM owners').fetchone()[0]
            value['requests'] = db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
            value['calls'], value['terminals'] = db.execute(
                'SELECT COUNT(*),COUNT(terminal) FROM calls').fetchone()
            value['attempts'], value['retired'] = db.execute(
                "SELECT COUNT(*),SUM(CASE WHEN terminal='RETIRED' THEN 1 ELSE 0 END) FROM attempts").fetchone()

    def market_state():
        value['connected'] = store.metadata().get('connected')

    def orphans():
        value['orphan_files'] = len(list(scratch.glob('*.owner-*.lock')))

    # Copy metrics before later reads can change their counters or error state.
    for operation, action in (('market.in_memory_metrics', market_metrics),
            ('control.in_memory_metrics', control_metrics), ('control.final_counts', counts),
            ('market.final_metadata', market_state), ('fixture.final_orphan_files', orphans)):
        attempt(operation, action)
    return value
