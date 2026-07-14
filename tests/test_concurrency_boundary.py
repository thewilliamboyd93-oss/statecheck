"""
tests/test_concurrency_boundary.py (pytest)
    Demonstrates that TaintTracker's own primitives (check_or_raise, mark)
    have no locking built into them — called directly, bypassing
    execute_contracted, a TOCTOU race is possible. This was true when
    written and remains true: TaintTracker itself is not thread-safe on
    its own.

    IMPORTANT SCOPE NOTE (added when _GATE_LOCK was introduced): this no
    longer describes the practical guarantee for real callers. Anyone
    going through the actual sanctioned entry point — execute_contracted —
    is now protected: the entire check-execute-post-check sequence
    serializes under a module-level lock, closing exactly this race at the
    real interface. See tests/test_gate_lock_serialization.py for that
    proof against the real entry point, not TaintTracker's internals in
    isolation. This test is retained because "the building block has no
    internal lock" is still an accurate, useful fact — it's why the lock
    had to be added at the call-site level rather than relying on
    TaintTracker to protect itself.
"""

import threading

from umpire.tool_executor import GLOBAL_TAINT, ContractViolation


def _fresh_violation(reason: str) -> ContractViolation:
    return ContractViolation(stage="post", tool_name="worker_b", predicate_name="race_test",
                              recovery_state="tainted", note=reason)


def test_toctou_race_between_check_and_in_flight_mutation():
    GLOBAL_TAINT.clear()
    GLOBAL_TAINT._recovery_registry.clear()

    checked = threading.Event()
    release_worker_a = threading.Event()
    result = {}

    def worker_a():
        # This mirrors exactly what execute_contracted does: check the
        # guard, THEN do the mutating work. Between those two steps, this
        # thread is "in flight" — its check has already passed.
        try:
            GLOBAL_TAINT.check_or_raise("worker_a")
            result["a_passed_check"] = True
        except ContractViolation:
            result["a_passed_check"] = False
            checked.set()
            return

        checked.set()  # tell the main thread the check has passed
        release_worker_a.wait(timeout=2)  # simulate real in-flight work
        # The mutation "completes" here — in a real tool this would be the
        # actual file write / DB commit / subprocess call finishing.
        result["a_mutation_completed"] = True

    t = threading.Thread(target=worker_a)
    t.start()
    assert checked.wait(timeout=2), "worker_a never reached its check"
    assert result.get("a_passed_check") is True, "worker_a's check should pass on a clean session"

    # Taint the session from a concurrent caller WHILE worker_a is mid-flight,
    # after its check already passed.
    GLOBAL_TAINT.mark("RACE_INDUCED_TAINT", _fresh_violation("induced during worker_a's in-flight window"))
    assert GLOBAL_TAINT.is_tainted()

    release_worker_a.set()
    t.join(timeout=2)

    # THE ACTUAL FINDING: worker_a's mutation completed successfully despite
    # the session being tainted before that completion — because the guard
    # is checked once, at entry, not continuously. This is exactly the
    # scenario single-writer/sequential-only exists to rule out by
    # assumption, since the code itself does not prevent it.
    assert result.get("a_mutation_completed") is True, (
        "if this is False, the race no longer exists (e.g. a lock was added) — "
        "update PROTOCOL.md §5 accordingly rather than leaving this test stale"
    )

    GLOBAL_TAINT.clear()
    GLOBAL_TAINT._recovery_registry.clear()
