# Hunter timing and validation receipt observation

This proposal is SHADOW ONLY. It does not change strategy, thresholds, the
20,000/17,000/3,000 capital allocation, ledgers, scheduling order, writer locks,
generation admission, CAS, or authoritative readback. It does not enable reuse,
deploy anything, or change permissions. PR98/35 and historical request deduplication
are outside scope. The implementation has **no test-skipping mode**.

## Baseline and production evidence

Work started from latest `origin/main` at
`272903bdde8c69866e8e9901cc7fb6f20bc42b1b`, including merged SEC PR110.
The fixed [research health source](https://github.com/leo14881-eng/btc-grid-state/blob/3cdf738e2050c3195e3ce35e83d12a699abda7c1/research/results/hunter-runtime-research-health.json)
reports runner start `2026-10-12T00:22:36.172226Z`, authoritative readback completion
`00:28:57.132602Z`, and health-document construction at `00:28:57.133781Z`.
The old `duration_seconds=380.962` excludes checkout preparation and subsequent
health publication. The user-provided service journal starts at `00:19:34` and
exits at `00:29:03`. These give the observed 182.172-second startup gap and
approximately 563.133 seconds to business readback. The health document alone
does not establish the service start/exit timestamps.

| Observed segment | Seconds | Evidence/limit |
| --- | ---: | --- |
| Service start to Python runner | 182.172 | Journal + health start; clone/fetch/checkout allocation unknown |
| Compile, full tests, state gate | 110.289 | Health step; unittest itself 107.961 s, 1026 tests, one skip |
| Research outside portfolio lock | 174.893 | Supplied timeline; includes many network/data computations |
| Later three shadow test modules | 0.310 | Health step; 78 tests, unittest itself 0.086 s |
| Portfolio lock wait | 32.193304 | Existing lock instrumentation |
| Critical section | 62.064 | Supplied timeline; preserved |

Monitor's existing prepare timer combines clone/fetch/checkout and its selected
tests before the lock. The reported 111–112 seconds cannot be attributed to tests
alone. Monitor is assessed here but its launcher and sequencing are unchanged.

## Measurement boundaries

`hunter_scheduled_job.sh` emits fixed-name START/END events for admission, clone,
fetch, checkout, watchdog health (where applicable), runner and cleanup, including
retry cleanup. Each elapsed value uses `time.monotonic_ns()` from the system
interpreter. The phase timer includes the tiny second clock-process launch cost;
it does not claim instruction-level profiling. START without END identifies an
interrupted phase. Total scope is launcher entry through cleanup, not the earlier
systemd service-start event. Uninstrumented setup can be derived from total minus
phases. Phase logs contain no argv, remote URL, email, checkout path or environment.
Existing child output keeps its existing routing; runner diagnostic tails also
redact emails. These additions do not assert all legacy stdout is sanitized.

The legacy UTC health duration keeps its existing meaning and is now explicitly
labelled. A separate monotonic field measures runner work before health publication.
`HUNTER_HEALTH_PUBLICATION_END` measures local-receipt construction plus GitHub
health publication and its readback, including unsuccessful attempts. Its elapsed
time cannot be put into the same already-published JSON without another publish;
the journal is the source for that interval. `HUNTER_RUNNER_TOTAL` includes health
publication and excludes launcher preparation. Publication success and business
success remain distinct. UTC timestamps support cross-process correlation;
monotonic deltas measure elapsed time.

## Dependency audit and eligible closure

[The audit inventory](hunter-validation-dependency-audit.json) lists every current
`test_hunter*.py` module, imports, test-definition counts and direct I/O, environment,
clock, fixture, dynamic-import and subprocess review locations. These static hints
are **not a proof** that unmarked tests are pure. Every module is denied eligibility
by default. Only two complete source/test closures were manually read and frozen:

* `test_hunter_execution_cost.py` and `hunter_execution_cost.py`: three deterministic
  arithmetic tests, inline books, `math` and stdlib test/import machinery.
* `test_hunter_v1_coverage.py` and `hunter_v1_coverage.py`: four deterministic
  partition/guard tests, inline data, `copy` and stdlib unittest.

The remaining tests still run. In particular the later `test_hunter_shadow_trader`,
`test_hunter_shadow_trader_v2`, `test_hunter_shadow_optimizer` invocation is retained.
V1/V2 import `hunter_policy`, which reads `research/results/hunter-shadow-rules.json`
at import time. V2 also reads `HUNTER_SHADOW_FREEZE`, `HUNTER_BINANCE_API` and
`HUNTER_OPPORTUNITY_BACKFILL_BUDGET`; tests use wall time and transitively import
other engines. Optimizer's tested metrics function is pure, but that does not
certify the combined three-module invocation. The full discover pass also includes
filesystem, process, locking, fixture and environment-dependent tests. Broad reuse
would require further dependency proofs and separate approval.

The full workflow's compile step, complete unittest discovery and required-current
JSON/capital check execute first, unchanged. An optional observation is attached
after success. Every subsequent market freshness, identity, capital, generation,
SingleWriter, SHADOW, ledger, rebind, CAS and readback guard still runs every time.
An original failure stops the pipeline before observation. A comparison test failure
also fails the step; a hit cannot override it.

## Content address and trust model

The checked-in manifest is an explicit stdlib-only dependency lock and records
SHA-256 hashes of all four reviewed source/test files. A change requires re-audit;
it does not silently certify new imports. The digest additionally covers the
manifest, receipt implementation, fixture tree (conservatively), actual interpreter,
stdlib and extension contents, loaded native libraries, Python build/ABI, kernel,
architecture, libc version, isolated command flags and the complete child environment.
The child runs `-I -S -B`, with only `TZ=UTC` and `LC_ALL=C.UTF-8`, no site packages or
inherited credentials. A fresh empty `-X pycache_prefix` also prevents reading
pre-existing untracked bytecode (`-B` alone only prevents writes).
Added package initializers, missing inputs and symlinks
invalidate eligibility. Editable/pip dependencies are excluded by isolation, not
assumed locked; existing full-suite aiohttp requirements remain range-pinned and
are one reason the full suite is not eligible. No `main` SHA is used as a substitute
for content. Dynamic ledger changes do not invalidate these seven pure tests, but
cannot bypass a current-state gate.

Each bounded JSON receipt authenticates schema, fingerprint, exact successful count,
boot ID and monotonic issue time with HMAC-SHA256. Expiry is 24 hours within the same
boot; future timestamps and previous boots miss. The signing key must be separately
provisioned: 32 bytes, owned by the current uid, regular 0600 file in a private 0700
directory. The receipt directory must also already be 0700 and current-uid-owned.
Every path component is opened with `O_NOFOLLOW`; writable parents are rejected
except root-owned sticky temporary parents. Files must have one link, correct owner,
permissions and bounded size. No chmod, permission repair or production provisioning
is performed. Windows/unknown permission models fall back.

The trusted boundary is the OS account plus key and reviewed checkout. This is not
protection against a compromised process running as that same account or root,
which can read the key and change the verifier. A writable `SUCCESS` string is
never sufficient. Neither key nor raw environment is written into logs or receipts.

A nonblocking file lock serializes comparison and atomic publication. Data is
fsynced then renamed within the open directory descriptor. Sixteen content-addressed
slots bound receipts to 64 KiB plus bounded marker/lock/pending overhead. Replacing
only an authenticated slot performs bounded eviction; there is no recursive cleanup
or deletion of user files. Foreign content and interrupted `.pending` files cause
fallback and are left untouched. A failed comparison writes an authenticated
tombstone so an older success is not reused. Storage failure cannot suppress a
comparison failure.

## Modes and rollout

Default `HUNTER_VALIDATION_RECEIPTS=off` does not inspect the store. `read-only`
looks up an already provisioned receipt and writes nothing. `compare` always runs
the isolated seven tests, including on a hit, and records only exact success after
unchanged-input revalidation. Both optional modes use
`HUNTER_VALIDATION_RECEIPT_DIR` and `HUNTER_VALIDATION_RECEIPT_KEY`.
Missing/corrupt/untrusted/unsafe/expired/unmatched/incomplete receipts, lock contention
and unavailable dependency identity fall back to the original full validation.
Observation records always say `tests_skipped=0` and
`production_health_evidence=false`. No workflow or deployment config sets these modes.

Seven fast tests cannot plausibly remove the observed 110 seconds. Fingerprinting
and comparison add overhead. This stage verifies observability and protocol safety;
it is not an enabled performance optimization. Actual reuse, a broader eligible
group or a clone architecture change requires new review and approval.

## Verification and performance reporting

The dedicated read-only GitHub Linux workflow runs launcher/runner contracts,
receipt adversarial cases, the full Hunter discovery suite nine times on identical
checkout inputs, and scheduler/Bybit Node regressions. It never invokes production
services, trading engines or publication jobs. `/usr/bin/time` captures elapsed
time and command peak RSS. The artifact includes every sample and nearest-rank
median/P95/P99: 30 per pure-mode variant and 3 per full-flow variant. Those small
sample tails, especially n=3, mostly describe observed maxima; they cannot establish
production P95/P99. Cold/hot means receipt absence/presence, not flushed OS caches.
No local simulation is a production performance result. CI results and links are
recorded in the PR after execution.
