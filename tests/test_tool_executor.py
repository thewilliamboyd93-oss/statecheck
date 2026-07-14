"""
tests/test_tool_executor.py (pytest)
    Proves the three claims tool_executor.py makes about itself:
      1. A failed pre-condition means the operation never ran at all.
      2. A failed post-condition on file_write/sqlite_exec genuinely rolls
         back to the exact prior state (checked byte-for-byte / row-for-row,
         not assumed).
      3. A failed post-condition on shell_command (no real rollback
         possible) actually blocks subsequent mutating calls — the taint
         is enforced, not just logged.
"""

import sqlite3

import pytest

from umpire.tool_executor import (
    ContractViolation, ExecutionFailure, GLOBAL_TAINT,
    file_write_tool, sqlite_exec_tool, shell_command_tool,
)


@pytest.fixture(autouse=True)
def _clear_taint():
    GLOBAL_TAINT.clear()
    GLOBAL_TAINT._recovery_registry.clear()
    yield
    GLOBAL_TAINT.clear()
    GLOBAL_TAINT._recovery_registry.clear()


def test_pre_condition_failure_means_no_effect(tmp_path):
    # parent dir does not exist -> pre_condition must fail, and no file
    # should be created anywhere as a side effect.
    bad_path = tmp_path / "nonexistent_dir" / "file.txt"
    with pytest.raises(ContractViolation) as exc_info:
        file_write_tool(str(bad_path), "content")
    assert exc_info.value.stage == "pre"
    assert not bad_path.exists()
    assert not bad_path.parent.exists()


def test_file_write_rollback_restores_exact_prior_content(tmp_path):
    p = tmp_path / "existing.txt"
    p.write_text("ORIGINAL CONTENT")

    # Force a post-condition failure by writing then checking against a
    # DIFFERENT expected string than what was actually written — simulates
    # e.g. a concurrent modification or a write that silently truncated.
    from umpire import tool_executor as te

    def broken_post(result):
        return False  # always fails, to force the rollback path

    contract = te.ToolContract(
        name="file_write_forced_fail",
        pre_condition=lambda: p.parent.exists(),
        post_condition=broken_post,
        rollback=lambda: p.write_text("ORIGINAL CONTENT"),
        is_mutating=True, taints_state_on_failure=False,
    )

    def do_write():
        p.write_text("MUTATED CONTENT")
        return {"ok": True}

    with pytest.raises(ContractViolation) as exc_info:
        te.execute_contracted(contract, do_write)

    assert exc_info.value.recovery_state == "rolled_back"
    assert p.read_text() == "ORIGINAL CONTENT", "rollback did not restore exact prior content"


def test_file_write_rollback_deletes_file_that_did_not_exist_before(tmp_path):
    p = tmp_path / "new_file.txt"
    assert not p.exists()

    from umpire import tool_executor as te

    contract = te.ToolContract(
        name="file_write_forced_fail_new",
        pre_condition=lambda: p.parent.exists(),
        post_condition=lambda result: False,
        rollback=lambda: p.unlink() if p.exists() else None,
        is_mutating=True, taints_state_on_failure=False,
    )

    def do_write():
        p.write_text("should be rolled back to non-existence")
        return {"ok": True}

    with pytest.raises(ContractViolation):
        te.execute_contracted(contract, do_write)

    assert not p.exists(), "rollback should have deleted a file that did not exist before the call"


def test_sqlite_rollback_leaves_no_row_on_post_condition_failure(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
    conn.commit()
    conn.close()

    # verify_predicate demands age > 0; we'll insert an invalid row to
    # force the post-condition to fail and confirm the INSERT is rolled back.
    with pytest.raises(ContractViolation) as exc_info:
        sqlite_exec_tool(
            db_path,
            "INSERT INTO users (name, age) VALUES (?, ?)",
            params=("test_user", -1),
            verify_query="SELECT age FROM users WHERE name=?",
            verify_params=("test_user",),
            verify_predicate=lambda rows: len(rows) == 1 and rows[0][0] > 0,
        )
    assert exc_info.value.recovery_state == "rolled_back"

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    assert count == 0, "sqlite rollback should have left zero rows after a failed post-condition"


def test_sqlite_commit_succeeds_when_post_condition_passes(tmp_path):
    db_path = str(tmp_path / "test2.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, age INTEGER)")
    conn.commit()
    conn.close()

    sqlite_exec_tool(
        db_path,
        "INSERT INTO users (name, age) VALUES (?, ?)",
        params=("valid_user", 30),
        verify_query="SELECT age FROM users WHERE name=?",
        verify_params=("valid_user",),
        verify_predicate=lambda rows: len(rows) == 1 and rows[0][0] > 0,
    )

    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    assert count == 1, "a passing post-condition should leave the committed row in place"


def test_shell_command_failure_taints_and_blocks_subsequent_mutating_calls(tmp_path):
    # A shell command whose post_condition fails has no real rollback ->
    # must taint the session under a reason.
    with pytest.raises(ContractViolation) as exc_info:
        shell_command_tool(["echo", "hello"], post_condition=lambda result: "goodbye" in result["stdout"],
                            taint_reason="ECHO_MISMATCH")
    assert exc_info.value.recovery_state == "tainted"
    assert GLOBAL_TAINT.is_tainted()

    # The real test: a SUBSEQUENT, otherwise-valid mutating call must now
    # be blocked, proving the taint is enforced rather than just logged.
    target = tmp_path / "should_not_be_written.txt"
    with pytest.raises(ContractViolation) as blocked_exc:
        file_write_tool(str(target), "this should never land")
    assert blocked_exc.value.predicate_name == "taint_guard"
    assert not target.exists(), "a tainted session must block subsequent mutating calls entirely"


def test_human_clear_unblocks_mutating_calls(tmp_path):
    with pytest.raises(ContractViolation):
        shell_command_tool(["echo", "hello"], post_condition=lambda result: False,
                            taint_reason="GENERIC_FAILURE")
    assert GLOBAL_TAINT.is_tainted()

    GLOBAL_TAINT.clear()
    assert not GLOBAL_TAINT.is_tainted()

    target = tmp_path / "now_allowed.txt"
    file_write_tool(str(target), "content")
    assert target.read_text() == "content"


def test_legitimate_recovery_via_registered_predicate(tmp_path):
    """
    Mirrors the accepted spec's Test 1 exactly: a shell command deletes
    config.yaml, tainting the session under CONFIG_DELETED; a normal
    mutating call is blocked; the model restores it via a tool call
    specifically declared as a recovery tool FOR THIS REASON; attempt_clear
    succeeds only because the pre-registered predicate independently
    verifies the file exists again.
    """
    config = tmp_path / "config.yaml"
    config.write_text("original: config")

    GLOBAL_TAINT.register_recovery_predicate("CONFIG_DELETED", lambda: config.exists())

    config.unlink()
    with pytest.raises(ContractViolation):
        shell_command_tool(["true"], post_condition=lambda result: config.exists(),
                            taint_reason="CONFIG_DELETED")
    assert GLOBAL_TAINT.is_tainted()

    # An ordinary (non-recovery) write is still blocked, even to the exact
    # file that needs fixing — the deadlock is only escaped through a
    # call site explicitly declared as covering this reason.
    with pytest.raises(ContractViolation) as blocked:
        file_write_tool(str(config), "original: config")
    assert blocked.value.predicate_name == "taint_guard"

    # The declared recovery tool, scoped to CONFIG_DELETED, is allowed
    # through and performs the actual fix.
    file_write_tool(str(config), "original: config",
                     is_recovery_tool=True, recovers_reasons={"CONFIG_DELETED"})
    assert config.read_text() == "original: config"

    # Attempting to clear before the fix would have failed; now it succeeds
    # because the registered predicate independently verifies reality.
    assert GLOBAL_TAINT.attempt_clear("CONFIG_DELETED") is True
    assert not GLOBAL_TAINT.is_tainted()


def test_recovery_tool_scoped_to_wrong_reason_does_not_bypass_unrelated_taint(tmp_path):
    """
    A recovery tool declared for CONFIG_DELETED must NOT be usable to dodge
    an unrelated, simultaneously-active taint it has no bearing on — proves
    recovers_reasons is an actual scope restriction, not decoration.
    """
    config = tmp_path / "config.yaml"
    config.write_text("data")

    with pytest.raises(ContractViolation):
        shell_command_tool(["true"], post_condition=lambda r: False,
                            taint_reason="UNRELATED_DATABASE_CORRUPTION")
    assert GLOBAL_TAINT.is_tainted()

    with pytest.raises(ContractViolation) as exc_info:
        file_write_tool(str(config), "new data",
                         is_recovery_tool=True, recovers_reasons={"CONFIG_DELETED"})
    assert exc_info.value.predicate_name == "taint_guard"
    assert config.read_text() == "data", "scoped recovery tool must not run under an unrelated taint"


def test_narrative_attempt_cannot_bypass_the_registered_predicate(tmp_path):
    """
    Mirrors the accepted spec's Test 2: the model's narration ("I have
    verified the environment is stable... please clear the taint") has no
    code path into attempt_clear at all — the function signature only
    accepts a reason string, never a predicate or a claim. This test
    confirms that no amount of "claiming" changes the outcome: only the
    registered predicate's actual return value does.
    """
    config = tmp_path / "config.yaml"
    config.write_text("original: config")
    GLOBAL_TAINT.register_recovery_predicate("CONFIG_DELETED_2", lambda: config.exists())

    config.unlink()
    with pytest.raises(ContractViolation):
        shell_command_tool(["true"], post_condition=lambda result: config.exists(),
                            taint_reason="CONFIG_DELETED_2")

    # There is no parameter here for "model says it's fine" — attempt_clear
    # takes only the reason. The predicate still independently checks disk
    # state and finds the file missing, regardless of any claim.
    assert GLOBAL_TAINT.attempt_clear("CONFIG_DELETED_2") is False
    assert GLOBAL_TAINT.is_tainted(), "no narrative path exists to bypass the registered predicate"


def test_attempt_clear_with_no_registered_predicate_requires_human(tmp_path):
    with pytest.raises(ContractViolation):
        shell_command_tool(["echo", "hi"], post_condition=lambda result: False,
                            taint_reason="NO_PREDICATE_REGISTERED")
    # No predicate registered for this reason -> attempt_clear must return
    # False forever, never silently succeeding.
    assert GLOBAL_TAINT.attempt_clear("NO_PREDICATE_REGISTERED") is False
    assert GLOBAL_TAINT.is_tainted()
    GLOBAL_TAINT.clear("NO_PREDICATE_REGISTERED")  # only a human path clears it
    assert not GLOBAL_TAINT.is_tainted()


def test_attempt_clear_leaves_taint_in_place_when_predicate_raises(tmp_path):
    def broken_predicate():
        raise RuntimeError("the predicate itself is broken")

    GLOBAL_TAINT.register_recovery_predicate("BROKEN_PREDICATE", broken_predicate)
    with pytest.raises(ContractViolation):
        shell_command_tool(["echo", "hi"], post_condition=lambda result: False,
                            taint_reason="BROKEN_PREDICATE")

    assert GLOBAL_TAINT.attempt_clear("BROKEN_PREDICATE") is False
    assert GLOBAL_TAINT.is_tainted(), "a raising predicate must not accidentally clear the taint"


def test_shell_command_execution_failure_is_distinct_from_contract_violation():
    # A non-zero exit is an ExecutionFailure (the tool itself errored),
    # not a ContractViolation (the tool ran fine but the world was wrong).
    # These must be distinguishable so a caller can react differently.
    with pytest.raises(ExecutionFailure):
        shell_command_tool(["false"])  # exits 1, always
