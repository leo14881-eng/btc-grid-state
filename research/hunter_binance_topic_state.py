"""Fail-closed topic revision projection for Binance SPOT (draft candidate).

This is a data-change hint, NOT an authority to deliver market data.
Consumers must retain existing scope, validity, proof, hash and control fences.
"""
from dataclasses import dataclass
import sqlite3

EXCHANGE = "BINANCE"
MARKET = "spot"
STATUSES = frozenset(("valid", "quiet_verified", "invalid", "quarantined"))


def install(db):
    db.execute("""CREATE TABLE IF NOT EXISTS topic_state (
        exchange TEXT NOT NULL, market TEXT NOT NULL,
        symbol TEXT NOT NULL, interval TEXT NOT NULL,
        epoch TEXT NOT NULL, generation TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision >= 0),
        last_rx_ms INTEGER NOT NULL, last_commit_ms INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN
            ('valid','quiet_verified','invalid','quarantined')),
        valid_until_ms INTEGER NOT NULL,
        PRIMARY KEY (exchange,market,symbol,interval)
    )""")


def key(name):
    from research.hunter_binance_stream_store import split_topic
    symbol, interval = split_topic(name)
    return EXCHANGE, MARKET, symbol, interval


def begin(db, names, epoch):
    install(db)
    db.execute("DELETE FROM topic_state WHERE exchange=? AND market=?",
               (EXCHANGE, MARKET))
    db.executemany("""INSERT INTO topic_state VALUES
        (?,?,?,?,?,?,0,0,0,'invalid',0)""",
        [(*key(name), epoch, epoch) for name in names])


def bump(db, name, epoch, received, *, status="valid", valid_until_ms=None, repaired=False):
    if status not in STATUSES:
        raise ValueError("BINANCE_TOPIC_STATE_STATUS_INVALID")
    if type(received) is not int or received < 0:
        raise ValueError("BINANCE_TOPIC_STATE_TIME_INVALID")
    if valid_until_ms is None:
        valid_until_ms = received if status != "valid" else received + 30_000
    if type(valid_until_ms) is not int:
        raise ValueError("BINANCE_TOPIC_STATE_VALIDITY_INVALID")
    identity=key(name)
    # The surrounding market transaction is authoritative. Do not allocate a
    # new epoch when a stale writer misses its scoped row.
    cur=db.execute("""UPDATE topic_state SET
        revision=revision+1, last_rx_ms=?,last_commit_ms=?,
        status=CASE WHEN status IN ('invalid','quarantined')
             AND last_rx_ms>0 AND ?=0 THEN status ELSE ? END,
        valid_until_ms=?
        WHERE exchange=? AND market=? AND symbol=? AND interval=?
          AND epoch=? AND generation=?""",
        (received, received, int(bool(repaired)), status, valid_until_ms, *identity, epoch, epoch))
    if cur.rowcount != 1:
        raise ValueError("BINANCE_TOPIC_STATE_EPOCH_OR_SCOPE_MISMATCH")


def _read_rows(db):
    return {tuple(row[k] for k in ("exchange","market","symbol","interval")):dict(row)
            for row in db.execute("SELECT * FROM topic_state WHERE exchange=? AND market=?", (EXCHANGE,MARKET))}


class IncrementalReader:
    """Two-phase revision protocol. Results are hints, not validated candles."""
    def __init__(self, path):
        from pathlib import Path
        self.path=Path(path)
        if not self.path.is_absolute():
            raise ValueError("ABSOLUTE_STATE_PATH_REQUIRED")
        self.previous={}

    def poll(self, now_ms, *, before_recheck=None):
        if type(now_ms) is not int:
            raise ValueError("BINANCE_TOPIC_STATE_TIME_INVALID")
        uri=self.path.as_uri()+"?mode=ro"
        with sqlite3.connect(uri,uri=True,timeout=.25) as db:
            db.row_factory=sqlite3.Row
            db.execute("PRAGMA query_only=1")
            db.execute("PRAGMA busy_timeout=250")
            # No long-lived read transaction; row copy closes before parsing.
            begin_rows=_read_rows(db)
        changed={k:v for k,v in begin_rows.items() if
                 k not in self.previous or self.previous[k] != (
                     v["revision"],v["epoch"],v["generation"])}
        # Copy only changed topics, in one short read transaction. Opening
        # one connection per topic multiplies WAL/SQLite overhead by 1228.
        from research.hunter_binance_stream_store import topic
        mapped={topic(k[2],k[3]):k for k in changed if k[:2]==(EXCHANGE,MARKET)}
        samples={k:[] for k in mapped.values()}
        with sqlite3.connect(uri,uri=True,timeout=.25) as db:
            db.row_factory=sqlite3.Row
            db.execute("PRAGMA query_only=1")
            db.execute("PRAGMA busy_timeout=250")
            db.execute("BEGIN")
            for offset in range(0,len(mapped),400):
                group=list(mapped)[offset:offset+400]
                if not group:continue
                marks=",".join("?" for _ in group)
                for row in db.execute(
                    "SELECT * FROM bars WHERE topic IN ("+marks+") ORDER BY topic,start",
                    group):
                    samples[mapped[row["topic"]]].append(dict(row))
            db.commit()
        if before_recheck:
            before_recheck()
        with sqlite3.connect(uri,uri=True,timeout=.25) as db:
            db.row_factory=sqlite3.Row
            db.execute("PRAGMA query_only=1")
            db.execute("PRAGMA busy_timeout=250")
            end_rows=_read_rows(db)
        # Any change of an old topic OR whole-roster reconfiguration refuses
        # the whole read; do not re-use stale bars or stale previous cache.
        if begin_rows.keys()!=end_rows.keys() or any(
            (v["revision"],v["epoch"],v["generation"]) != (
              end_rows[k]["revision"],end_rows[k]["epoch"],end_rows[k]["generation"])
            for k,v in begin_rows.items()):
            self.previous.clear()
            raise ValueError("BINANCE_TOPIC_STATE_REVISION_RACE")
        if any(v["status"] not in ("valid","quiet_verified")
               or now_ms>v["valid_until_ms"] for v in begin_rows.values()):
            self.previous.clear()
            raise ValueError("BINANCE_TOPIC_STATE_INVALID_OR_EXPIRED")
        self.previous={k:(v["revision"],v["epoch"],v["generation"])
                       for k,v in begin_rows.items()}
        return dict(changed=samples,changed_topics=len(changed),
                    topic_count=len(begin_rows),state=begin_rows)
