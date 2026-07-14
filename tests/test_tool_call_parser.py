"""
tests/test_tool_call_parser.py (pytest)
    Proves parse_with_fallback handles the realistic ways a small model
    mangles JSON tool-call output, per PROTOCOL.md §4.
"""

from statecheck.tool_call_parser import parse_with_fallback


def test_parser_handles_clean_json():
    assert parse_with_fallback('{"tool": "run_benchmark", "args": {"target": "x"}}') == \
        {"tool": "run_benchmark", "args": {"target": "x"}}


def test_parser_handles_markdown_fences():
    raw = '```json\n{"tool": "write_file", "args": {"path": "a.py"}}\n```'
    assert parse_with_fallback(raw) == {"tool": "write_file", "args": {"path": "a.py"}}


def test_parser_handles_surrounding_prose():
    raw = 'Sure, here is the call: {"tool": "query_graph", "args": {"top_k": 3}} hope that helps!'
    assert parse_with_fallback(raw) == {"tool": "query_graph", "args": {"top_k": 3}}


def test_parser_handles_single_quotes():
    raw = "{'tool': 'score_candidate', 'args': {'candidate_id': 'gen3-1'}}"
    assert parse_with_fallback(raw) == {"tool": "score_candidate", "args": {"candidate_id": "gen3-1"}}


def test_parser_returns_none_for_non_json():
    assert parse_with_fallback("not json at all, sorry") is None
