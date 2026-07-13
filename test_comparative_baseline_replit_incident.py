"""
tests/test_comparative_baseline_replit_incident.py (pytest)
    Grounds the "Comparative Utility" question in a real, documented
    incident rather than an invented friction-vs-safety number: the July
    2025 Replit case (Fortune, The Register, OECD AI Incident DB #1152).
    An agent was told, in an active "code freeze," in all caps, repeatedly,
    not to modify the production database. It deleted it anyway, then
    told the user rollback was impossible — which was false.

    The precise mechanical claim under test: a natural-language instruction
    ("do not touch the database") and a confirmation-prompt regime are BOTH
    ultimately something the agent or a fatigued human can choose to
    override. A deterministic pre_condition, evaluated in code before the
    operation runs, cannot be talked past — it either evaluates true or it
    doesn't.

    Honest limits, stated up front:
      - This is one author (me) modeling both sides of the comparison, the
        same "Author's Paradox" flagged in every primitive-generalization
        test so far. It is illustrative, not a user study.
      - It does not use the more rigorous evaluation apparatus that exists
        for this exact question — see "Oversight Has a Capacity:
        Calibrating Agent Guards to a Subjective, Fatiguing Human"
        (arXiv:2606.08919), which frames this as selective classification
        under asymmetric cost (Neyman-Pearson operating point, AURC) rather
        than a pass/fail count. That is the standard a real comparative
        claim should eventually be measured against.
      - "Always-allow" is cited here (Wang et al., arXiv:2605.24309) as the
        dominant real production pattern specifically because per-action
        confirmation causes fatigue — not asserted from intuition.
"""

import pytest

from invariant_gate import tool_executor as te
from invariant_gate.tool_executor import ContractViolation, GLOBAL_TAINT


@pytest.fixture(autouse=True)
def _reset():
    GLOBAL_TAINT.clear()
    GLOBAL_TAINT._recovery_registry.clear()
    yield
    GLOBAL_TAINT.clear()
    GLOBAL_TAINT._recovery_registry.clear()


class ProductionDatabase:
    """Minimal stand-in for the thing that actually got deleted."""
    def __init__(self):
        self.records = {"executives": 1206, "companies": 1196}
        self.deleted = False

    def delete_all(self):
        self.records = {}
        self.deleted = True


def test_natural_language_instruction_alone_does_not_prevent_the_action():
    """
    Models what actually happened: a freeze is communicated as an
    instruction the agent is expected to honor, not as a code-level gate.
    Per the incident transcript, the agent was told this eleven times in
    all caps and proceeded regardless — a natural-language directive is
    advisory, not enforced.
    """
    db = ProductionDatabase()
    freeze_instruction = "DO NOT MODIFY THE PRODUCTION DATABASE. CODE FREEZE IN EFFECT."

    def naive_agent_delete(instruction_seen: str):
        # This is the actual failure mode: the instruction exists in
        # context, but nothing in the code path checks it before acting.
        db.delete_all()
        return {"status": "success"}

    naive_agent_delete(freeze_instruction)
    assert db.deleted is True, (
        "this reproduces the real incident's outcome: an instruction with "
        "no enforcement mechanism did not prevent the destructive action"
    )


def test_confirmation_prompt_regime_does_not_structurally_prevent_it_either():
    """
    Models the "ask for confirmation" baseline under the realistic
    operating point the literature reports: always-allow / auto-approval
    is the dominant production pattern precisely because per-action
    confirmation is fatiguing (Wang et al., arXiv:2605.24309 — "sandbox
    auto-approval cuts Claude Code prompt count by 84%"; "always-allow...
    is the most common production strategy and the one with the worst
    long-term security profile"). Modeling the realistic case, not the
    ideal one: a fatigued/auto-approving operator.
    """
    db = ProductionDatabase()

    def confirm(action_description: str) -> bool:
        # The realistic operating point per the cited literature, not a
        # worst-case strawman: always-allow is the dominant real pattern.
        return True

    def agent_delete_with_confirmation():
        if confirm("Delete production database?"):
            db.delete_all()
        return {"status": "success"}

    agent_delete_with_confirmation()
    assert db.deleted is True, (
        "a confirmation step under the realistic (always-allow) operating "
        "point provides no more structural prevention than no gate at all"
    )


def test_deterministic_pre_condition_blocks_it_regardless_of_agent_intent():
    """
    The actual comparison: the same destructive operation, now wrapped in
    ToolContract with a pre_condition checking a code-level freeze flag —
    not a string in a prompt. This cannot be talked past, panicked past, or
    rubber-stamped past; it is evaluated in code before fn() ever runs.
    """
    db = ProductionDatabase()
    freeze_state = {"active": True}

    def delete_all():
        db.delete_all()
        return {"status": "success"}

    contract = te.ToolContract(
        name="delete_production_database",
        pre_condition=lambda: not freeze_state["active"],
        post_condition=None, rollback=None,
        is_mutating=True, taints_state_on_failure=False,
    )

    with pytest.raises(ContractViolation) as exc:
        te.execute_contracted(contract, delete_all)

    assert exc.value.stage == "pre"
    assert db.deleted is False, (
        "the deterministic pre_condition prevented the operation from "
        "running at all — no panic, no misjudgment, no confirmation to "
        "rubber-stamp, because there was no step where a choice was made"
    )


def test_pre_condition_gate_is_indifferent_to_the_agents_stated_reasoning():
    """
    Per the real transcript, the agent later explained it "panicked" upon
    seeing an empty query result and judged deletion safe. A deterministic
    gate doesn't evaluate the agent's stated reasoning at all — this test
    proves that explicitly, by having the "agent" argue for the deletion
    at length and showing it has zero bearing on the outcome.
    """
    db = ProductionDatabase()
    freeze_state = {"active": True}

    agent_justification = (
        "I saw an empty query result and concluded the database was "
        "already in an inconsistent state, so I judged it safe to reset it "
        "rather than risk further corruption. This was a reasonable "
        "engineering judgment under uncertainty."
    )

    def delete_all():
        db.delete_all()
        return {"status": "success", "agent_reasoning": agent_justification}

    contract = te.ToolContract(
        name="delete_production_database",
        pre_condition=lambda: not freeze_state["active"],
        is_mutating=True,
    )

    with pytest.raises(ContractViolation):
        te.execute_contracted(contract, delete_all)

    assert db.deleted is False, (
        "no amount of plausible-sounding justification reaches the "
        "pre_condition — it was never given the opportunity to evaluate "
        "the agent's reasoning, only the freeze_state flag"
    )
