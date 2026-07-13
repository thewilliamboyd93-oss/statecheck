"""
tests/test_sandbox.py (pytest)
    Proves the pre-execution static sandbox check actually rejects common
    escape attempts before any candidate code runs.
"""

import pytest

from invariant_gate.verifier_search import static_sandbox_check, SandboxViolation


ADVERSARIAL_CASES = [
    ("import os\ndef candidate(x):\n    os.system('rm -rf /')\n    return x", "os_shell"),
    ("import socket\ndef candidate(x):\n    s = socket.socket()\n    return x", "network"),
    ("def candidate(x):\n    eval(\"__import__('os').system('ls')\")\n    return x", "eval_escape"),
    ("def candidate(x):\n    open('/etc/passwd').read()\n    return x", "filesystem_read"),
    ("import subprocess\ndef candidate(x):\n    subprocess.run(['ls'])\n    return x", "subprocess"),
    # Not a "known-bad" module in the old denylist sense — the point of the
    # allowlist fix is precisely that this doesn't need to be enumerated as
    # dangerous to be rejected. It's rejected for not being on the allowed
    # list, which is a categorically different (and stronger) guarantee.
    ("import numpy as np\ndef candidate(x):\n    return np.exp(x)", "unlisted_but_unapproved_module"),
]


@pytest.mark.parametrize("source,label", ADVERSARIAL_CASES, ids=[c[1] for c in ADVERSARIAL_CASES])
def test_adversarial_source_is_rejected(source, label):
    with pytest.raises(SandboxViolation):
        static_sandbox_check(source)


def test_legitimate_candidate_passes_static_check():
    benign = "import torch\ndef candidate(x):\n    return torch.softmax(x, dim=-1)\n"
    static_sandbox_check(benign)  # should not raise


def test_from_import_form_is_checked_against_the_same_allowlist():
    from invariant_gate.verifier_search import SandboxViolation

    static_sandbox_check("from math import sqrt\ndef candidate(x):\n    return sqrt(x)\n")  # allowed, no raise

    with pytest.raises(SandboxViolation):
        static_sandbox_check("from os import system\ndef candidate(x):\n    return x\n")


def test_dotted_submodule_import_resolves_to_its_top_level_package():
    from invariant_gate.verifier_search import SandboxViolation

    # os.path is still "os" at the top level — the allowlist check must
    # resolve the dotted form, not be fooled by checking the full string.
    with pytest.raises(SandboxViolation):
        static_sandbox_check("import os.path\ndef candidate(x):\n    return x\n")
