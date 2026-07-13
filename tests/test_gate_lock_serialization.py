"""
tests/test_gate_lock_serialization.py (pytest)
    Proves _GATE_LOCK actually serializes execute_contracted calls end to
    end — not just that TaintTracker's raw methods are unlocked in
    isolation (test_concurrency_boundary.py proves that, deliberately, by
    bypassing execute_contracted to test the tracker's own primitives).
    This test goes through the real, sanctioned entry point.

    Methodology: thread A enters fn() and blocks there (via an Event).
    Thread B is started and attempts a second execute_contracted call.
    We then check thread_b.is_alive() after a bounded join — if the lock
    works, B is still blocked waiting for A's lock and hasn't finished.
    This is a genuine test of "is B currently blocked," not a timing
    guess about interleaving order — the assertion is unambiguous either
    way the scheduler behaves, unlike a sleep-and-hope race test.
"""

import threading

from invariant_gate import tool_executor as te


def _reset():
    te.GLOBAL_TAINT.clear()
    te.GLOBAL_TAINT._recovery_registry.clear()


def test_lock_blocks_a_second_call_while_first_is_in_flight():
    _reset()
    release_a = threading.Event()
    a_entered = threading.Event()
    order = []

    def fn_a():
        a_entered.set()
        release_a.wait(timeout=5)
        order.append("a")
        return {"ok": True}

    contract_a = te.ToolContract(name="lock_test_a", pre_condition=lambda: True,
                                  post_condition=lambda r: True, is_mutating=True)

    thread_a = threading.Thread(target=lambda: te.execute_contracted(contract_a, fn_a))
    thread_a.start()
    assert a_entered.wait(timeout=2), "thread_a never entered fn_a — test setup broken"

    def fn_b():
        order.append("b")
        return {"ok": True}

    contract_b = te.ToolContract(name="lock_test_b", pre_condition=lambda: True,
                                  post_condition=lambda r: True, is_mutating=True)

    thread_b = threading.Thread(target=lambda: te.execute_contracted(contract_b, fn_b))
    thread_b.start()

    # THE ACTUAL ASSERTION: B must still be blocked on the lock, since A
    # hasn't released it yet. A bounded join returning "still alive" is
    # the standard, correct way to test blocking — not a hopeful sleep.
    thread_b.join(timeout=0.3)
    assert thread_b.is_alive(), (
        "thread_b completed before A released the lock — serialization is not working"
    )

    release_a.set()
    thread_a.join(timeout=2)
    thread_b.join(timeout=2)

    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert order == ["a", "b"], f"expected strict ordering ['a', 'b'], got {order}"
    _reset()


def test_lock_prevents_the_original_toctou_race_through_the_real_entry_point():
    """
    The actual scenario test_concurrency_boundary.py demonstrated via
    direct TaintTracker access — now attempted through execute_contracted
    itself, proving the real entry point closes it.
    """
    _reset()
    release_a = threading.Event()
    a_entered = threading.Event()

    def fn_a():
        a_entered.set()
        release_a.wait(timeout=5)
        return {"ok": True}

    contract_a = te.ToolContract(name="race_test_a", pre_condition=lambda: True,
                                  post_condition=lambda r: True, is_mutating=True)

    thread_a = threading.Thread(target=lambda: te.execute_contracted(contract_a, fn_a))
    thread_a.start()
    assert a_entered.wait(timeout=2)

    # Attempt to taint the session via a second, unrelated mutating call
    # WHILE A is still in flight — this is exactly the race scenario.
    def fn_b_taints():
        return {"status": "failed"}

    contract_b = te.ToolContract(name="race_test_b", pre_condition=lambda: True,
                                  post_condition=lambda r: False,  # always fails -> taints
                                  is_mutating=True, taints_state_on_failure=True,
                                  taint_reason="RACE_INDUCED")

    result = {}

    def run_b():
        try:
            te.execute_contracted(contract_b, fn_b_taints)
        except te.ContractViolation as e:
            result["b_violation"] = e

    thread_b = threading.Thread(target=run_b)
    thread_b.start()
    thread_b.join(timeout=0.3)
    assert thread_b.is_alive(), "B should still be blocked behind A's lock"

    release_a.set()
    thread_a.join(timeout=2)
    thread_b.join(timeout=2)

    # A completed cleanly (its post_condition saw a clean, untainted world,
    # since B could not have tainted it until A released the lock).
    # B's taint is now applied, strictly after A finished.
    assert "b_violation" in result
    assert result["b_violation"].recovery_state == "tainted"
    assert te.GLOBAL_TAINT.is_tainted()
    _reset()


def test_reentrant_call_from_rollback_deadlocks_and_this_is_a_documented_constraint():
    """
    _GATE_LOCK is a plain threading.Lock, deliberately not an RLock — a
    reentrant lock would let a nested execute_contracted call (e.g. one
    invoked from inside another contract's rollback or post_condition)
    silently succeed, which could let two logically distinct operations
    interleave inside what's supposed to be one atomic sequence. A plain
    Lock fails loudly (deadlock) instead of quietly permitting that.

    This test proves the deadlock is real, using a bounded thread join so
    it can't hang the suite — daemon=True lets the process exit cleanly
    even though the deadlocked thread itself never finishes. The takeaway
    is documented as a hard constraint on tool-authoring code: a
    pre_condition, post_condition, or rollback function must NEVER call
    execute_contracted itself. This is the same category of rule as "no
    dynamic predicates" — a boundary tool authors must respect, not
    something the primitive can safely paper over.
    """
    _reset()

    def nested_rollback():
        inner_contract = te.ToolContract(name="reentrant_inner", pre_condition=lambda: True,
                                          post_condition=lambda r: True, is_mutating=True)
        te.execute_contracted(inner_contract, lambda: {"inner": True})

    outer_contract = te.ToolContract(name="reentrant_outer", pre_condition=lambda: True,
                                      post_condition=lambda r: False,  # force the rollback path
                                      rollback=nested_rollback, is_mutating=True)

    result = {"finished": False}

    def run():
        try:
            te.execute_contracted(outer_contract, lambda: {"ok": True})
        except te.ContractViolation:
            pass
        result["finished"] = True

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=1.5)

    assert not result["finished"] and t.is_alive(), (
        "expected a deadlock here — if this now finishes, _GATE_LOCK's "
        "reentrancy behavior changed and PROTOCOL.md / README must be "
        "updated to match, not just this test"
    )

    # CRITICAL CLEANUP: the deadlocked thread now holds _GATE_LOCK forever.
    # Since it's a module-level singleton shared by every test file in this
    # same pytest process, leaving it held would silently hang every OTHER
    # test that calls execute_contracted — this file sorts alphabetically
    # before several of them (git/http generalization, stress tests,
    # tool_executor tests). Replace the module's lock reference with a
    # fresh, unlocked one so subsequent calls (in this file and everywhere
    # else) use the new lock. The orphaned thread keeps holding the old,
    # now-unreferenced lock object forever — harmless, since it's a daemon
    # thread and nothing else will ever touch that specific lock instance.
    te._GATE_LOCK = threading.Lock()
    _reset()
