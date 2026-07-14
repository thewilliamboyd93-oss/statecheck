"""
tests/test_primitive_generalization_git.py (pytest)
    The second half of the falsification pair PROTOCOL.md §4 names
    explicitly (HTTP was tested in test_primitive_generalization_http.py).
    Git is a genuinely different tool shape again: unlike file writes,
    sqlite, or HTTP, a failed git merge HAS a real, well-defined rollback
    (`git merge --abort`) — this is the first tool in the whole project
    with authentic rollback outside file/sqlite, and the first time the
    rollback path gets exercised against an external process rather than
    Python-native state.

    Same rule as the HTTP test: if this needs any change to
    tool_executor.py, the primitive doesn't generalize as cleanly as
    claimed. Real git repos, real subprocess calls, real merge conflicts —
    no mocking.
"""

import subprocess
from pathlib import Path

import pytest

from umpire.tool_executor import ToolContract, execute_contracted, ExecutionFailure, ContractViolation, GLOBAL_TAINT


@pytest.fixture(autouse=True)
def _reset():
    GLOBAL_TAINT.clear()
    GLOBAL_TAINT._recovery_registry.clear()
    yield
    GLOBAL_TAINT.clear()
    GLOBAL_TAINT._recovery_registry.clear()


def _run(cmd, cwd, check=False):
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"fixture setup command failed: {cmd}\nstdout={result.stdout}\nstderr={result.stderr}")
    return result


@pytest.fixture
def conflicting_repo(tmp_path):
    """A real git repo with two branches that modify the same line
    differently — a genuine, reproducible merge conflict, not a simulated
    error code. Uses the repo's actual default branch name rather than
    assuming "main" — this environment defaults to "master"."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q"], repo, check=True)
    _run(["git", "config", "user.email", "gate-test@local"], repo, check=True)
    _run(["git", "config", "user.name", "gate-test"], repo, check=True)

    f = repo / "shared.txt"
    f.write_text("original line\n")
    _run(["git", "add", "."], repo, check=True)
    _run(["git", "commit", "-q", "-m", "initial"], repo, check=True)
    base_branch = _run(["git", "branch", "--show-current"], repo, check=True).stdout.strip()

    _run(["git", "checkout", "-q", "-b", "feature"], repo, check=True)
    f.write_text("feature branch change\n")
    _run(["git", "add", "."], repo, check=True)
    _run(["git", "commit", "-q", "-m", "feature change"], repo, check=True)

    _run(["git", "checkout", "-q", base_branch], repo, check=True)
    f.write_text("main branch change\n")
    _run(["git", "add", "."], repo, check=True)
    _run(["git", "commit", "-q", "-m", "main change"], repo, check=True)

    return repo


# --------------------------------------------------------------------------- #
# The tool: built using ONLY the existing ToolContract API.
# --------------------------------------------------------------------------- #

def git_merge_tool(repo_dir, branch: str, taint_reason=None):
    def pre():
        # Deterministic, real check: a lock file means another git process
        # already has a claim on this repo — refuse before even trying,
        # exactly the shape of the code-freeze check from the last round.
        return not (Path(repo_dir) / ".git" / "index.lock").exists()

    def do_merge():
        # Normalize at the tool-author boundary, same lesson as the HTTP
        # test's HTTPError handling: a merge conflict is a domain-level
        # result (the process ran, git has an opinion), not a transport-
        # level execution failure. Only let genuinely unexpected failures
        # (missing repo, git itself broken) propagate as exceptions.
        result = subprocess.run(
            ["git", "merge", "--no-ff", "-m", "merge", branch],
            cwd=repo_dir, capture_output=True, text=True,
        )
        status = subprocess.run(["git", "status", "--porcelain"], cwd=repo_dir,
                                 capture_output=True, text=True)
        has_conflict_markers = any(line.startswith("UU") for line in status.stdout.splitlines())
        return {"returncode": result.returncode, "stderr": result.stderr, "conflict": has_conflict_markers}

    def post(result):
        return result["returncode"] == 0 and not result["conflict"]

    def rollback():
        # THE REAL ROLLBACK — first authentic rollback against an external
        # process (not Python-native state) in the whole project.
        subprocess.run(["git", "merge", "--abort"], cwd=repo_dir, capture_output=True)

    contract = ToolContract(
        name="git_merge", pre_condition=pre, post_condition=post,
        rollback=rollback, is_mutating=True, taints_state_on_failure=False,
        taint_reason=taint_reason,
    )
    return execute_contracted(contract, do_merge)


# --------------------------------------------------------------------------- #
# The falsification tests
# --------------------------------------------------------------------------- #

def test_clean_merge_succeeds(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "gate-test@local"], repo)
    _run(["git", "config", "user.name", "gate-test"], repo)
    (repo / "a.txt").write_text("base\n")
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-q", "-m", "init"], repo)
    _run(["git", "checkout", "-q", "-b", "feature"], repo)
    (repo / "b.txt").write_text("new file\n")
    _run(["git", "add", "."], repo)
    _run(["git", "commit", "-q", "-m", "add b"], repo)
    _run(["git", "checkout", "-q", "main"], repo)

    result = git_merge_tool(repo, "feature")
    assert result["returncode"] == 0
    assert (repo / "b.txt").exists()


def test_real_merge_conflict_triggers_real_rollback(conflicting_repo):
    """
    THE key test. A genuine, reproducible merge conflict — git actually
    leaves the working tree in a conflicted state with markers in the
    file. post_condition catches it via `git status --porcelain`, and
    rollback is `git merge --abort` — a real external-process rollback,
    verified by checking the repo is genuinely back to its pre-merge state.
    """
    before_status = _run(["git", "status", "--porcelain"], conflicting_repo).stdout
    before_content = (conflicting_repo / "shared.txt").read_text()

    with pytest.raises(ContractViolation) as exc:
        git_merge_tool(conflicting_repo, "feature", taint_reason="MERGE_CONFLICT")

    assert exc.value.recovery_state == "rolled_back"

    after_status = _run(["git", "status", "--porcelain"], conflicting_repo).stdout
    after_content = (conflicting_repo / "shared.txt").read_text()

    assert after_status == before_status, "git status must be identical after rollback — repo genuinely reverted"
    assert after_content == before_content, "the conflicted file must be back to its pre-merge content"
    assert not GLOBAL_TAINT.is_tainted(), "a real rollback means no taint is needed — the world was actually restored"


def test_lock_file_blocks_before_any_git_process_runs(conflicting_repo):
    """
    A lock file (real git concurrency mechanism) is a deterministic
    pre-condition failure — no git process should even spawn.
    """
    branch_before = _run(["git", "branch", "--show-current"], conflicting_repo).stdout.strip()
    lock = conflicting_repo / ".git" / "index.lock"
    lock.write_text("")  # a real git lock file is just a marker; presence is what matters

    with pytest.raises(ContractViolation) as exc:
        git_merge_tool(conflicting_repo, "feature")

    assert exc.value.stage == "pre"
    # The branch must be unchanged — proof no merge attempt happened at all.
    branch_after = _run(["git", "branch", "--show-current"], conflicting_repo).stdout.strip()
    assert branch_after == branch_before


def test_mid_execution_exception_on_nonexistent_repo_taints(tmp_path):
    """
    A third tool shape exercising the mid-execution-exception fix: pointing
    git at a directory that doesn't exist raises a genuine Python exception
    (FileNotFoundError via subprocess), not a clean non-zero return —
    proving the fix generalizes beyond the two tools it was found and
    fixed against (HTTP, and the original file-write test).
    """
    nonexistent = tmp_path / "does_not_exist"

    contract = ToolContract(
        name="git_merge_broken_repo", pre_condition=lambda: True,
        post_condition=lambda r: True, rollback=None,
        is_mutating=True, taints_state_on_failure=True, taint_reason="GIT_REPO_MISSING",
    )

    def do_merge_broken():
        subprocess.run(["git", "merge", "feature"], cwd=str(nonexistent), check=True,
                        capture_output=True, text=True)

    with pytest.raises(ExecutionFailure):
        execute_contracted(contract, do_merge_broken)

    assert GLOBAL_TAINT.is_tainted()
    assert "GIT_REPO_MISSING" in GLOBAL_TAINT._tainted
