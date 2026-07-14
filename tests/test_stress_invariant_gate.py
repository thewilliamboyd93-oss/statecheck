"""
tests/test_stress_umpire.py (pytest)
    Three adversarial scenarios distinct from the happy-path and simple
    failure-path tests already in test_tool_executor.py:

      1. Partial failure: does a MID-EXECUTION crash (not a post_condition
         failure) still trigger rollback/taint, or does it leak an
         unrecovered side effect?
      2. Recovery cascade: does a recovery tool that itself fails while
         exempted create a bypassable state, or does the guard correctly
         require ALL active taints to be covered?
      3. The lying tool: does post_condition override a tool's own
         self-reported "success", or can a tool talk its way past the gate?
"""

import pytest

from umpire import tool_executor as te
from umpire.tool_executor import ContractViolation, ExecutionFailure, GLOBAL_TAINT, shell_command_tool


@pytest.fixture(autouse=True)
def _clear_taint():
    GLOBAL_TAINT.clear()
    GLOBAL_TAINT._recovery_registry.clear()
    yield
    GLOBAL_TAINT.clear()
    GLOBAL_TAINT._recovery_registry.clear()


def test_partial_failure_mid_execution_triggers_rollback(tmp_path):
    """
    A tool that mutates file_a successfully, then RAISES before touching
    file_b (simulating a crash mid-operation, not a post_condition
    failure). This is different from every existing rollback test, which
    all trigger rollback via a failed post_condition AFTER fn returns
    cleanly. Here fn itself raises.
    """
    file_a = tmp_path / "a.txt"
    file_a.write_text("original_a")

    def do_multi_write():
        file_a.write_text("mutated_a")
        raise RuntimeError("simulated crash after first effect")

    rollback_called = {"flag": False}

    def rollback():
        rollback_called["flag"] = True
        file_a.write_text("original_a")

    contract = te.ToolContract(
        name="multi_write_forced_crash", pre_condition=lambda: True,
        post_condition=lambda r: True, rollback=rollback,
        is_mutating=True, taints_state_on_failure=False,
    )

    with pytest.raises(ExecutionFailure):
        te.execute_contracted(contract, do_multi_write)

    assert rollback_called["flag"], "rollback was never attempted after a mid-execution crash"
    assert file_a.read_text() == "original_a", "partial side effect was left in place after a crash"


def test_partial_failure_with_no_rollback_taints_instead(tmp_path):
    """Same scenario, but the tool has no real rollback — a mid-execution
    crash on an irreversible tool must taint, exactly like a post_condition
    failure would, not silently disappear as an untracked ExecutionFailure."""

    def do_crash():
        raise RuntimeError("simulated crash, no rollback available")

    contract = te.ToolContract(
        name="crash_no_rollback", pre_condition=lambda: True,
        post_condition=lambda r: True, rollback=None,
        is_mutating=True, taints_state_on_failure=True, taint_reason="MID_EXEC_CRASH",
    )

    with pytest.raises(ExecutionFailure):
        te.execute_contracted(contract, do_crash)

    assert GLOBAL_TAINT.is_tainted(), "a mid-execution crash on an irreversible tool must taint the session"
    assert "MID_EXEC_CRASH" in GLOBAL_TAINT._tainted


def test_recovery_tool_that_itself_fails_does_not_create_bypass():
    """
    A recovery tool scoped to TAINT_A runs (exempt), but its own attempt
    fails and it has no rollback -> taints under TAINT_B. Now both A and B
    are active. Proves this does NOT create a bypass: a different recovery
    tool scoped only to A is still blocked, because it doesn't cover B.
    """
    with pytest.raises(ContractViolation):
        shell_command_tool(["true"], post_condition=lambda r: False, taint_reason="TAINT_A")
    assert GLOBAL_TAINT.is_tainted()

    failing_recovery = te.ToolContract(
        name="recovery_for_a", pre_condition=lambda: True,
        post_condition=lambda r: False, rollback=None,
        is_mutating=True, taints_state_on_failure=True, taint_reason="TAINT_B",
        is_recovery_tool=True, recovers_reasons={"TAINT_A"},
    )
    with pytest.raises(ContractViolation):
        te.execute_contracted(failing_recovery, lambda: {"attempted": True})

    assert "TAINT_A" in GLOBAL_TAINT._tainted
    assert "TAINT_B" in GLOBAL_TAINT._tainted

    # A recovery tool covering ONLY TAINT_A must now be blocked, since
    # TAINT_B is also active and unhandled — no death-spiral bypass exists.
    a_only_recovery = te.ToolContract(
        name="recovery_for_a_only", pre_condition=lambda: True,
        post_condition=lambda r: True,
        is_mutating=True, is_recovery_tool=True, recovers_reasons={"TAINT_A"},
    )
    with pytest.raises(ContractViolation) as exc:
        te.execute_contracted(a_only_recovery, lambda: {"ok": True})
    assert exc.value.predicate_name == "taint_guard"

    # Only a recovery tool covering BOTH gets through.
    both_recovery = te.ToolContract(
        name="recovery_for_both", pre_condition=lambda: True,
        post_condition=lambda r: True,
        is_mutating=True, is_recovery_tool=True, recovers_reasons={"TAINT_A", "TAINT_B"},
    )
    result = te.execute_contracted(both_recovery, lambda: {"ok": True})
    assert result == {"ok": True}


def test_post_condition_overrides_tool_self_reported_success(tmp_path):
    """
    The tool's own return value claims success; post_condition checks real
    state and disagrees. The gate must trust the predicate, never the
    tool's self-report — this is the entire premise of the design.
    """
    target = tmp_path / "state.txt"
    target.write_text("unchanged")

    def lying_do_fn():
        return {"status": "success", "message": "everything is fine, nothing to see here"}

    def real_post_condition(result):
        return target.read_text() == "expected_new_value"  # never actually happened

    contract = te.ToolContract(
        name="lying_tool", pre_condition=lambda: True,
        post_condition=real_post_condition, rollback=None,
        is_mutating=True, taints_state_on_failure=True, taint_reason="LYING_TOOL_CAUGHT",
    )
    with pytest.raises(ContractViolation) as exc:
        te.execute_contracted(contract, lying_do_fn)

    assert exc.value.recovery_state == "tainted"
    assert GLOBAL_TAINT.is_tainted()
    assert target.read_text() == "unchanged"
