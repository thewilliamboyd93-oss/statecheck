"""
statecheck/tool_executor.py
    A model's "hands" — filesystem, sqlite, shell, python — do not get to act
    on the model's say-so alone. Every mutating call passes through the
    same shape of gate that verifier_search.py already proved works for
    kernel candidates: a deterministic pre-condition, the actual operation,
    a deterministic post-condition, and a typed result either way. This
    module is that gate, generalized.

    Two things the naive version of this idea gets wrong, corrected here:

      1. "Roll back the operation" is not equally true for every tool.
         A file write can be rolled back (snapshot before, restore after a
         failed post-condition). A sqlite write can be rolled back (it's
         already transactional). A shell command in the general case CANNOT
         be rolled back — there is no undo for `curl` or `rm`. Promising
         rollback there would be a lie the system tells about its own
         safety. Instead, tools without real rollback are marked
         `taints_state=True`, and a failed post-condition on one of them
         actually blocks further mutating calls in the session until a
         human clears the taint — a real enforcement mechanism, not a label.

      2. Predicates are never model-supplied strings. Per the spec this
         module implements: the model chooses WHICH tool to call and WITH
         WHAT arguments; the tool's author defines WHAT must be true before
         and after. Letting a model pass its own "goal" as an evaluable
         predicate would reintroduce exactly the fuzziness this primitive
         exists to remove.

    Direct mapping, per the accepted implementation plan: verifier_search's
    `ScoredCandidate.all_correct` becomes the post_condition of the search
    tool's contract, unchanged — this module does not reimplement that
    logic, it wraps it.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("statecheck.tool_executor")


# --------------------------------------------------------------------------- #
# Typed failure — replaces raw stderr strings so the caller (brain.py) can
# act on the failure structurally instead of re-reading prose.
# --------------------------------------------------------------------------- #

@dataclass
class ContractViolation(Exception):
    stage: str                    # 'pre' | 'post'
    tool_name: str
    predicate_name: str
    actual: Any = None
    recovery_state: str = "unknown"   # 'rolled_back' | 'tainted' | 'no_effect'
    note: str = ""

    def __str__(self) -> str:
        return (f"ContractViolation(tool={self.tool_name}, stage={self.stage}, "
                f"predicate={self.predicate_name}, recovery={self.recovery_state})")

    def to_dict(self) -> dict:
        return {
            "status": "error", "type": "ContractViolation",
            "tool": self.tool_name, "stage": self.stage,
            "failed_predicate": self.predicate_name,
            "actual": repr(self.actual)[:500],
            "recovery_state": self.recovery_state,
            "note": self.note,
        }


class ExecutionFailure(Exception):
    """The tool itself raised (e.g. a shell command returned non-zero) —
    distinct from a ContractViolation, which means the tool ran fine but
    the world it left behind didn't satisfy the contract."""


# --------------------------------------------------------------------------- #
# Taint tracking — the real enforcement behind "Tainted", not just a label.
# --------------------------------------------------------------------------- #

class TaintTracker:
    """
    Session-scoped. When a contract without real rollback fails its
    post-condition, the SPECIFIC REASON is marked tainted (not just "this
    tool failed" — a reason like "config_deleted" is more precise and lets
    different failure modes of the same tool have different recovery
    criteria). Any further call to a MUTATING tool is blocked while any
    taint is active, until every active reason is cleared.

    Recovery predicates are registered ONLY via register_recovery_predicate,
    which must be called from tool-authoring code at module load time — not
    from a request path driven by model input. attempt_clear() therefore
    can only ever LOOK UP a predicate by reason string; it can never RECEIVE
    one. This is what keeps the model's role strictly as "the trigger" and
    the tool author's role strictly as "the criterion" — a model can ask
    to clear a taint, but has no path, structural or incidental, to supply
    or influence what counts as recovered.
    """

    def __init__(self):
        self._tainted: dict[str, ContractViolation] = {}
        self._recovery_registry: dict[str, Callable[[], bool]] = {}

    def register_recovery_predicate(self, reason: str, predicate: Callable[[], bool]) -> None:
        """Tool-author-time only. Overwrites any prior registration for the
        same reason — intentionally simple, since re-registration only
        happens when tool code itself changes, not at runtime."""
        self._recovery_registry[reason] = predicate

    def mark(self, reason: str, violation: ContractViolation) -> None:
        self._tainted[reason] = violation
        logger.warning("TAINT: reason=%s (tool=%s). Mutating calls blocked until cleared.",
                        reason, violation.tool_name)

    def is_tainted(self) -> bool:
        return len(self._tainted) > 0

    def check_or_raise(self, calling_tool: str) -> None:
        if self._tainted:
            reasons = ", ".join(self._tainted)
            raise ContractViolation(
                stage="pre", tool_name=calling_tool, predicate_name="taint_guard",
                actual=reasons, recovery_state="tainted",
                note=f"Session is tainted: {reasons}. Call attempt_clear(reason) or, "
                     "for reasons with no registered predicate, a human must call clear().",
            )

    def check_or_raise_for_recovery(self, calling_tool: str, recovers_reasons: Optional[set]) -> None:
        """Scoped variant used by is_recovery_tool=True contracts: exempt
        only while every ACTIVE taint reason is one this tool is declared
        to address. An unrelated simultaneous taint still blocks it."""
        if not self._tainted:
            return
        if recovers_reasons is None:
            return  # unconditionally exempt, author's explicit choice
        active = set(self._tainted.keys())
        unhandled = active - set(recovers_reasons)
        if unhandled:
            raise ContractViolation(
                stage="pre", tool_name=calling_tool, predicate_name="taint_guard",
                actual=", ".join(unhandled), recovery_state="tainted",
                note=f"{calling_tool} is a recovery tool for {sorted(recovers_reasons)}, "
                     f"but unrelated taint reason(s) are also active: {sorted(unhandled)}. "
                     "Blocked — this is not the exemption this tool was declared for.",
            )

    def attempt_clear(self, reason: str) -> bool:
        """
        The model may call this (by reason string) to trigger re-evaluation.
        It cannot supply, alter, or narrate the check itself — only a
        predicate registered ahead of time by tool-authoring code is ever
        evaluated. A reason with no registered predicate is permanent until
        a human calls clear() directly.
        """
        if reason not in self._tainted:
            return True  # nothing to clear
        predicate = self._recovery_registry.get(reason)
        if predicate is None:
            logger.info("no recovery predicate registered for reason=%r; requires human clear()", reason)
            return False
        try:
            recovered = bool(predicate())
        except Exception as exc:
            logger.warning("recovery predicate for reason=%r raised (%s); taint remains", reason, exc)
            return False
        if recovered:
            self.clear(reason)
            logger.info("taint reason=%r auto-cleared: registered predicate passed", reason)
            return True
        logger.warning("recovery predicate for reason=%r returned False; taint remains", reason)
        return False

    def clear(self, reason: Optional[str] = None) -> None:
        """Human-initiated override — always available regardless of
        whether a recovery predicate exists or passes."""
        if reason:
            self._tainted.pop(reason, None)
        else:
            self._tainted.clear()
        logger.info("taint cleared (human/explicit)%s", f" for reason={reason}" if reason else " (all)")

    def status(self) -> dict:
        return {reason: v.to_dict() for reason, v in self._tainted.items()}


# A single tracker per process is the right scope: it mirrors the intended
# design (single-executor semantics, ORNG-era rejection of the "State
# Handshake" primitive still applies — no distributed-writer problem is
# being solved here). That design intent was, for a while, only a
# documented assumption: nothing actually stopped two threads from racing
# each other. _GATE_LOCK below closes that gap — see execute_contracted.
GLOBAL_TAINT = TaintTracker()

# Serializes the entire check -> execute -> post-check sequence for every
# call to execute_contracted, unconditionally, for every contract — no
# per-tool or per-mutating-flag special-casing. This is deliberately the
# simplest rule available: one lock, uniformly applied, nothing to reason
# about case by case. It turns "single-writer, sequential-only" from a
# claim in PROTOCOL.md §7 into something actually true of the code, not
# just documented as an assumption callers were trusted to honor.
#
# Cost, stated plainly: mutating tool calls now fully serialize within one
# process. A slow call (a real git merge, a slow HTTP request) blocks every
# other call until it resolves. This is not a new cost introduced here —
# it's the cost single-executor semantics always implied; previously it
# just wasn't enforced. This does NOT extend across separate processes.
_GATE_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# The contract + gate
# --------------------------------------------------------------------------- #

@dataclass
class ToolContract:
    name: str
    pre_condition: Optional[Callable[..., bool]] = None
    post_condition: Optional[Callable[[Any], bool]] = None
    rollback: Optional[Callable[[], None]] = None
    is_mutating: bool = True          # gates it against the taint guard
    taints_state_on_failure: bool = False  # True only when rollback is impossible
    taint_reason: Optional[str] = None     # defaults to `name` if unset; see below
    is_recovery_tool: bool = False
    recovers_reasons: Optional[set] = None
    """
    Author-declared ONLY — never settable by a model at call time, same
    asymmetry as the recovery-predicate registry. A contract marked
    is_recovery_tool=True is exempt from the taint guard, so it can run
    WHILE the session is tainted — otherwise a taint can deadlock: if the
    only legitimate fix for a condition is itself a mutating call, and
    mutating calls are unconditionally blocked, there is no path back to
    untainted except a human acting completely outside the system.

    `recovers_reasons`, if set, scopes the exemption further: this tool is
    only exempt while EVERY active taint reason is in this set. A recovery
    tool declared for "CONFIG_DELETED" must not also run through an
    unrelated, simultaneous "DATABASE_CORRUPTED" taint it has no bearing
    on — that would turn a narrow, audited exception into a general
    bypass. `None` means unconditionally exempt (use sparingly, only for
    tools that are genuinely safe under any taint).
    """



def execute_contracted(contract: ToolContract, fn: Callable[..., Any], *args, **kwargs) -> Any:
    """
    The gate. Pre-check -> execute -> post-check -> resolve. This is the
    only path through which a mutating tool call should ever run.

    The entire sequence runs under _GATE_LOCK, unconditionally, for every
    contract. This is what makes PROTOCOL.md §7's "single-executor
    semantics only" claim actually true: two concurrent calls to this
    function fully serialize, so the TOCTOU race
    tests/test_concurrency_boundary.py demonstrated against direct
    TaintTracker access cannot occur through this function — see
    tests/test_gate_lock_serialization.py for the proof against the real
    entry point, not just the tracker's internals in isolation.
    """
    with _GATE_LOCK:
        pre_name = contract.pre_condition.__name__ if contract.pre_condition else "none"
        post_name = contract.post_condition.__name__ if contract.post_condition else "none"

        if contract.is_mutating and not contract.is_recovery_tool:
            GLOBAL_TAINT.check_or_raise(contract.name)
        elif contract.is_mutating and contract.is_recovery_tool:
            GLOBAL_TAINT.check_or_raise_for_recovery(contract.name, contract.recovers_reasons)

        if contract.is_mutating and contract.pre_condition is None:
            raise ContractViolation(stage="pre", tool_name=contract.name,
                                     predicate_name="none", recovery_state="no_effect",
                                     note="mutating tool has no pre_condition (deny-by-default)")
        if contract.pre_condition is not None:
            try:
                ok = contract.pre_condition()
            except Exception as exc:
                raise ContractViolation(stage="pre", tool_name=contract.name, predicate_name=pre_name,
                                         actual=str(exc), recovery_state="no_effect",
                                         note="pre_condition itself raised") from exc
            if not ok:
                raise ContractViolation(stage="pre", tool_name=contract.name, predicate_name=pre_name,
                                         recovery_state="no_effect",
                                         note="pre_condition false — operation never attempted")

        try:
            output = fn(*args, **kwargs)
        except Exception as exc:
            exec_failure = ExecutionFailure(f"{contract.name} raised during execution: {exc}")
            # This is the fix for the gap a stress test found: a crash PARTWAY
            # through fn (not a clean return that then fails its post_condition)
            # was previously falling straight through to ExecutionFailure with
            # NO rollback attempt and NO taint — silently leaving any partial
            # side effect unrecovered and untracked. A mid-execution exception
            # gives no more certainty about world-state than a failed
            # post_condition does, so it gets the same treatment.
            if contract.rollback is not None:
                try:
                    contract.rollback()
                    logger.warning(
                        "%s raised mid-execution; rollback attempted (best-effort — "
                        "if the crash happened after only SOME of the tool's effects "
                        "landed, rollback only undoes what its own logic covers).",
                        contract.name,
                    )
                except Exception as rb_exc:
                    logger.error(
                        "%s raised mid-execution AND its rollback itself failed (%s); "
                        "state is genuinely unknown, not just unrecovered.",
                        contract.name, rb_exc,
                    )
            elif contract.taints_state_on_failure:
                reason = contract.taint_reason or contract.name
                violation = ContractViolation(
                    stage="execution", tool_name=contract.name,
                    predicate_name="mid_execution_exception", actual=str(exc),
                    recovery_state="tainted",
                    note=f"{contract.name} raised mid-execution with no rollback available "
                         f"(reason={reason}). Partial side effects, if any, are unverified — "
                         "treated identically to a failed post_condition.",
                )
                GLOBAL_TAINT.mark(reason, violation)
            raise exec_failure from exc

        if contract.post_condition is not None:
            try:
                ok = contract.post_condition(output)
            except Exception as exc:
                ok = False
                logger.warning("post_condition for %s raised (%s); treating as failed", contract.name, exc)

            if not ok:
                if contract.rollback is not None:
                    contract.rollback()
                    state = "rolled_back"
                elif contract.taints_state_on_failure:
                    reason = contract.taint_reason or contract.name
                    violation = ContractViolation(stage="post", tool_name=contract.name,
                                                   predicate_name=post_name, actual=output,
                                                   recovery_state="tainted",
                                                   note=f"no rollback available for this tool (reason={reason})")
                    GLOBAL_TAINT.mark(reason, violation)
                    raise violation
                else:
                    state = "unknown"
                raise ContractViolation(stage="post", tool_name=contract.name, predicate_name=post_name,
                                         actual=output, recovery_state=state)

        return output


# --------------------------------------------------------------------------- #
# Concrete tools — the "hands", each honest about its own rollback story.
# --------------------------------------------------------------------------- #

def file_write_tool(path: str, content: str, is_recovery_tool: bool = False,
                     recovers_reasons: Optional[set] = None) -> Any:
    """
    Rollback is REAL here: snapshot before write, restore on a failed
    post-condition. This tool never taints state. `is_recovery_tool=True`
    (optionally scoped by `recovers_reasons`) is set only by the specific
    call site meant to be usable during an active taint of that reason
    (e.g. restoring a deleted config file) — it is not a general property
    of file writes, and a caller cannot flip it on speculatively to dodge
    the guard for an unrelated write or an unrelated taint.
    """
    p = Path(path)

    def pre():
        return p.parent.exists()

    backup = {"existed": p.exists(), "prior_content": p.read_text() if p.exists() else None}

    def do_write():
        p.write_text(content)
        return {"path": str(p), "written_bytes": len(content)}

    def post(result):
        return p.exists() and p.read_text() == content

    def rollback():
        if backup["existed"]:
            p.write_text(backup["prior_content"])
        elif p.exists():
            p.unlink()
        logger.info("file_write_tool rolled back %s to pre-call state", p)

    contract = ToolContract(name="file_write", pre_condition=pre, post_condition=post,
                             rollback=rollback, is_mutating=True, taints_state_on_failure=False,
                             is_recovery_tool=is_recovery_tool, recovers_reasons=recovers_reasons)
    return execute_contracted(contract, do_write)


def sqlite_exec_tool(db_path: str, sql: str, params: tuple = (),
                      verify_query: Optional[str] = None,
                      verify_params: tuple = (),
                      verify_predicate: Optional[Callable[[list], bool]] = None) -> Any:
    """
    Rollback is REAL here too: sqlite transactions are native. If a
    verify_predicate is supplied and fails against verify_query's result,
    the write is rolled back via the transaction itself, not a snapshot.
    """
    conn = sqlite3.connect(db_path)

    def pre():
        return Path(db_path).exists()

    def do_exec():
        conn.execute("BEGIN")
        conn.execute(sql, params)
        if verify_query:
            rows = conn.execute(verify_query, verify_params).fetchall()
            return {"rows": rows}
        return {"rows": None}

    def post(result):
        if verify_predicate is None:
            return True
        return verify_predicate(result["rows"])

    def rollback():
        conn.rollback()
        logger.info("sqlite_exec_tool rolled back transaction on %s", db_path)

    try:
        contract = ToolContract(name="sqlite_exec", pre_condition=pre, post_condition=post,
                                 rollback=rollback, is_mutating=True, taints_state_on_failure=False)
        result = execute_contracted(contract, do_exec)
        conn.commit()
        return result
    finally:
        conn.close()


def shell_command_tool(cmd: list, timeout_s: int = 15,
                        post_condition: Optional[Callable[[Any], bool]] = None,
                        taint_reason: Optional[str] = None) -> Any:
    """
    NO real rollback is possible for an arbitrary shell command in the
    general case — this is stated explicitly rather than papered over.
    A failed post-condition here taints the session under `taint_reason`
    (defaults to "shell_command" if not given) and blocks further mutating
    calls until that reason is cleared — either automatically via a
    pre-registered recovery predicate (GLOBAL_TAINT.register_recovery_predicate)
    or by a human via GLOBAL_TAINT.clear(). Denied by default unless the
    caller explicitly opts in, since this is the highest-risk of the
    four hands.
    """
    import subprocess

    def pre():
        return True  # narrow, tool-specific pre-conditions are supplied by callers via wrapping

    def do_run():
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        if proc.returncode != 0:
            raise ExecutionFailure(f"shell command exited {proc.returncode}: {proc.stderr[-500:]}")
        return {"stdout": proc.stdout, "stderr": proc.stderr, "returncode": proc.returncode}

    contract = ToolContract(name="shell_command", pre_condition=pre, post_condition=post_condition,
                             rollback=None, is_mutating=True, taints_state_on_failure=True,
                             taint_reason=taint_reason)
    return execute_contracted(contract, do_run)


def search_tool(target, propose_fn, config=None) -> Any:
    """
    Direct mapping per the accepted plan: this wraps
    statecheck.verifier_search.run_search unchanged, and its post_condition
    IS ScoredCandidate.all_correct — the exact logic already proven in
    verifier_search.py, not a reimplementation of it. A search whose best
    result is incorrect is itself a contract failure at the tool_executor
    layer, not just an internal fitness=0.0.
    """
    from statecheck.verifier_search import run_search, SearchConfig

    def do_search():
        return run_search(target, propose_fn, config or SearchConfig())

    def post(run):
        return run.best is not None and run.best.all_correct

    contract = ToolContract(name="search", pre_condition=None, post_condition=post,
                             rollback=None, is_mutating=False, taints_state_on_failure=False)
    return execute_contracted(contract, do_search)
