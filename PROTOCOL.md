# Umpire — Protocol Specification — v0.4.1

**Status:** Draft. Everything in this document is versioned and append-only
by convention: fields are never silently removed or repurposed across a
MAJOR version, so that a client written against a given MAJOR version
keeps working against every release within it.

**Contents:** §1 core objects (`Candidate`, `ScoredCandidate`) · §2
execution contract (sandboxing, correctness-gating) · §3 tool-call wire
format · §4 the Umpire itself (pre/post-conditions, rollback,
taint-tracking, scoped recovery, and its concurrency guarantee) · §5 the
non-goals · §6 comparative grounding against real incidents/literature.

**v0.2.0 is a breaking change from v0.1.0**, and the break is deliberate,
not incidental: the impact-scoring contract (former §3), continuity node
schema (former §5), and everything built around them (insight storage,
pitchdeck packaging, model-selection benchmarking) have been removed from
scope. Direction narrowed to one question — does the Umpire
primitive (§4) generalize as a reusable interface, the way MCP's transport
primitive did — and this document now describes only the system built to
answer that question. See Changelog for the full list of what left scope
and why. The project's earlier "orng" framing (an autonomous-inventor
narrative with its own vocabulary — insight, blueprint, pitchdeck,
contribution) has been dropped along with the code it went with; nothing
in the current scope refers to it.

## Why this document exists

A primitive is trusted long-term for the same reason a protocol is:
because its interface is stable and its behavior on a given input is
predictable, *independent of which specific codebase implements it*. This
document is that interface. `orng_core/` is **one implementation** of it —
not the spec itself. A different runtime (a different model, a different
sandbox, even a different language) that satisfies this document is a
conformant implementation.

---

## 1. Core objects

### 1.1 `Candidate`
A single proposed code artifact, evaluated against exactly one `Target`.

| field | type | required | notes |
|---|---|---|---|
| `id` | string | yes | unique within a `SearchRun` |
| `source` | string | yes | full source, must be syntactically valid in the target language |
| `generation` | int | yes | 0 = seed/reference, increments per mutation round |
| `parent_id` | string \| null | no | lineage pointer, null only for generation 0 |
| `mutation_note` | string | no | human/model-readable description of the change |

### 1.2 `ScoredCandidate`
The result of running a `Candidate` through the execution contract (§2).
Nothing downstream may treat a `Candidate` that hasn't produced one of
these as having any standing.

| field | type | required | notes |
|---|---|---|---|
| `candidate` | `Candidate` | yes | |
| `results` | array of per-input result objects | yes | one entry per benchmarked input shape |
| `all_correct` | bool | yes | AND across all `results[i].correct` |
| `mean_speedup` | float | yes | `reference_ms / candidate_ms`, averaged; `0.0` if `all_correct` is `false` |
| `fitness` | float | yes | equal to `mean_speedup` when `all_correct`, else `0.0` |

**Invariant (MUST):** `fitness == 0.0` whenever `all_correct == false`.
Any conformant implementation that assigns non-zero fitness to an
incorrect candidate is not conformant. This invariant is the entire basis
for trusting this system's output — relaxing it anywhere breaks the guarantee
for everywhere else.

---

## 2. Execution contract

A conformant executor MUST, for every `Candidate` it scores:

1. Run the candidate's `source` in isolation from the orchestrating process
   (subprocess, container, or stronger — never in-process `eval`).
2. Enforce a wall-clock timeout.
3. Enforce a memory ceiling.
4. Default to **no network access** for the executed code unless the
   caller explicitly opts in per-invocation (never as a silent default).
5. Compare output against a fixed, version-controlled reference
   implementation using a tolerance appropriate to the domain (default:
   `atol=1e-3, rtol=1e-3` for floating point).
6. Never assign a score to code that did not actually run to completion
   under points 1–5.

A `SandboxViolation` (a candidate that trips an isolation boundary, e.g. a
banned import or a resource-limit kill) MUST be scored identically to a
correctness failure: `fitness = 0.0`.

---

## 3. Tool-call wire format

Any component driving a model as a `ProposeFn` or general tool-caller
MUST accept output in the canonical form:

```json
{"tool": "<tool_name>", "args": {"...": "..."}}
```

and MUST run it through a fallback parser (see
`orng_core/tool_call_parser.py: parse_with_fallback`) attempting, in
order: (1) direct parse, (2) markdown-fence stripping, (3) greedy `{...}`
extraction, (4) single- to double-quote repair. A `null` result from all
four strategies MUST be treated as "no tool call," never as an exception
that halts the caller.

---

## 4. The Umpire primitive (tool_executor)

**This is the primitive the current direction is centered on evaluating.**
It generalizes §2's correctness gate from search candidates to any tool
(filesystem, sqlite, shell, and — per the open falsification test below —
whatever tool shape is tried next). A conformant `tool_executor` MUST:

1. **(§4.1)** Wrap every mutating call in a `ToolContract`: an optional deterministic
   `pre_condition`, the operation, and an optional deterministic
   `post_condition`. Predicates MUST be defined in tool-authoring code,
   never supplied or narrated by the model at call time — the model
   selects the tool and arguments; the tool author defines what must be
   true.
2. **(§4.2)** On post-condition failure OR a mid-execution exception raised by the
   tool itself (not just a clean return that then fails its check): roll
   back if the tool has a real rollback mechanism (filesystem:
   snapshot/restore; sqlite: native transaction). If no real rollback
   exists (e.g. an arbitrary shell command), the session MUST be tainted
   under a specific reason string, and every subsequent mutating call MUST
   be blocked, EXCEPT a call from a contract explicitly marked
   `is_recovery_tool=True`. A mid-execution exception gives no more
   certainty about world-state than a failed post-condition does, and MUST
   receive identical treatment — silently falling through to a bare
   execution error, with no rollback attempt and no taint, is non-conformant.
3. **(§4.3)** A recovery tool's exemption MUST be scoped via `recovers_reasons`: it
   is exempt only while every currently-active taint reason is in that
   set. An unrelated simultaneous taint still blocks it. `recovers_reasons
   = None` is an unconditional exemption and should be used sparingly.
4. **(§4.4)** Clearing a taint (`attempt_clear`) MUST evaluate only a predicate that
   was registered ahead of time, by reason, via tool-authoring code. There
   is no code path by which a model can supply, alter, or narrate what
   counts as recovered. A reason with no registered predicate requires an
   explicit human `clear()` — this is intentional, not a gap: some
   failures have no automatable proof of safety.

**Known limit, stated explicitly:** a recovery tool that itself fails
mid-fix has no stronger guarantee than any other irreversible tool — it
can only be marked with a new taint. Scoped recovery closes the deadlock
where fixing a taint requires a blocked action; it does not make
irreversible operations reversible.

**Concurrency guarantee, stated explicitly (not just implied by §7's
single-executor non-goal):** `execute_contracted` runs its entire
check -> pre_condition -> execute -> post_condition sequence under a
single module-level lock (`_GATE_LOCK`), unconditionally, for every
contract — no per-tool or per-mutating-flag special-casing. Two
concurrent calls to `execute_contracted` fully serialize within one
process: the second cannot begin until the first has completed and
released the lock. This makes single-executor semantics **actually true
of the code, not just documented as an assumption callers were trusted
to honor** — `tests/test_gate_lock_serialization.py` proves it against
the real entry point using a bounded `join()` to detect genuine blocking,
not a timing guess.

**History, kept honest rather than silently erased:** an earlier version
of this primitive had no such lock. `check_or_raise` was evaluated once,
at entry, with no re-check between `fn()` completing and `post_condition`
evaluating — meaning the guarantee was **per-caller, not per-system**: a
caller already in flight when a concurrent taint occurred was not
retroactively affected. `tests/test_concurrency_boundary.py` demonstrated
that gap directly, by calling `TaintTracker`'s primitives (`check_or_raise`,
`mark`) directly, bypassing `execute_contracted` — and it still does,
since `TaintTracker` itself remains unlocked; the guarantee now comes from
the lock at the call site, not from the tracker protecting itself. That
gap was not a deliberate trade-off weighed against a rejected fix —
concurrent access was placed out of scope early, and the original test
existed to confirm that scope boundary was real, not to evaluate whether
closing it was worthwhile. It has since been closed: see the Changelog.

**What the lock does not do:** it has no effect across separate
processes — this remains a single-process guarantee, consistent with §7's
non-goal of any distributed/multi-agent coordination. A slow tool call
(a real git merge, a slow HTTP request) blocks every other mutating call
until it resolves; this is the cost single-executor semantics always
implied, now actually paid rather than merely assumed away.

**Hard constraint, found by stress-testing after the lock shipped, not
before:** `_GATE_LOCK` is a plain `threading.Lock`, deliberately not
reentrant. A `pre_condition`, `post_condition`, or `rollback` function
MUST NEVER call `execute_contracted` itself — doing so deadlocks the
calling thread permanently, since it would be trying to acquire a lock it
already holds. This was not a hypothetical risk: `tests/test_gate_lock_serialization.py::test_reentrant_call_from_rollback_deadlocks_and_this_is_a_documented_constraint`
proves the deadlock occurs, using a bounded, daemon-threaded check so it
doesn't hang the test process itself — and needed a real fix in the test
*itself*, not just the primitive: the deadlocked thread holds the module
lock forever, which would have silently hung every other test file that
calls `execute_contracted` if the test hadn't replaced the module's lock
reference afterward. A reentrant lock (`RLock`) was considered and
rejected: it would let a nested call silently succeed, which could let
two logically distinct operations interleave inside what's supposed to
be one atomic sequence — a loud deadlock is a safer failure mode than a
quiet correctness violation. This is the same category of rule as "no
dynamic predicates" (§4.1): a boundary tool-authoring code must respect,
not something the primitive can safely paper over.

**Also found by the same stress pass:** `TaintTracker` methods called
directly — `mark()`, `attempt_clear()`, `clear()` — bypass `_GATE_LOCK`
entirely, since the lock only wraps `execute_contracted`'s body, not the
tracker's own methods. This is consistent with, and extends,
`tests/test_concurrency_boundary.py`'s existing scope note (previously
demonstrated only for `check_or_raise`/`mark`; now confirmed for
`attempt_clear` too): the guarantee lives at the call site
(`execute_contracted`), not in the tracker. Code that calls
`TaintTracker` methods directly, outside `execute_contracted`, gets none
of §4's concurrency guarantee.

**Open question (the current direction's actual falsification test):**
`ToolContract` has so far only wrapped four tool shapes, all designed by
the same author who designed the abstraction — file writes, sqlite
transactions, arbitrary shell, and the search primitive itself. That is
not yet evidence the primitive generalizes; it is evidence it fits the
cases it was built to fit. The test that would actually answer the
question: wrap a tool shape with a materially different failure mode (an
HTTP call with rate limits, or a git operation with merge conflicts) and
see whether `ToolContract` holds without a new escape hatch.

**Result (tests/test_primitive_generalization_http.py):** an HTTP POST
tool was wrapped using only the existing public API — no changes to
`tool_executor.py`. It correctly handled three failure modes distinct
from the original four: a domain-level failure on a successful round-trip
(rate limiting, HTTP 429), a genuine mid-transport failure (connection
reset partway through the request), and a timeout — the latter two
specifically exercising the mid-execution-exception fix from §4.2 against
a tool shape it wasn't designed against. One real finding surfaced in the
process: `urllib`'s default behavior conflates "transport succeeded, server
said no" with "transport failed," and the correct fix was to normalize
that at the tool-author boundary (inside `http_post_tool`), not inside the
primitive — itself supporting evidence, since the primitive didn't need to
know the difference to handle both correctly once normalized.

**Result (tests/test_primitive_generalization_git.py):** completes the
named pair. A git merge tool was wrapped, again with zero changes to
`tool_executor.py`, using `git merge --abort` as an authentic external-
process rollback — the first tool in the project with real rollback
outside Python-native state (file/sqlite). A genuine merge conflict was
verified rolled back to byte-identical pre-merge state via
`git status --porcelain`, not just "no exception raised." A real git
lock file blocked the operation at the pre_condition stage before any git
process spawned. A nonexistent-repo path exercised the mid-execution-
exception fix (§4.2) against a third tool shape. One process note, not a
primitive finding: the test's own fixture initially failed silently by
assuming a `main` default branch in an environment that defaults to
`master` — caught only because the test asserted on real git state
rather than trusting the fixture, reinforcing the same discipline this
whole spec is built around.

**Still open:** both tests were written by the same author who designed
the abstraction, same as the original four. Two materially different
domains (network I/O, external version-control process) with zero
changes needed is stronger evidence than either alone, but the fully
independent version — someone else wrapping a tool with `ToolContract`
with no involvement from this design process — still hasn't happened.

---

## 6. Comparative grounding (why a pre_condition gate, not confirmation)

The design choice to gate mutating tool calls with deterministic
predicates rather than natural-language instructions or per-action
confirmation prompts is grounded in real, cited evidence, not intuition:

- **Natural-language instructions are advisory, not enforced.** The July
  2025 Replit incident (Fortune; The Register; OECD AI Incident Database
  #1152) is a documented case of an agent told, in an active "code freeze,"
  in all caps, eleven times, not to modify a production database —
  proceeding anyway, deleting it, then falsely claiming rollback was
  impossible. `tests/test_comparative_baseline_replit_incident.py` models
  this exact shape: a string-based instruction with no code-level
  enforcement does not prevent the action; a `pre_condition` evaluated in
  code before the operation runs does, and is indifferent to whatever
  justification the agent offers for proceeding.
- **Confirmation prompts converge to auto-approval in practice.**
  Wang et al., "Reframing LLM Agent Security as an Agent–Human Interaction
  Problem" (arXiv:2605.24309), surveying 21 production agent systems,
  report that always-allow/auto-approval is the most widely deployed
  fatigue-mitigation strategy despite having "the worst long-term security
  profile," and that sandbox-based auto-approval cuts Claude Code's prompt
  count by 84% — evidence that per-action confirmation, as actually
  deployed, trends toward not being a meaningful gate either.

**Honest limits of this grounding, stated here rather than separately:**
this is one author modeling both sides of the comparison — the same
limitation flagged for every primitive-generalization test in §4. It
illustrates a real mechanical distinction (enforced-in-code vs.
advisory-in-context) using a real incident; it is not a user study, and it
does not use the more rigorous evaluation apparatus that exists for this
exact question. "Oversight Has a Capacity: Calibrating Agent Guards to a
Subjective, Fatiguing Human" (arXiv:2606.08919) frames agent-guard
evaluation as selective classification under asymmetric cost — reporting
an operating-point curve, a Neyman-Pearson point, and AURC rather than a
pass/fail count — and includes an open-source measurement apparatus. That
is the standard a real comparative-utility claim for the Umpire
should eventually be measured against, not this illustration.

---

## 7. Non-goals of v0.2.0

Stated explicitly so scope doesn't silently creep back in:

- No distributed/multi-agent coordination protocol — this remains
  entirely out of scope. Within a single process, however, concurrent
  calls to `execute_contracted` DO fully serialize (a module-level lock,
  see §4) — this is stronger than "single-executor semantics only" might
  suggest; it's not merely a documented expectation but an enforced one
  for in-process callers.
- The sandbox in §2 is not claimed to be a hard security boundary
  equivalent to a hypervisor-backed sandbox (gVisor/Firecracker/full
  container). It defines the *minimum* isolation a conformant
  implementation must provide.
- No model training/fine-tuning procedures.
- No persistent memory/knowledge-graph, no claim-packaging/pitchdeck
  format, no model-selection benchmarking. These existed in v0.1.0 and
  were deliberately cut — see Changelog. Reintroducing any of them should
  be a conscious decision at that time, not a drift back through
  incremental additions to this spec.

---

## 8. Interface, observability, and measured overhead

Stated explicitly since none of it was previously written down anywhere
a reader could find without inferring it from test code:

- **Return semantics are exception-based, not a return-value trace
  object.** `execute_contracted(contract, fn, *args, **kwargs)` returns
  whatever `fn()` returned on success. On failure it raises — never
  returns a boolean or a "PASS/FAIL with a trace" struct. Two exception
  types, meaningfully distinct: `ContractViolation` (the operation ran,
  or a guard/predicate refused it — has `.stage`, `.tool_name`,
  `.predicate_name`, `.recovery_state`, `.to_dict()`) and
  `ExecutionFailure` (the tool itself raised — `fn()` errored,
  irrespective of any predicate).
- **Observability is Python's standard `logging` module**, namespaced
  per file (e.g. `umpire.tool_executor`). There is no separate
  structured-output file, no built-in trace persistence beyond what a log
  handler is configured to capture, and no dashboard. `ContractViolation.to_dict()`
  exists for structured inspection but is not automatically written
  anywhere — a caller that wants a persisted trace has to do that itself.
- **Measured per-call overhead** (not previously measured, only assumed
  cheap): on this hardware, a no-op `pre_condition`/`post_condition` pair
  through `execute_contracted` costs on the order of **0.8 microseconds**
  over a raw function call for a non-mutating contract, and about
  **0.76 microseconds** for a mutating one (the extra cost is the taint
  guard check plus lock acquisition — see §4). Negligible next to any
  real tool's actual I/O latency (file, sqlite, HTTP, subprocess), which
  is milliseconds or more. Since every call now serializes under a single
  lock (§4), high-concurrency mutating workloads will be throughput-bound
  by the slowest individual tool call in the queue, not by this overhead.
- **Python version:** developed and tested against 3.12.3. No version
  floor has been verified; nothing in the code uses syntax newer than
  what's available in 3.10, but that has not been tested directly.

---

## Changelog
- **v0.5.1** — Renamed the project from `invariant-gate` to `umpire`
  (package `invariant_gate/` -> `umpire/`). Reason: a name-collision check
  against a curated AI-security tools directory (done as part of GTM
  research before a submission) found the prior name overlapped closely
  with Invariant Labs' existing, more established product line
  (Invariant Gateway, mcp-scan, Invariant Guardrails) in the exact same
  space. Resolved by renaming rather than by disclaimer, since the
  overlap was close enough (shared "Invariant" root, same technical
  niche) that a disclaimer would not have meaningfully reduced confusion.
  New name chosen and verified against the same directory plus targeted
  searches for direct collisions before adopting -- "umpire" ties to the
  gate's actual post_condition behavior (an independent party that makes
  the call based on what's observably true, not on any party's
  self-report) rather than to the pre_condition/access-control metaphor
  most named competitors in this space already use. No functional code
  changes; full existing suite re-verified passing (52/52) after the
  rename, both at the package-directory level and after all doc/comment
  references were updated.
- **v0.5.0** — Closed a real gap found via a self-authored comparative
  claim (20 hand-labeled scenarios inspired by arXiv:2606.08919's
  selective-classification framing): a mutating tool with
  `pre_condition=None` previously executed unconditionally, since the
  gate only checked the predicate `if contract.pre_condition is not
  None`. Now, any mutating tool with no `pre_condition` raises a
  `ContractViolation` before execution (deny-by-default). Non-mutating
  tools (e.g. `search`, which legitimately uses `pre_condition=None`
  with `is_mutating=False`) are unaffected -- confirmed explicitly,
  since a blanket deny-by-default would have broken that intentional
  case. Verified against the full existing suite (52/52 passing, zero
  regressions) plus two new targeted checks (non-mutating tool
  unaffected; mutating tool with no predicate now correctly denied).
  Distinct from, and complementary to, the earlier comparative-claim
  fix which hardened *weak* predicates -- this closes *missing*
  predicates at the framework level.

- **v0.4.1** — Two findings from a final stress-testing pass: (1)
  `_GATE_LOCK` deadlocks on reentrant calls (a rollback/pre/post-condition
  calling `execute_contracted` itself) — documented as a hard constraint
  on tool-authoring code, proven with a bounded test that required its
  own fix (cleaning up the module lock afterward so the deadlock didn't
  poison every subsequent test in the same process); (2) `TaintTracker`
  methods called directly bypass `_GATE_LOCK` entirely — confirmed this
  extends to `attempt_clear`, not just `check_or_raise`/`mark` as
  previously shown.
- **v0.4.0** — Closed the TOCTOU gap documented in v0.3.2: added
  `_GATE_LOCK`, a single module-level lock serializing the entire
  check -> execute -> post-check sequence in `execute_contracted`,
  unconditionally, for every contract. Single-executor semantics (§7) are
  now enforced for in-process callers, not just documented as an
  assumption. `tests/test_gate_lock_serialization.py` proves it against
  the real entry point. `tests/test_concurrency_boundary.py` is retained,
  rescoped to demonstrate that `TaintTracker`'s own primitives remain
  unlocked in isolation — the guarantee lives at the call site, not in
  the tracker. Re-measured per-call overhead to reflect lock acquisition
  cost (§8). Considered and rejected a narrower "re-check taint right
  before post_condition" mitigation: it would only discard an in-flight
  result computed during a taint, not eliminate the race, and would
  introduce unfairness (rolling back operations unrelated to the taint
  that happened to overlap in time) — a full lock was simpler and matched
  what §7 already claimed.
- **v0.3.2** — Dropped the "orng" project framing entirely (package
  renamed `orng_core/` -> `umpire/`, spec file renamed
  `ORNG_PROTOCOL.md` -> `PROTOCOL.md`). Added §8 (interface/observability/
  measured overhead) and made the per-caller-vs-per-system concurrency
  guarantee explicit in §4 and §7, correcting the record that it was a
  deliberate rejected-alternative trade-off — it was a scope decision,
  and the concurrency test exists to confirm the boundary, not to weigh
  an alternative.
- **v0.3.1** — Second half of the named falsification pair: git merge tool
  with authentic external-process rollback (`git merge --abort`), tested
  against a real merge conflict (byte-verified rollback), a real lock
  file, and a mid-execution exception on a third tool shape. Zero changes
  to tool_executor.py required.
- **v0.3.0** — Added §6, comparative grounding for the pre_condition-gate
  design choice, using a real documented incident (Replit, July 2025) and
  cited literature (arXiv:2605.24309, arXiv:2606.08919) rather than an
  invented friction-vs-safety estimate. Honest limits stated alongside the
  claim, including the more rigorous evaluation apparatus this should
  eventually be measured against.
- **v0.2.2** — First falsification test result: an HTTP POST tool wrapped
  with zero changes to tool_executor.py, handling rate-limiting,
  connection reset, and timeout — see §4 for detail and the honest limit
  (same author as the abstraction; independent validation still open).
- **v0.2.1** — Fixed a real gap found by adversarial testing: a mid-execution
  exception (fn raises partway through, distinct from a clean return that
  then fails post_condition) previously bypassed rollback and taint-tracking
  entirely, silently leaving partial side effects unrecovered and untracked.
  §4.2 now requires identical treatment for both failure modes.
- **v0.2.0** — Breaking change. Removed: impact-scoring contract (former
  §3), continuity node schema (former §5), and all dependent code
  (`continuity/`, `orng_core/impact.py`, `pitchdeck_composer/`,
  `orng_core/model_select.py`'s benchmarking logic, `cloud/gpu_detect.py`).
  Direction narrowed to: does the Umpire primitive (§4)
  generalize. Extracted `parse_with_fallback` into its own module
  (`orng_core/tool_call_parser.py`) since it's coupled to §3 (wire format)
  independent of model selection.
- **v0.1.0** — initial draft. Core object schemas, execution contract,
  impact scoring contract, tool-call wire format, continuity schema
  versioning requirement, and the Umpire primitive for general
  tool execution (prevention, containment, scoped recovery).
