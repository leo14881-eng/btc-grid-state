# Research primary/backup admission and timing audit

Inspection main is fixed at `44327057a565e86faa6eb62465504e34c6fc0b6d`.
The checkout is a depth-one fetch in a separate task directory and branch;
no AGENTS.md, .agents instructions, repository skills, or dedicated test guide
were present in the tracked tree. The relevant validation instructions were
read from the schedule validation workflow and Research lock/rebind tests.

This is an investigation, not acceptance of a runtime repair. No production
code, ledger, permission, schedule, strategy, or risk limit was changed.
SHADOW ONLY, real trading false, NONE_SHADOW_ONLY, and V2 20000 remain required.
PR98/35 and WebSocket PR119/120 are untouched.

## Confirmed duplicate execution

[Actions run 38269372446](https://github.com/leo14881-eng/btc-grid-state/actions/runs/38269372446)
was created at 15:19:21 UTC on October 12 and completed at 15:22:58 (217 seconds).
Its research job ran 15:19:43–15:22:58 (195 seconds). Actual log output, excluding
echoed script text and test fixture markers, includes:

- 15:19:49.460621: DOWNSTREAM_BOUND_GENERATION `20261012T151714726991Z`.
- 15:22:17.802230: HUNTER_RESEARCH_REBOUND `cd74205df7251d15994171446482a36704150442` for that generation.
- 15:22:30.080409: HUNTER_CLOSED_LOOP_ACCEPTED, BUY [], V1 175, V2 8.
- 15:22:44.523522: HUNTER_MAIN_READBACK_E2E_OK, V2 used 12000.0.

[Vultr failure receipt](https://github.com/leo14881-eng/btc-grid-state/blob/3a0079f70d1eac89f64a32f3aa033f604956f4b6/research/results/hunter-runtime-research-health.json)
records the **same generation**, starting 15:17:54.871487 and ending
15:23:21.797224, 326.926 seconds. Its reported source SHA is
`d4c71bb93221e1272d9b5a2185c9d16338240c4e` (also the Actions event head SHA).
At 15:23:17.181891 it reached bind; `lock_wait_seconds=0.000233`.
The bind step failed after 4.615 seconds with RESEARCH_INPUT_CHANGED, listing
18 protected Research output files. It performed no later shadow execution or
persistence, and `main_readback_verified=false`. The rejection preserved the
current state; it is not evidence that the duplicate computation was prevented.

## Actual scheduling path

- `hunter-discovery.timer` requests Discovery hourly at :17 UTC, with 5-second
  accuracy. Discovery's ExecStartPost requests hunter-research.service.
- The launcher obtains `/run/lock/hunter-job-research.lock` locally before
  clone/fetch/checkout. This suppresses another local Research launcher only.
- `hunter_job_runner.run_job` selects only the workflow's research job. Vultr
  therefore does not execute its separate GitHub runtime/bind jobs.
- GitHub runtime gate skips only after recent, verified Vultr success for the
  current discovery generation. While Vultr computes a new generation, its
  previous generation's SUCCESS is deliberately insufficient; backup is allowed.
- GitHub generation admission skips an already aligned health document. It has
  no atomic in-progress claim. The Actions concurrency group serializes GitHub
  jobs, not the Vultr process.
- The Vultr portfolio flock begins only at late bind. GitHub executes that
  workflow step directly, without the runner's local lock wrapper. Filesystem
  flock on one host cannot establish ownership on the other host.
- Rebind and final CAS reject protected changes. They prevent stale publication,
  but are reached after full validation and expensive Research work.

Both schedulers can consequently pass their respective starting conditions and
compute the same generation. A later successful generation does not change this
code path.

## Measurements and their boundaries

| Source, UTC Oct 12 | Runner through health construction | Full validation | Research before later shadow tests | Later shadow tests | Local lock wait |
| --- | ---: | ---: | ---: | ---: | ---: |
| Historical 00:28 success | 380.962 | 110.289 | 174.512 | 0.310 | 32.193304 |
| 15:23 duplicate failure | 326.926 | 132.669 | 186.558 | 0.306 | 0.000233 |
| 20:24 subsequent success | 339.568 | 125.351 | 146.771 | 0.290 | 0.000293 |

All values are seconds. Research here uses the same explicit boundaries for each
receipt: sum from the signals step through official Bybit execution channels,
excluding the later shadow-test step and bind. The old approximately 175-second
timeline is consistent, but its 174.893 value had a different boundary/overhead.

For the actual Actions research job, full validation was 76 seconds, signals
through official channels 70 seconds, and bind through persist/readback 29 seconds
(step timestamps have one-second resolution). These are observations on different
hosts and generations, **not an A/B performance comparison**.

The 15:23 failure spent 319.227 seconds in full validation plus Research. Its
signals step alone was 73.695 seconds, versus 29.389 seconds in the 20:24 record;
these logs do not allocate network, CPU, or throttling costs. Lock waiting was
negligible in this incident. The full validation gates are retained.

[Old 00:28 receipt](https://github.com/leo14881-eng/btc-grid-state/blob/3cdf738e2050c3195e3ce35e83d12a699abda7c1/research/results/hunter-runtime-research-health.json)
verifies 110.289 seconds of validation and 32.193304 seconds waiting. UTC interval
from bind start to readback completion, minus recorded monotonic lock wait, gives
62.063484 seconds; this is a critical-section proxy, not a captured unlock event.
The 20:24 proxy is 65.283249 seconds. Actual release timestamps need journal events.

The historic 182.172-second startup and 563.133-second service-to-readback totals
depend on a service-start journal timestamp supplied to PR111's earlier task.
The receipt alone cannot independently verify it. No current service journal was
available here. Current launcher total, clone/fetch/checkout allocation, and
health-publication duration remain unknown. Do not label the health duration as
the full service cycle or use those historic figures as current performance.

## Reuse existing work and coordinate ownership

[PR111](https://github.com/leo14881-eng/btc-grid-state/pull/111), head
`d130fab0cb2ff6f21009f4db6791d82f8a252c12`, remains draft. Its dedicated
[timing/receipt CI](https://github.com/leo14881-eng/btc-grid-state/actions/runs/38192031374)
explicitly tests that candidate head; other PR workflows may use synthetic merge
refs. Its instrumentation addresses the missing startup/publication measurements.
Receipt observation is default off and does not skip the full suite. Prefer
continuing that PR, not introducing another timing implementation.

[PR112](https://github.com/leo14881-eng/btc-grid-state/pull/112), head
`93ea2c343933b73c6d22a25f43906b54bd375440`, is currently open **and not draft**.
The API confirms three successful check runs associated with this head. Its body
correctly identifies tested merge SHA `8ee363ea2fa8494a7d26e0342abf9a71279fef42`;
these are not proof of testing the exact candidate tree alone. The existing
fixed-input experiment reports 32 to 24 terminal calls with complete report
equivalence, not a production wall-time gain. The cache is default off and has
no runner wiring. Do not recreate or enable it to claim a duplicate-run fix.

The narrow cross-host fix needs a common, atomic generation admission record
**before** full validation/research, plus publication-time ownership fencing.
Both primary and backup must consult it. An expired owner must not publish after
a replacement has acquired ownership. Keep completed-generation verification,
generation/input identity, forced/manual semantics, failover after owner failure,
all full validation, local portfolio serialization, CAS, and authoritative readback.
A fixed sleep, stale health SUCCESS, or dropping RESEARCH_INPUT_CHANGED paths
does not implement this contract. A precompute recheck can reduce some wasted
work but cannot by itself close the simultaneous-admission race.

Coordinate exclusive ownership of `scripts/hunter_job_runner.py`,
`scripts/hunter_generation_admission.py`, `scripts/hunter_research_rebind.py`, and
`.github/workflows/hunter-research-pipeline.yml` before implementation. PR111
already changes the shared runner. The shared portfolio-lock and Monitor modules
must remain owned by the other task. No writer changes were made in this audit.

Required offline repair acceptance: deterministic primary/backup simultaneous
admission; one expensive computation; owner crash/expiry and safe takeover;
late old owner rejected at publication; discovery supersession; failed validation;
manual force; complete ledger preservation. Benchmark before/after with identical
complete fixtures and all checks, reporting computation count separately from
elapsed time. Production acceptance still needs newly observed scheduled overlap
and full service phase timestamps after an independently approved deployment.

## Local verification and remaining limits

On the pinned main code, Windows/Python 3.10 ran
`python -B -X utf8 -m unittest tests.test_hunter_generation_admission tests.test_hunter_research_rebind -v`:
23 tests in 88.831 seconds, 22 passed, one explicit POSIX-flock skip. This includes
real temporary local Git clone/push/rebind/CAS tests. Initial sandbox attempts
failed creating/pushing fixture repositories; the authorized retry succeeded.
No production Git target was used by these tests. WSL inventory was denied;
no alternative Linux execution or dependency installation was attempted.

The task's `evidence/reproduce_admission.py` separately runs four portable
characterization cases, all passed: previous-generation SUCCESS allows backup,
RUNNING allows backup, two identical admission calls both admit, and verified
current-generation SUCCESS suppresses backup. This confirms existing behavior,
not repaired behavior or atomic concurrent locking.

`evidence/summarize.py` derives the disjoint timing columns from the three pinned
JSON receipts and emits raw receipt SHA-256 values in `timing-summary.json`.
The Actions jobs API response and selected runtime log markers are retained next
to it. No before/after repair benchmark or new exact-head CI exists because no
runtime repair has been made or pushed. The local audit document is the only
repository change; existing PR111/112 heads were left intact pending coordination.
