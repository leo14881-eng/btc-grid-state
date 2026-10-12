# Historical replay response reuse (disabled by default)

Scope: only `hunter_tail_risk_replay.get` and `hunter_leading_risk_replay.get`.
The original parsers, alignments, detectors, financial proxies, retry schedules,
reports and workflow contract guards still run. No strategy, live market-data
consumer, policy, ledger, runner, workflow or verification-receipt changes.
No production enablement, merge, deployment or additional request concurrency.
Merging this PR alone produces no production request savings: runner wiring and
explicit activation remain absent. The original 32 requests still run by default.

## Verified request shape

Tail makes eight GETs. Leading makes three windows times eight GETs. One window
matches tail exactly: 32 requests / 24 unique requests, before retries. Monthly
ZIP archive caching does not cover these REST paths.

Both request public Binance spot `/api/v3/klines`, `5m`, `limit=1000`, the same
eight symbols, with explicit start/end times. Binance's end bound is inclusive
on **open** time. A 24h request contains 289 rows; tail discards the last close
beyond its end time, while leading keeps it. The 48h windows contain 577 rows.
The raw response, including that boundary row, is reused without sharing parser
results. Every returned row, including the boundary row, must already be closed.

## First-response snapshot and consumer provenance

The cache is a **first-successfully-published validated response snapshot** for
one invocation/request. Closed candles can still be corrected at the source.
If the original two HTTP calls would return valid versions A then B, an enabled
sequential hit deliberately returns A to both consumers. This is not a promise
that the second uncached response, resulting report or risk decision equals A.
The fixed-input equivalence test holds only when the source bytes are fixed.

Concurrent misses can already have fetched different versions before either
publishes. The first successful atomic publisher owns the snapshot; the loser
returns its own original HTTP response. It never substitutes the winner's bytes
for a response it already fetched, overwrites the snapshot or adds a request.
Subsequent valid hits use the published snapshot. Fallback therefore does not
guarantee a uniform snapshot across all consumers; the source receipts expose it.

Each eligible enabled consumption emits `HUNTER_REPLAY_CONSUMPTION` to the
existing runner stdout/journal, independently of the unchanged replay report.
It records invocation UUID, generation, canonical request/request ID, consumer
(`tail` or `leading`), original HTTP versus snapshot selection, publication result,
raw-byte SHA-256, original UTC/monotonic fetch times and boot ID. An invocation
that cannot establish valid clock/context has no cache receipt or cache hit.
Cached-source and new-HTTP-source metadata are separate. If hit revalidation
fails and the fallback HTTP response then cannot acquire its own fetch clock,
the original response still reaches the parser, without a complete source
receipt. It never inherits the rejected snapshot's hash or fetch time. A real
opener regression covers this with valid source revisions A/B and two consecutive
clock failures, and verifies no extra HTTP attempt or cache rewrite.
Records contain no payload, credential, arbitrary URL or environment dump. They
are diagnostic provenance, not an authenticated trading/validation receipt.
The fixed experiment retains these records as `source_receipts` in its output
JSON; production journal retention belongs to the existing caller, not this cache.

A hit is checked again after writing its journal record. If that I/O crosses
expiry or the context changes, `HUNTER_REPLAY_SELECTION_INVALIDATED <receipt_id>`
cancels the selection, and the original HTTP fallback gets its own receipt.
Readers must exclude cancelled IDs, as `receipts_from` in the fixture does.
Provenance I/O failure cannot add retries to an original HTTP response. No source
records are added to strategy reports, ledgers or the 64-file cache namespace.

## Explicit integration interface (not wired into production)

All seven variables are required; otherwise each request follows its old path:

| Variable | Meaning |
| --- | --- |
| `HUNTER_REPLAY_CACHE=1` | Explicit opt-in |
| `HUNTER_REPLAY_CACHE_DIR` | Existing dedicated absolute POSIX directory, owned by current UID, mode 0700 |
| `HUNTER_REPLAY_CACHE_GENERATION` | Admitted Research scan generation, 1–160 safe characters |
| `HUNTER_REPLAY_CACHE_CYCLE` | Fresh UUID4 hex (32 lowercase hex characters) for **this Research invocation**, shared by its two processes |
| `HUNTER_REPLAY_CACHE_STARTED_AT` | Original invocation start, Unix seconds; never refreshed by a retry or hit |
| `HUNTER_REPLAY_CACHE_STARTED_MONOTONIC_NS` | Same invocation's `time.clock_gettime_ns(time.CLOCK_BOOTTIME)`; includes suspend; never refreshed |
| `HUNTER_REPLAY_CACHE_BOOT_ID` | Same host boot's `/proc/sys/kernel/random/boot_id`, checked again by each consumer |

Future runner ownership must provide these once per natural Research invocation
and dispose of its dedicated directory after the two replays. A scan generation
alone is insufficient: multiple Research runs or retries can use the same scan.
This PR deliberately leaves that integration to a separately reviewed change;
no workflow, runner or existing generation/CAS interface is modified. Reusing a
cycle UUID for a different invocation violates this interface. Capture the wall
and BOOTTIME origins together once, with BOOTTIME sampled first for conservative
expiry. The new monotonic/boot fields intentionally invalidate the previous
five-variable interface and v1 entries. Windows and platforms without Linux
BOOTTIME/boot identity or safe POSIX operations always bypass the cache.

## Admission and failure behavior

The key includes schema, generation, cycle, original wall/monotonic cycle starts,
boot ID, method, exact
HTTPS origin, spot market, endpoint, every query parameter and Accept header.
Only `https://data-api.binance.vision` and `https://api.binance.com` are admitted,
and they never share entries. Query-key order is immaterial; duplicate keys,
unknown parameters, alternative intervals/limits/symbols, fragments, credentials,
cookies, POSTs and unknown headers are rejected. The two existing fixed
User-Agent labels only identify the replay caller and do not select market data;
they are the sole deliberate header difference excluded from the key. Neither
on-wire User-Agent changes.

Only HTTP 200 JSON from the requested source, without Set-Cookie, is admitted.
Strict checks cover all 12 kline columns, exact row count and contiguous timestamps,
positive finite OHLC, price ranges, finite nonnegative volumes, trades and closed
times. Partial/error/rate-limited responses and future windows are never reused.
The response bytes and original fetch time are stored with SHA-256 checksums;
identity, checksum, row content and age are revalidated on every read. Checksums
detect corruption; they do not authenticate files against the same privileged
OS user. Private owner-only directories are part of the trust boundary.

TTL is fixed at 900 seconds from the original cycle start and original fetch on
both wall time and same-boot BOOTTIME. Small wall-clock rollback cannot extend
actual TTL; future counters, reboot, missing clocks or expired clocks miss.
Identity and both clocks are rechecked after decoding/row validation, after closing
the descriptor, and after journal output immediately before returning a hit.
Writers also recheck before creating a temp and immediately before publication.
Bodies are at most 256 KiB, envelopes
360 KiB, rows 1000, namespace files (including abandoned partials) 64, root
namespaces 64. Nonblocking directory locks bound concurrent namespace/file
creation. Publication reserves two directory entries before starting, including
the temporary-plus-destination interval. At most 63 durable names remain after a
normal write, with a peak of 64; 63 or 64 existing names force a miss publication
to bypass storage. No sweeper deletes an active or untrusted directory; exhausted budgets
bypass caching until the caller disposes of its scratch directory.

Every path component is opened using directory FDs and `O_NOFOLLOW`. Owner/mode,
regular-file and hardlink checks reject unsafe files. New 0600 temporary files
are flushed/fsynced and linked atomically without replacing an existing entry.
A process interrupted before publication leaves only an ignored partial file.
A process dying after link but before unlink leaves two names with link count 2;
reads reject that entry and fall back without deleting or repairing it. The real
child-crash test performs this at 62 pre-existing entries, yielding exactly 64.
Concurrent writers may each make their original request, but cannot add retries,
overwrite a first successful response, expose partial data or refresh its fetch
time. Cache errors always fall back to the existing single HTTP attempt; leading's
existing four-attempt retry loop and tail's one attempt are unchanged, including
403/429 handling. This does not bypass access or regional restrictions.

## Reproducible offline evidence

Run on Linux from the repository root:

```text
python -B -X utf8 -m unittest discover -s tests -p test_hunter_replay_history_cache.py -v
python -B -X utf8 tests/fixtures/hunter_replay_history_experiment.py > /tmp/replay-history-experiment.json
python -B -X utf8 -m unittest discover -s tests -p 'test_hunter*.py' -v
```

`tests/fixtures/hunter_replay_history_expected.json` was generated by executing the
unchanged replay sources from commit `95779e1b947e52b894e3d0c4df20e0abea66cc43`
against `body_for` in the experiment fixture, with clock fixed to
2026-10-12 00:00 UTC. The fixture records SHA-256 of both original Python sources,
all original raw responses, consumed rows, and complete reports. It is synthetic
test input, not production performance evidence. To regenerate the baseline,
read each original source using `git show <commit>:research/hunter_{tail,leading}_risk_replay.py`,
execute it as a module and call the fixture's `run_stage(module, temporary_dir)`.

The experiment runs tail and leading as separate subprocesses for each mode,
uses the real `urlopen` / opener / HTTPS handler / HTTP request and response
processing chain, and replaces only `HTTPSConnection.connect` with an in-memory
wire response. DNS and real socket connections are blocked. This exercises
stdlib request mutations: `do_request_` adds an unredirected `Host`. Initial
cache eligibility still forbids caller-supplied Host; after transport, only the
exact allowlisted origin Host in `unredirected_hdrs` is accepted, with no Host in
regular headers. Unknown headers, auth, cookies and changed Host still reject
publication. A direct tail-to-leading regression requires one network-terminal
call, one cache file and original/hit receipts. The experiment compares every
report field and consumed source hash to the
original fixture, and verifies 32 requests when disabled versus 24 on POSIX when
enabled for fixed source bytes. Full report equality in this fixture covers all risk decisions and monetary/slippage
proxies. These two replay modules have no executed orders or fee calculation;
there is no fabricated fee saving or fill claim. Original source hashes and raw
response hashes remain equal for this fixed source. Separate A/B source-revision
tests explicitly prove that enabled hits can differ from a later uncached revision.
Pipe barriers force two original requests and real publication-lock contention;
the different returned hashes are retained in each consumer's receipts. Cache
source metadata lives in the envelope and consumer journal events; report `as_of_utc` still means replay
execution time and is never presented as a new market-data fetch.

Expected complete report hashes for this fixed synthetic input:

| Report | SHA-256 (sorted compact JSON) |
| --- | --- |
| Tail | `f06a5c879f3b7ee6048f7f8bc27debd6939652f3d741c464f610fafac015f620` |
| Leading | `73bce6d5c0c49e4268d68c63381255643f2c0a5f9f329ca8ae71d1dc17148290` |

No wall-clock percentage or three-second full-cycle claim follows from request
deduplication. Production tail 12.083s / leading 37.829s are prior observations,
not measurements of this disabled patch.
