# Hunter offline publication capacity benchmark

`scripts/hunter_lock_capacity.py` measures an isolated **state publication cycle**.
It does not run either complete Research/Monitor strategy, contact market APIs,
make orders, or publish to the repository's real origin. Both worker roles use
the existing monitor publisher to exercise shared locking, latest-state binding,
CAS, one atomic Git commit containing both ledgers, and exact authoritative
readback against a temporary local bare repository.

The source is the seven tracked snapshot files from one resolved commit, including
both portfolio ledgers. The script records the commit SHA, byte sizes and SHA-256
hashes. The source working files are hashed before and after and must be unchanged.
Source Git operations are read-only. Git writes, clones, resets, commits and pushes
are limited to a newly created temporary directory; Git transport is restricted to
local files, and user/system Git settings and hooks are disabled. No primary runtime
health proof is published (`GITHUB_ACTIONS=true` in the isolated workers).

## Run on Linux CI

After checkout and the existing regression suite, run:

```sh
python -m unittest discover -s tests -p 'test_hunter_lock_capacity.py' -v
python scripts/hunter_lock_capacity.py --source-ref HEAD --scales 1 2 5 \
  --iterations 2 --workers 2 --output /tmp/hunter-lock-capacity.json
```

`HEAD` uses the tracked state in the checked-out CI commit. To measure a newer
main snapshot, let the CI checkout/fetch step obtain it first and pass its exact
SHA using `--source-ref`. The benchmark itself never fetches a network remote.
Upload `/tmp/hunter-lock-capacity.json` as an Actions artifact with an `always()`
condition. Keep source SHA, tested code SHA and the CI run URL together in the
acceptance evidence. Running on Linux/POSIX is required for the production lock
helper. Windows intentionally reports that requirement instead of replacing flock.

There is no latency threshold and no timing-based regression assertion. Structural
failures still return nonzero: invalid source contracts, failed workers, changed
source files, inconsistent readback, altered payload, or a wrong commit count.
The worker safety deadline is a harness timeout, not a performance target.

## Payload model and evidence boundary

* **1x** begins with the exact tracked source bytes. Its real source snapshot must
  pass the existing publication validator before the benchmark starts.
* **2x/5x** duplicate the existing `closed_positions`, `events` and `decisions`
  lists in both ledgers. Original rows remain an identical prefix. Summary closed
  counts are adjusted in the temporary copy. Current open positions, tranches,
  capital settings, history record contents and source files are unchanged.
* Factors describe historical row counts, not total bytes: fixed snapshot sections
  are not enlarged. Empty history arrays remain empty; actual byte counts and row
  counts in the JSON show the effective payload size.
* Repeated historical identifiers and transactions are deliberate synthetic load.
  Amplified files are **not valid trading histories**, historical continuity proof,
  strategy replay results, or evidence of a duplicate-trade defect. The publisher's
  validator checks snapshot counts, generations, SHADOW permissions and exposure;
  it does not establish historical uniqueness. No strategy consumes these files.
* Each worker acquires the same production `PortfolioLock`, then fetches and resets
  its disposable checkout to the latest temporary main before parsing and validating
  the current generation. Only generation/revision metadata is changed for a
  publication. No old snapshot is committed after waiting. Every expected commit
  contains the two ledgers and passes real CAS and exact readback.
* Success requires unchanged history/capital payloads and exactly one commit per
  successful simulated cycle. `real_trading_enabled=false` and
  `capital_authority=NONE_SHADOW_ONLY` are recorded in the report.

The fixture contract test follows the actual publication chain: every writer's
base must equal the preceding writer's published SHA, through four contending
publications. The main lock/publisher regression suite owns timeout, process
failure, stale CAS rejection and idempotent repeated-execution tests. This capacity
benchmark does not replace those tests or production runtime acceptance.

## Metrics

The machine JSON records each role's lock wait, hold and total simulated cycle time;
worktree read; JSON parse; latest-state validation; serialization; worktree write;
publication including CAS; Git command totals including commit/push/fetch/rebase;
and readback including fetch/parse/validation. It also includes the helper's audit
receipts, the bound and published generation/SHA, source/expanded sizes, setup time,
payload preparation time and overall scenario time. `parse` and `serialize` measure
the harness transformation; repeated parsing within the publisher is included in
the labeled validation and readback phases.

Timings overlap: publication includes commit and readback; Git command totals also
occur inside the named phases. Do not sum them as independent durations. Lock hold
includes defensive harness payload checks. Scenario time includes setup, process
startup, contention, final integrity checks and temporary cleanup. Local Git transport
does not measure production network latency, complete Git history growth, live market
requests, host contention or the full Research/Monitor end-to-end cycle.

The helper uses kernel blocking flock but does not promise FIFO fairness. Two-worker
samples show observed contention only; they cannot prove freedom from starvation.

## Follow-up storage candidates, outside this change

| Candidate | Expected benefit | Requirements before any authority change |
| --- | --- | --- |
| Keep Git/JSON and shorten the critical section | Smallest operational change; preserves current audit/readback model | Compare measured lock hold with network/preparation and JSON costs |
| Transactional database | Avoid rewriting complete growing ledgers; indexed reads and transactional two-ledger writes | Design and test single-writer/generation CAS, idempotency keys, immutable audit exports, backup/restore and rollback |
| Hot state plus immutable cold history | Reduce frequently parsed and serialized data while retaining all history | Define stable event IDs, hash-linked archive manifests, exact reconstruction, atomic archive/state publication and recovery |

No database or archive is installed or made authoritative by this benchmark.
Storage migration needs separate replay and recovery evidence. The current work
does not remove, truncate or rewrite production history.
