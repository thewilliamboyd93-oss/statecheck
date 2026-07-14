# StateCheck

A hard gate that stops an agent from running a tool unless a
deterministic pre-condition holds — and rolls back or taints the world if
a post-condition fails afterward. No prompts. No self-reports. Code
executing and checking, nothing else.

Read `PROTOCOL.md` for the actual spec — versioned, with a table of
contents. This file is the orientation.

## Quick example

```python
from statecheck.tool_executor import ToolContract, execute_contracted

# Predicates are plain Python callables, written by whoever authors the
# tool — never strings passed in at call time, never something a model
# supplies. This is deliberate: a string evaluated at runtime is exactly
# the kind of dynamic, model-influenceable check this primitive exists
# to rule out.
def repo_is_clean():
    import subprocess
    return subprocess.run(["git", "status", "--porcelain"], cwd="/repo",
                           capture_output=True, text=True).stdout == ""

def push_succeeded(result):
    return result["returncode"] == 0

contract = ToolContract(
    name="git_push",
    pre_condition=repo_is_clean,
    post_condition=push_succeeded,
    is_mutating=True,
)

def do_push():
    import subprocess
    r = subprocess.run(["git", "push", "origin", "main"], cwd="/repo",
                        capture_output=True, text=True)
    return {"returncode": r.returncode}

result = execute_contracted(contract, do_push)
# Returns whatever do_push() returned, on success.
# Raises ContractViolation or ExecutionFailure on failure — see below.
```

## What's here

```
statecheck/
  verifier_search.py    the correctness gate + search loop
                         (propose -> execute -> score -> mutate,
                          sandboxed, fitness=0 if incorrect, full stop)
  tool_executor.py       the StateCheck itself
                         (pre_condition -> execute -> post_condition,
                          real rollback where possible, taint-tracking
                          with scoped recovery where it isn't)
  tool_call_parser.py    robust parsing of a model's {"tool": ..., "args": ...}
                         output — markdown fences, stray prose, single
                         quotes, all handled without guessing

tests/
  test_verifier_search.py                    correctness gate mechanics
  test_sandbox.py                            adversarial code rejection
                                              (real allowlist enforcement)
  test_tool_executor.py                      the Gate's core contract
  test_stress_statecheck.py              a real bug this found and the fix
  test_tool_call_parser.py                   wire-format parsing
  test_concurrency_boundary.py               proves TaintTracker's raw
                                              primitives are unlocked —
                                              why the lock lives at the
                                              call site (see below)
  test_gate_lock_serialization.py            proves execute_contracted
                                              itself fully serializes —
                                              the real, enforced guarantee
  test_primitive_generalization_http.py      does the Gate hold on a new
                                              tool shape? (real sockets)
  test_primitive_generalization_git.py       does it hold on another one?
                                              (real git, real merge conflicts)
  test_comparative_baseline_replit_incident.py
                                              why a deterministic gate beats
                                              a prompt instruction, grounded
                                              in a real 2025 incident
```

52 tests, all exercising real behavior — real subprocess sandboxing, real
sockets, real git repos, real filesystem/sqlite rollback, real thread
serialization. Nothing here is mocked when the real thing was cheap enough to just
run.

## Why it's built this way

The core discipline, stated once so it doesn't need repeating in every
module: **a claim only counts if something executed and checked it.**
Not a plausible-sounding LLM self-report, not a heuristic score, not a
confidence number. `verifier_search.py`'s `fitness == 0.0 whenever
all_correct == false` invariant and `tool_executor.py`'s refusal to let a
model's own return value override a `post_condition` are the same rule
applied in two places.

The predicates that gate a tool call are written by whoever authors the
tool, never by the model at call time. The model picks which tool to call
and with what arguments; it does not get a vote on what counts as success.

## Return values, exceptions, and observability

`execute_contracted(contract, fn, *args, **kwargs)` returns whatever
`fn()` returned, on success — not a boolean, not a "pass/fail with trace"
object. On failure it raises one of two exceptions, meaningfully
distinct: `ContractViolation` (a guard or predicate refused the call, or
it ran but failed its post-condition — carries `.stage`, `.tool_name`,
`.predicate_name`, `.recovery_state`, and `.to_dict()`) or
`ExecutionFailure` (the tool itself raised, independent of any
predicate).

Observability is Python's standard `logging` module, namespaced per file
(`statecheck.tool_executor`, etc.). There's no separate structured
log file or dashboard — a caller that wants a persisted trace calls
`.to_dict()` on a caught `ContractViolation` and writes it out itself.

Measured overhead (not previously measured anywhere, only assumed cheap):
roughly **0.8 microseconds** per call over raw for a non-mutating
contract, **0.76 microseconds** for a mutating one, with no-op
predicates — this now includes lock acquisition (see "Known, stated
limits" below). Negligible against any real tool's actual I/O latency.

## Running it

```bash
pip install -r requirements.txt
python -m pytest tests/ -v
```

Developed and tested against **Python 3.12.3**. No lower bound has been
verified directly — nothing in the code obviously needs newer than 3.10,
but that claim hasn't been tested the way everything else in this
project has.

No GPU required. Everything here runs on CPU in well under two minutes.
(A GPU-dependent branch of this project — an actual model driving the
gate, benchmarked against KernelBench — was cut from scope; see
`PROTOCOL.md` for why, and what would need to be true to bring it back.)

## What this does and doesn't prove

**Proven, with a failing test behind each claim:**
- The correctness gate can't be fooled by a plausible-looking but wrong
  candidate (`test_verifier_search.py`).
- The sandbox rejects real adversarial code, not just obviously-bad
  examples (`test_sandbox.py`).
- The Gate's rollback is real where claimed and its taint-blocking is
  actually enforced, not just logged (`test_tool_executor.py`,
  `test_stress_statecheck.py`).
- The Gate generalizes to tool shapes it wasn't designed around — HTTP
  (connection resets, rate limits, timeouts) and git (real merge
  conflicts, real lock files) — with zero changes to the primitive itself
  (`test_primitive_generalization_*.py`).
- A deterministic pre-condition is a categorically different guarantee
  than a natural-language instruction, illustrated against a real,
  documented incident (`test_comparative_baseline_replit_incident.py`).

**Not proven, stated plainly:**
- That any of this outperforms what production agent tools already ship,
  measured rigorously (the honest target is the selective-classification
  methodology in arXiv:2606.08919 — cited in `PROTOCOL.md` §6, not
  yet applied here).
- That the mechanism is useful to anyone who didn't design it. Every test
  so far was written by the same person who wrote the primitive.
- Anything about whether a small model can find genuine algorithmic
  improvements — that was the original headline claim of the earlier,
  larger version of this project, and it's the one piece nothing here has
  evidence for.

## Known, stated limits

**Sandbox allowlist.** `verifier_search.py`'s sandbox restricts candidate
code to a fixed allowlist of imports (`torch`, `math`, `time`, `json` —
see `_ALLOWED_MODULES`). A legitimate candidate needing, say, `numpy`
will be rejected by the static check before it ever runs — a real, tested
constraint (`tests/test_sandbox.py`), not an oversight to quietly patch
around. This claim was briefly *false*: an earlier version only checked a
denylist of known-bad imports, so `numpy` (not explicitly enumerated as
dangerous) passed straight through unexamined. Found by testing this
README's own claim against the code, fixed by making the check a real
allowlist.

**Concurrency: was per-caller, now enforced per-system (single process).**
`execute_contracted` now runs its entire check -> execute -> post-check
sequence under a single module-level lock, unconditionally, for every
contract — `tests/test_gate_lock_serialization.py` proves two concurrent
calls fully serialize, using a bounded `join()` to detect real blocking,
not a timing guess. This closes a real gap: an earlier version only
checked the taint guard once, at entry, with no re-check before a call's
own post-condition evaluated — `tests/test_concurrency_boundary.py`
still demonstrates that gap directly (by calling `TaintTracker`'s
primitives without going through `execute_contracted`), because
`TaintTracker` itself remains unlocked; the guarantee now lives at the
call site, not in the tracker. This was never a deliberate trade-off
against a rejected fix — concurrent access was out of scope from early
on, and a full lock turned out to be simpler than a narrower re-check
mitigation that was considered and rejected (it would only have
discarded results computed during a taint, not eliminated the race, and
would have penalized operations unrelated to whatever caused the taint).
**What the lock doesn't cover:** separate processes — this remains a
single-process guarantee only, and mutating calls now fully serialize
within that process, which is the cost single-executor semantics always
implied.

**Reentrancy deadlocks — a hard constraint, not a bug to fix.** A
`pre_condition`, `post_condition`, or `rollback` function must never call
`execute_contracted` itself — `_GATE_LOCK` is a plain, non-reentrant
`threading.Lock`, so a nested call deadlocks the thread permanently.
Proven directly in `tests/test_gate_lock_serialization.py`, which itself
needed a real fix: the deadlocked thread holds the module-level lock
forever, which would have silently hung every other test file calling
`execute_contracted` afterward, until the test was changed to replace the
module's lock reference once the deadlock was confirmed. A reentrant lock
(`RLock`) was considered and rejected — it would let a nested call
silently succeed instead of failing loudly, risking two logically
distinct operations interleaving inside what's meant to be one atomic
sequence.

**`TaintTracker` methods called directly bypass the lock entirely.**
`mark()`, `attempt_clear()`, `clear()` — none of them go through
`_GATE_LOCK`, since it only wraps `execute_contracted`'s body. Code that
calls these directly, rather than through `execute_contracted`, gets none
of the concurrency guarantee above.

## License

MIT — free and unrestricted, no commercial terms attached to this code,
matching the actual strategy MCP itself used (Anthropic never sold or
restricted MCP directly; it later donated MCP entirely to the Linux
Foundation). Anyone can use, modify, fork, or ship this commercially with
no obligation beyond keeping the copyright notice. Copyright is held by
William Boyd individually — the intended company (Zowskyy) isn't
incorporated yet, and an unincorporated name can't hold copyright; once
it exists, ownership can be assigned to it with a standard written
transfer. Related, separate commercial products or services may be built
in the future that use this as a credibility anchor — this repository
itself carries no licensing fee
now or later.
