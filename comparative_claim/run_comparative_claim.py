"""
Comparative claim runner — executes the labeled scenario set through the
REAL umpire ToolContract/execute_contracted mechanism and reports
how gate decisions line up against ground-truth labels.

SCOPING NOTE: umpire's gate is a deterministic pre_condition/
post_condition check, not a continuous confidence score. arXiv:2606.08919
evaluates guards using selective-classification framing (risk vs.
coverage across a sweepable threshold), which assumes a scalar confidence
signal to threshold. That doesn't directly apply here — there's no
threshold to sweep. The honest comparison is a confusion matrix: did the
gate's allow/refuse decision match the ground-truth label.

Run from repo root:
    PYTHONPATH=. python3 comparative_claim/run_comparative_claim.py
"""

import sys
import os
import importlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from umpire.tool_executor import execute_contracted, ToolContract

SCENARIO_MODULE = sys.argv[1] if len(sys.argv) > 1 else "comparative_scenarios"
_scenarios_mod = importlib.import_module(SCENARIO_MODULE)
SCENARIOS = _scenarios_mod.SCENARIOS


def run_scenario(scenario):
    contract = ToolContract(
        name=scenario["name"],
        pre_condition=scenario["pre_condition"],
        post_condition=scenario["post_condition"],
    )
    try:
        execute_contracted(contract, scenario["operation"])
        return "allowed"
    except Exception as e:
        msg = str(e)
        if "stage=pre" in msg or "ContractViolation" in msg:
            return "refused"
        return "failed"


def main():
    print(f"Running scenario set: {SCENARIO_MODULE}\n")
    results = []
    for scenario in SCENARIOS:
        outcome = run_scenario(scenario)
        results.append({
            "name": scenario["name"],
            "label": scenario["label"],
            "outcome": outcome,
        })

    safe_correct = safe_total = 0
    risky_correct = risky_total = 0
    ambiguous_allowed = ambiguous_refused = ambiguous_failed = ambiguous_total = 0

    print(f"{'name':<40} {'label':<10} {'outcome':<10}")
    print("-" * 62)
    for r in results:
        print(f"{r['name']:<40} {r['label']:<10} {r['outcome']:<10}")

        if r["label"] == "safe":
            safe_total += 1
            if r["outcome"] == "allowed":
                safe_correct += 1
        elif r["label"] == "risky":
            risky_total += 1
            if r["outcome"] in ("refused", "failed"):
                risky_correct += 1
        elif r["label"] == "ambiguous":
            ambiguous_total += 1
            if r["outcome"] == "allowed":
                ambiguous_allowed += 1
            elif r["outcome"] == "refused":
                ambiguous_refused += 1
            else:
                ambiguous_failed += 1

    print("\n--- Summary ---")
    if safe_total:
        print(f"Safe scenarios correctly allowed:   {safe_correct}/{safe_total} "
              f"({100 * safe_correct / safe_total:.0f}%)  [false-refusal rate: "
              f"{100 * (safe_total - safe_correct) / safe_total:.0f}%]")
    if risky_total:
        print(f"Risky scenarios correctly refused:  {risky_correct}/{risky_total} "
              f"({100 * risky_correct / risky_total:.0f}%)  [false-allow rate: "
              f"{100 * (risky_total - risky_correct) / risky_total:.0f}% "
              f"<- the costly error class]")
    if ambiguous_total:
        print(f"Ambiguous scenarios — split (no ground truth, reported not scored):")
        print(f"  allowed: {ambiguous_allowed}/{ambiguous_total}, "
              f"refused: {ambiguous_refused}/{ambiguous_total}, "
              f"failed: {ambiguous_failed}/{ambiguous_total}")

    print("\nScoping reminder: this is a self-authored 20-scenario first pass,")
    print("not a replication of arXiv:2606.08919's 125-item, multi-rater study.")
    print("See WRITEUP_NOTES.md for the full scoping discussion before citing.")

    return results


if __name__ == "__main__":
    main()