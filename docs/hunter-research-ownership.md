# Research generation ownership (candidate; not deployed)

The October 12 primary/backup incident is reconstructed in
[the pinned audit](hunter-research-admission-audit-20261013.md). Both hosts spent
minutes validating and researching the same generation. The eventual protected
input rejection was correct, but it arrived after the duplicated work.

## Protocol

`research/results/hunter-research-ownership.json` is an operational receipt, not
a portfolio or historical ledger. This candidate adds no checked-in runtime
receipt and performs no deployment. It uses the existing main write capability;
no new token scope, branch permission, filesystem permission, server, daemon,
timer, or scheduler setting is required.

Both Actions bind and the server runner acquire before the full validation and
Research steps. Each fetches a fixed main commit and constructs a child commit
containing only the claim through a private Git index. A normal fast-forward push
to main is the compare-and-swap operation. Two hosts starting from the same parent
produce siblings: Git accepts at most one. There is no force push, remote JSON
overwrite assumption, or unfenced claim rebase. Push acknowledgements are checked
by fetching main and verifying ancestry and the claim.

A claim has a random 128-bit owner, exact generation and scan Git blob identity,
an issued time and 1800-second expiry, and state RUNNING, PUBLISHED, or FAILED.
Its shadow authority is fixed to NONE_SHADOW_ONLY and real trading is false.
Malformed receipts fail closed. A live RUNNING claim blocks every other owner,
including manual force and a different generation. A crashed invocation becomes
eligible for takeover at expiry. A valid owner can atomically record FAILED and
permit an earlier fresh retry; the failed record remains in Git history.

The server checks out its claim commit before workflow execution. Actions passes
the owner explicitly from bind to research. Both verify ownership again in the
initial baseline refresh, before any expensive validation. Production rebind and
CAS command entrypoints require ownership; the ownership file cannot be preserved
as a locally generated Research output during rebind.

After all original validation, shadow execution, integrity and acceptance gates,
the result commit includes PUBLISHED for the same owner. The final push helper
requires a still-live RUNNING owner on fetched main, unchanged scan identity, the
exact fetched main as its single parent, and the expected PUBLISHED receipt in
the result commit. A takeover between verification and push produces divergent
commits, so the old result push is rejected. Existing protected-input checks,
Monitor rebind, local portfolio flock, CAS retry checks and authoritative result
readback remain in place.

PUBLISHED means the result transaction was committed; it **does not mean readback
succeeded**. Duplicate publication is explicitly reported without executing the
pipeline or publishing a new successful health receipt. A forced rerun may replace
PUBLISHED, but cannot replace a live owner. Once a receipt exists, the Actions
runtime/bind path consults it even when an older health document is aligned; an
old SUCCESS cannot conceal a later FAILED forced invocation.

## Diagnostic writes are fenced too

A rejected contender must not write a protected runtime-health file and thereby
abort the incumbent's work. Server failure still exits nonzero and retains the
existing local receipt and journal. Only a valid current RUNNING owner can append
FAILED on main. The Actions failure-only tail does the same; an earlier failing
step remains a failure. Expired/lost owners retain their local/Actions evidence
without changing a replacement's main snapshot.

Server success health publication uses the same main-ref CAS and checks the exact
PUBLISHED owner, generation and scan blob on each attempt. If a successor has
already acquired, the old server retains its verified local receipt and logs
`HUNTER_RESEARCH_HEALTH_SUPERSEDED_LOCAL_RECEIPT_RETAINED`. It does not overwrite
the successor's protected inputs or reinterpret its own real readback as failure.
Non-race claim push rejection is explicit, distinct from exhausted CAS contention.

## Tests, cost and limits

`python -B -X utf8 -m unittest tests.test_hunter_research_ownership -v` creates two
small independent checkouts and a local bare remote. It uses real Git pushes for
simultaneous claims (barrier-controlled), crash/expiry takeover, expired execution,
late old-owner results, takeover between check and push, lost acknowledgements,
atomic results/receipt publication, repeated generation, corrected scan identity,
unrelated main advances, failure evidence, and successor-safe health publication.
No test contacts the production remote or invokes trading engines.

The same-input scheduler fixture compares the existing admission predicate's two
computations against one with the claim. It retains complete identical fixture
input, result and history bytes, and reports their hashes and candidate admission
overhead. It is **not a full Research latency benchmark** and does not establish
wall-clock speedup on Vultr. No calculation, data row, risk check or validation
test inside an admitted production invocation is removed. PR111 instrumentation
and PR112's opt-in replay cache remain separate and are not enabled here.

The added PR-only read-permission workflow checks out the exact PR head SHA and
runs admission, ownership, rebind, real Linux locking and runner diagnostics.
Other existing PR workflows may validate GitHub's synthetic merge tree; report
those separately. No manual workflow dispatch is required.

Lease expiry uses client UTC clocks; finite bounded values and future-clock checks
fail closed on malformed receipts. There is no background renewal. Actions queue
time and validation consume the lease. Expiry can permit overlapping computation
if an old owner is merely slow; it cannot let that old owner publish after a
replacement wins the main-ref CAS. A client-side time check also cannot prove the
lease was unexpired at the exact instant the remote received the push. The durable
guarantee is fencing against replacement, not perfect clocks or exactly-once CPU
work across crashes. All current freshness checks still apply after takeover.

Independent review, exact-head CI and approved coordinated deployment remain
necessary. Scheduled primary/backup overlap, whole-service phase timing and
post-deployment readback evidence are still required for production acceptance.
No production recovery or speedup is claimed by this candidate.
