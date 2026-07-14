"""
External-validation candidate: wrap a real Hugging Face Inference API
call (text-classification, confirmed live on hf-inference) in a
ToolContract, using the actual invariant_gate interface.
"""

import os
import requests

from invariant_gate.tool_executor import execute_contracted, ToolContract, GLOBAL_TAINT

HF_TOKEN = os.environ.get("HF_TOKEN")
HF_MODEL_URL = "https://router.huggingface.co/hf-inference/models/distilbert/distilbert-base-uncased-finetuned-sst-2-english"


def call_hf_inference(text: str) -> dict:
    response = requests.post(
        HF_MODEL_URL,
        headers={"Authorization": f"Bearer {HF_TOKEN}"},
        json={"inputs": text},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def pre_condition() -> bool:
    if not HF_TOKEN:
        return False
    return True


def post_condition(result) -> bool:
    # Expected shape: [[{"label": "POSITIVE", "score": 0.99}, ...]]
    if not isinstance(result, list) or len(result) == 0:
        return False
    inner = result[0]
    if not isinstance(inner, list) or len(inner) == 0:
        return False
    if "label" not in inner[0] or "score" not in inner[0]:
        return False
    return True


def run_candidate_test(text: str = "I love working on this project."):
    contract = ToolContract(
        name="hf_sentiment_call",
        pre_condition=pre_condition,
        post_condition=post_condition,
    )
    result = execute_contracted(contract, call_hf_inference, text)
    print("Result:", result)
    print("Taint state after call:", GLOBAL_TAINT)
    return result


def run_adversarial_case_forced_bad_input():
    contract = ToolContract(
        name="hf_sentiment_call_bad_input",
        pre_condition=lambda: False,  # deliberately force refusal
        post_condition=post_condition,
    )
    try:
        execute_contracted(contract, call_hf_inference, "")
        print("FAIL: gate did not refuse an invalid pre_condition")
    except Exception as e:
        print(f"PASS: gate correctly refused — {e}")


if __name__ == "__main__":
    run_candidate_test()
    run_adversarial_case_forced_bad_input()