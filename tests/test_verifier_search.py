"""
tests/test_verifier_search.py (pytest)
    Proves the core search mechanics independent of what proposes mutations:
    a broken candidate scores exactly 0 and is never selected; correct
    candidates are ranked by real measured wall-clock, not by how
    "optimized" the code looks.
"""

import textwrap

from invariant_gate.verifier_search import (
    DEFAULT_TARGET, Candidate, SearchRun, score_candidate,
)

VARIANTS = {
    "broken": textwrap.dedent("""
        import torch
        def candidate(x: torch.Tensor) -> torch.Tensor:
            return x  # deliberately wrong
    """),
    "naive_loop_slow": textwrap.dedent("""
        import torch
        def candidate(x: torch.Tensor) -> torch.Tensor:
            out = torch.empty_like(x)
            for i in range(x.shape[0]):
                row = x[i]
                m = row.max()
                e = torch.exp(row - m)
                out[i] = e / e.sum()
            return out
    """),
    "vectorized_manual": textwrap.dedent("""
        import torch
        def candidate(x: torch.Tensor) -> torch.Tensor:
            m = x.max(dim=-1, keepdim=True).values
            e = torch.exp(x - m)
            return e / e.sum(dim=-1, keepdim=True)
    """),
    "reference_direct": textwrap.dedent("""
        import torch
        def candidate(x: torch.Tensor) -> torch.Tensor:
            return torch.softmax(x, dim=-1)
    """),
}


def _run_all_variants():
    target = DEFAULT_TARGET
    run = SearchRun(target=target)
    for name, source in VARIANTS.items():
        cand = Candidate(id=name, source=source, generation=1, mutation_note=name)
        scored = score_candidate(cand, target, device="cpu")
        run.record(scored)
    return {s.candidate.id: s for s in run.history}, run


def test_broken_candidate_scores_zero_and_fails_correctness():
    by_name, _ = _run_all_variants()
    assert by_name["broken"].fitness == 0.0
    assert by_name["broken"].all_correct is False


def test_correct_variants_pass_correctness():
    by_name, _ = _run_all_variants()
    assert by_name["naive_loop_slow"].all_correct is True
    assert by_name["vectorized_manual"].all_correct is True
    assert by_name["reference_direct"].all_correct is True


def test_search_never_selects_broken_candidate():
    _, run = _run_all_variants()
    assert run.best.candidate.id != "broken"


def test_selection_tracks_real_measured_performance():
    by_name, _ = _run_all_variants()
    # This is the actual claim under test: a python for-loop is genuinely
    # slower than a vectorized op, and the harness must reflect that via
    # real wall-clock timing, not a static heuristic.
    assert by_name["vectorized_manual"].fitness > by_name["naive_loop_slow"].fitness
