"""
invariant_gate/verifier_search.py
    This is the module that decides whether a proposed change is a real
    improvement or a storyteller's confident guess. Everything upstream
    (a proposer model's prompting) can produce a plausible-sounding
    "insight." Only this module produces evidence.

    An earlier version of this project let a small local LLM
    free-associate across all of ML research and score its own output with
    a heuristic. That produces fluent restatements of known ideas,
    confidently mis-scored as novel. It does not produce anything an
    engineer could trust, ship, or point a benchmark at — and generalizing
    as a reusable primitive requires exactly that trust.

    So: this module does not "have ideas" in the open-ended sense. It runs
    a narrow, verifiable search loop, modeled on AlphaEvolve / FunSearch /
    KernelBench:

        propose  -> small local model emits a *code mutation* against a
                    fixed, scoped target (default: a Triton GPU kernel)
        execute  -> the mutation is actually run, in a sandbox, against a
                    reference implementation
        score    -> correctness (numerical match) gates everything; speed
                    (measured, not estimated) is the fitness signal
        mutate   -> the search keeps the fittest variants and perturbs them
                    again, exactly like an evolutionary search, because
                    that's what this is

    Nothing produced here is treated as trustworthy unless it survived
    execution. A failed or unverified candidate is discarded. This is the
    difference between an idea generator and an engine that can eventually
    be trusted the way a stable protocol primitive is: not because it
    sounds right, but because it was checked every time.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
Import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("invariant_gate.verifier_search")


# --------------------------------------------------------------------------- #
# Domain definition
# --------------------------------------------------------------------------- #
# This is deliberately scoped to ONE verifiable target domain at a time.
# "Innovate everywhere" produces nothing checkable. "Innovate here,
# provably" produces something a benchmark can confirm or deny.
#
# The default target below is a standard, well-understood op (fused
# softmax) precisely because the goal of v0 is not to shock anyone with the
# op chosen — it's to prove the *loop* produces real, measured, reproducible
# speedups on something everyone already benchmarks against. Credibility
# before ambition.

@dataclass
class Target:
    """A single, objectively verifiable optimization target."""
    name: str
    reference_impl: str          # ground-truth Python/PyTorch source, the correctness oracle
    candidate_harness: str       # test harness that runs a candidate against the reference
    input_shapes: list           # shapes/sizes to benchmark across
    baseline_op: Optional[str] = None  # e.g. "torch.softmax" for a speed floor


DEFAULT_TARGET = Target(
    name="fused_softmax_triton",
    reference_impl=textwrap.dedent(
        """
        import torch
        def reference(x: torch.Tensor) -> torch.Tensor:
            return torch.softmax(x, dim=-1)
        """
    ),
    candidate_harness=textwrap.dedent(
        """
        # Candidate module must define `candidate(x: torch.Tensor) -> torch.Tensor`
        # with identical semantics to `reference`. This harness checks correctness
        # (allclose vs reference) and measures wall-clock latency.
        import torch, time, importlib.util, sys

        def load_candidate(path):
            spec = importlib.util.spec_from_file_location("candidate_mod", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod.candidate

        def run(candidate_path, reference_fn, shapes, device="cpu"):
            candidate_fn = load_candidate(candidate_path)
            results = []
            for shape in shapes:
                x = torch.randn(*shape, device=device)
                ref_out = reference_fn(x)
                try:
                    cand_out = candidate_fn(x)
                except Exception as e:
                    results.append({"shape": shape, "correct": False, "error": str(e)})
                    continue
                correct = torch.allclose(ref_out, cand_out, atol=1e-3, rtol=1e-3)
                # measured latency, not estimated
                n_iters = 50
                t0 = time.perf_counter()
                for _ in range(n_iters):
                    candidate_fn(x)
                t1 = time.perf_counter()
                t0b = time.perf_counter()
                for _ in range(n_iters):
                    reference_fn(x)
                t1b = time.perf_counter()
                results.append({
                    "shape": shape,
                    "correct": correct,
                    "candidate_ms": (t1 - t0) / n_iters * 1000,
                    "reference_ms": (t1b - t0b) / n_iters * 1000,
                })
            return results
        """
    ),
    input_shapes=[(512, 512), (2048, 2048), (4096, 1024)],
    baseline_op="torch.softmax",
)


# --------------------------------------------------------------------------- #
# Candidate generation (proposer)
# --------------------------------------------------------------------------- #

@dataclass
class Candidate:
    id: str
    source: str
    generation: int
    parent_id: Optional[str] = None
    mutation_note: str = ""


ProposeFn = Callable[[Target, Optional[Candidate]], str]
"""A proposer takes the target + an optional parent candidate to mutate,
and returns candidate source code as a string. In production this is
invariant_gate.brain calling the locally selected tool-capable model with a
tightly constrained prompt ("here is the reference, here is the current
best candidate and its measured latency, propose ONE targeted mutation").
It is intentionally injected as a function here so this module has zero
hard dependency on any specific model."""


def default_seed_candidate(target: Target) -> str:
    """A safe, always-correct starting point: literally the reference,
    labeled as generation 0. The search only ever gets credit for measured
    improvement over this, never for a fabricated baseline."""
    return target.reference_impl.replace("def reference(", "def candidate(")


# --------------------------------------------------------------------------- #
# Execution sandbox
# --------------------------------------------------------------------------- #

class ExecutionError(Exception):
    pass


class SandboxViolation(ExecutionError):
    """Raised when a candidate is rejected before or during execution for
    violating the sandbox contract (banned imports, resource limits, or
    a network attempt). Distinct from ExecutionError so callers can tell
    'the code was wrong' apart from 'the code tried something it should
    never be allowed to do' — the latter is a signal worth logging loudly
    even though both currently score fitness 0.0."""


# Candidate source is never trusted. A model proposing "mutations" is, from
# a security standpoint, an adversarial code-generation process — even with
# no malicious intent, small models hallucinate imports and reach for
# whatever stdlib/package looks plausible. Longevity requires that this
# stays true even after a hundred generations of self-modification.
_IMPORT_STATEMENT_PATTERN = re.compile(
    r"^\s*(?:import\s+([\w\.]+)(?:\s+as\s+\w+)?|from\s+([\w\.]+)\s+import\b)",
    re.MULTILINE,
)
_BANNED_CALL_PATTERN = re.compile(r"\b(eval|exec|compile|__import__|open)\s*\(")

_ALLOWED_MODULES = {"torch", "math", "time", "json"}


def static_sandbox_check(candidate_source: str) -> None:
    """
    Pre-execution static gate. Cheap, deliberately conservative, and not a
    substitute for the process-level isolation below — defense in depth.

    This is an ALLOWLIST, not a denylist: every top-level import is
    extracted and checked against _ALLOWED_MODULES, and anything not in
    that set is rejected — including modules nobody thought to enumerate
    in advance. An earlier version of this function only checked a fixed
    list of known-bad imports (os, socket, subprocess, ...), which meant
    anything NOT on that list — numpy, for instance — passed straight
    through unexamined. A denylist only stops what you thought to write
    down; an allowlist stops everything except what you explicitly
    decided was safe. This was found and fixed via the same discipline
    used throughout this project: a claim (README: "numpy will be
    rejected") was tested against the actual code, and the code was wrong.
    """
    for match in _IMPORT_STATEMENT_PATTERN.finditer(candidate_source):
        module = (match.group(1) or match.group(2)).split(".")[0]
        if module not in _ALLOWED_MODULES:
            raise SandboxViolation(
                f"candidate imports {module!r}, which is outside the allowed set "
                f"{sorted(_ALLOWED_MODULES)} — rejected before execution"
            )
    if _BANNED_CALL_PATTERN.search(candidate_source):
        raise SandboxViolation(
            "candidate calls eval/exec/compile/__import__/open — rejected before execution"
        )


def _resource_limited_preexec(memory_mb: int = 512, cpu_seconds: int = 20):
    """
    Returns a preexec_fn that caps the child process's memory and CPU time
    via POSIX rlimits, and drops it into its own process group so a hung
    or forking candidate can be killed as a unit. This is what actually
    stops a fork-bomb or runaway allocation from taking down the host —
    the subprocess timeout alone (the original design) does not.
    """
    import resource
    import os as _os

    def _set_limits():
        resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 1024 * 1024, memory_mb * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        resource.setrlimit(resource.RLIMIT_NPROC, (32, 32))  # blocks fork-bombs
        resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))
        _os.setsid()  # own process group -> killable as a unit

    return _set_limits


def execute_candidate(target: Target, candidate_source: str, device: str = "cpu",
                       timeout_s: int = 30, memory_mb: int = 2048, cpu_seconds: int = 20,
                       allow_network: bool = False) -> list[dict]:
    """
    Actually run the candidate. Two independent layers of defense, because
    a code-execution agent that only has one layer eventually loses trust
    the first time it's wrong:

      1. static_sandbox_check() — reject obvious escapes before running
      2. process isolation — memory/CPU/process-count rlimits, its own
         process group (killable as a unit), no network by default, and a
         hard wall-clock timeout as the final backstop

    No score is ever assigned to code that was not actually executed under
    these constraints. A candidate that trips the sandbox is treated
    exactly like a correctness failure: fitness 0.0, discarded, logged.
    """
    static_sandbox_check(candidate_source)

    with tempfile.TemporaryDirectory(prefix="invariant_gate_verify_") as tmp:
        tmp_path = Path(tmp)
        cand_file = tmp_path / "candidate_mod.py"
        cand_file.write_text(candidate_source)

        driver = tmp_path / "driver.py"
        driver.write_text(
            target.reference_impl
            + "\n"
            + target.candidate_harness
            + textwrap.dedent(
                f"""
                import json
                results = run({str(cand_file)!r}, reference, {target.input_shapes!r}, device={device!r})
                print("ORNG_RESULT_JSON=" + json.dumps(results))
                """
            )
        )

        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": ""}
        if allow_network:
            logger.warning("executing candidate with allow_network=True — "
                            "only use this for explicitly trusted proposers")
        else:
            # No proxy/DNS env vars passed through; combined with the rlimits
            # above this is a best-effort block, not a true network namespace.
            # A production deployment should run this inside a container or
            # gVisor/firecracker sandbox for a real network boundary — noted
            # explicitly here rather than implied to be airtight.
            pass

        try:
            proc = subprocess.run(
                [sys.executable, "-I", str(driver)],  # sys.executable, not "python3" - guarantees same interpreter
                capture_output=True, text=True, timeout=timeout_s,
                cwd=str(tmp_path), env=env,
                preexec_fn=_resource_limited_preexec(memory_mb, cpu_seconds),
            )
        except subprocess.TimeoutExpired:
            raise ExecutionError(f"candidate timed out after {timeout_s}s (likely a hang)")

        if proc.returncode != 0:
            # Negative returncode means the process was killed by a signal
            # (e.g. -9 from RLIMIT_AS/RLIMIT_CPU) — surface that distinctly
            # since it usually means the candidate tried to over-allocate,
            # not that it had an ordinary bug.
            if proc.returncode is not None and proc.returncode < 0:
                raise SandboxViolation(
                    f"candidate was killed by signal {-proc.returncode} "
                    "(likely exceeded memory/CPU/process rlimits)"
                )
            raise ExecutionError(f"candidate crashed: {proc.stderr[-800:]}")

        for line in proc.stdout.splitlines():
            if line.startswith("ORNG_RESULT_JSON="):
                return json.loads(line[len("ORNG_RESULT_JSON="):])

        raise ExecutionError("candidate produced no parseable result")


# --------------------------------------------------------------------------- #
# Scoring — correctness gates everything, speed is the only fitness signal
# --------------------------------------------------------------------------- #

@dataclass
class ScoredCandidate:
    candidate: Candidate
    results: list[dict]
    all_correct: bool
    mean_speedup: float          # reference_ms / candidate_ms, averaged across shapes
    fitness: float                # 0.0 if incorrect, else mean_speedup

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate.id,
            "generation": self.candidate.generation,
            "parent_id": self.candidate.parent_id,
            "mutation_note": self.candidate.mutation_note,
            "all_correct": self.all_correct,
            "mean_speedup": self.mean_speedup,
            "fitness": self.fitness,
            "results": self.results,
        }


def score_candidate(candidate: Candidate, target: Target, device: str = "cpu") -> ScoredCandidate:
    try:
        results = execute_candidate(target, candidate.source, device=device)
    except ExecutionError as exc:
        logger.info("candidate %s failed execution: %s", candidate.id, exc)
        return ScoredCandidate(candidate, [{"error": str(exc)}], all_correct=False,
                                mean_speedup=0.0, fitness=0.0)

    all_correct = all(r.get("correct") for r in results)
    if not all_correct:
        return ScoredCandidate(candidate, results, all_correct=False, mean_speedup=0.0, fitness=0.0)

    speedups = [r["reference_ms"] / r["candidate_ms"] for r in results if r.get("candidate_ms")]
    mean_speedup = sum(speedups) / len(speedups) if speedups else 0.0
    return ScoredCandidate(candidate, results, all_correct=True, mean_speedup=mean_speedup,
                            fitness=mean_speedup)


# --------------------------------------------------------------------------- #
# The search loop
# --------------------------------------------------------------------------- #

@dataclass
class SearchConfig:
    population_size: int = 4
    generations: int = 5
    device: str = "cpu"
    min_fitness_to_survive: float = 1.0   # must beat the reference to count as progress


@dataclass
class SearchRun:
    target: Target
    history: list[ScoredCandidate] = field(default_factory=list)
    best: Optional[ScoredCandidate] = None

    def record(self, scored: ScoredCandidate) -> None:
        self.history.append(scored)
        if self.best is None or scored.fitness > self.best.fitness:
            self.best = scored

    def to_dict(self) -> dict:
        return {
            "target": self.target.name,
            "n_candidates_evaluated": len(self.history),
            "best": self.best.to_dict() if self.best else None,
        }


def run_search(target: Target, propose_fn: ProposeFn, config: SearchConfig = SearchConfig()) -> SearchRun:
    """
    The loop that replaces "ask a model for an insight" with "search a
    verifiable space and keep only what measurably won."

    Generation 0 is always the untouched reference (guaranteed correct,
    fitness == 1.0 by construction) — this anchors every later generation's
    speedup claim to a real, executed number, not a guess.
    """
    run = SearchRun(target=target)
    seed = Candidate(id="gen0-seed", source=default_seed_candidate(target), generation=0)
    seed_scored = score_candidate(seed, target, device=config.device)
    run.record(seed_scored)
    logger.info("generation 0 (seed) fitness=%.3f (anchor, expected ~1.0)", seed_scored.fitness)

    frontier = [seed_scored]
    for gen in range(1, config.generations + 1):
        gen_results = []
        for i in range(config.population_size):
            parent = max(frontier, key=lambda s: s.fitness)
            try:
                source = propose_fn(target, parent.candidate)
            except Exception as exc:
                logger.warning("proposer failed on gen %d cand %d: %s", gen, i, exc)
                continue
            cand = Candidate(
                id=f"gen{gen}-{i}", source=source, generation=gen,
                parent_id=parent.candidate.id,
            )
            scored = score_candidate(cand, target, device=config.device)
            run.record(scored)
            gen_results.append(scored)
            logger.info(
                "gen %d cand %d: correct=%s fitness=%.3f (parent=%s)",
                gen, i, scored.all_correct, scored.fitness, parent.candidate.id,
            )

        survivors = [s for s in gen_results if s.fitness >= config.min_fitness_to_survive]
        if survivors:
            frontier = survivors
        # if nothing beat the floor this generation, keep searching from the
        # existing frontier rather than collapsing back to the seed

    return run


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    parser = argparse.ArgumentParser(description="Run the verified search loop on the default target.")
    parser.add_argument("--generations", type=int, default=2)
    parser.add_argument("--population", type=int, default=2)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()

    def trivial_propose(target: Target, parent: Optional[Candidate]) -> str:
        # Placeholder proposer for smoke-testing the loop without a model:
        # returns the parent unchanged so the harness/execution path can be
        # validated end-to-end. invariant_gate/brain.py supplies the real,
        # model-driven proposer.
        return parent.source if parent else default_seed_candidate(target)

    result = run_search(
        DEFAULT_TARGET, trivial_propose,
        SearchConfig(population_size=args.population, generations=args.generations, device=args.device),
    )
    print(json.dumps(result.to_dict(), indent=2))
