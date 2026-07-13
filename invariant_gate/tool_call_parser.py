"""
invariant_gate/tool_call_parser.py
    The wire-format contract between any model and this project's tools
    (PROTOCOL.md §4). Extracted as its own module because it is the
    one piece of the original model-selection work actually coupled to
    the primitive: tool_executor.py's gate is only reachable through a
    correctly-parsed {"tool": ..., "args": ...} call, regardless of which
    model — or benchmarking process for picking one — eventually sits
    upstream of it. That selection question is a "mind" concern and is
    out of scope for the current direction; this parser is a "hands"
    concern and stays.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger("invariant_gate.tool_call_parser")


def parse_with_fallback(raw_text: str) -> Optional[dict]:
    """
    Robust tool-call parser. Small models often wrap JSON in markdown
    fences, add a stray sentence, or use single quotes. This tries
    progressively looser strategies rather than failing the whole call on
    a cosmetic formatting slip — while still returning None, not a guess,
    when the text isn't a tool call at all.
    """
    text = raw_text.strip()

    # Strategy 1: direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Strategy 2: strip markdown code fences
    fenced = re.sub(r"^```(json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(fenced)
    except json.JSONDecodeError:
        pass

    # Strategy 3: extract the first {...} block greedily
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        candidate = match.group(0)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # Strategy 4: single-quote to double-quote repair, last resort
            repaired = candidate.replace("'", '"')
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass

    logger.debug("parse_with_fallback exhausted all strategies on: %r", text[:200])
    return None
