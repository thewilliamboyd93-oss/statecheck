"""
tests/test_primitive_generalization_http.py (pytest)
    The actual falsification test named in PROTOCOL.md §4: wrap a
    tool shape with a materially different failure mode than the four
    tools tool_executor.py already has (file write, sqlite, shell, search)
    and see whether ToolContract holds WITHOUT any change to
    statecheck/tool_executor.py itself.

    HTTP is the right first case because its failure modes are genuinely
    different from what's already covered: connection reset mid-request
    (not just "command exited non-zero"), rate limiting (a domain-specific
    post-condition), and timeout. This test builds a real local HTTP
    server with controllable failure injection — no mocking of the network
    layer, actual sockets — specifically so the mid-execution-exception fix
    from the last round gets exercised against a tool shape it wasn't
    designed against.

    If this file needs to import anything from tool_executor.py beyond
    ToolContract / execute_contracted / ExecutionFailure / ContractViolation
    / GLOBAL_TAINT, or needs those to change, the primitive does not
    generalize as cleanly as claimed.
"""

import http.server
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from statecheck.tool_executor import (
    ToolContract, execute_contracted, ExecutionFailure, ContractViolation, GLOBAL_TAINT,
)


# --------------------------------------------------------------------------- #
# A real, controllable local HTTP server. Behavior mode is shared mutable
# state read by the handler on each request.
# --------------------------------------------------------------------------- #

_MODE = {"value": "normal"}


class _ControllableHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep test output clean

    def do_POST(self):
        mode = _MODE["value"]
        length = int(self.headers.get("Content-Length", 0))

        if mode == "reset_mid_request":
            # Read only part of the body, then abruptly close the raw
            # socket with no response — simulates a real connection reset
            # partway through a POST, not a clean error response.
            self.rfile.read(min(5, length))
            self.connection.close()
            return

        if mode == "hang":
            time.sleep(5)  # longer than the client's timeout
            return

        body = self.rfile.read(length)

        if mode == "rate_limited":
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "rate_limited"}).encode())
            return

        # normal
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"received": body.decode()}).encode())


@pytest.fixture(scope="module")
def local_server():
    server = http.server.HTTPServer(("127.0.0.1", 0), _ControllableHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _reset(local_server):
    _MODE["value"] = "normal"
    GLOBAL_TAINT.clear()
    GLOBAL_TAINT._recovery_registry.clear()
    yield
    _MODE["value"] = "normal"
    GLOBAL_TAINT.clear()
    GLOBAL_TAINT._recovery_registry.clear()


# --------------------------------------------------------------------------- #
# The tool: http_post_tool, built using ONLY the existing ToolContract API.
# No rollback exists for a POST in the general case (can't un-send a
# request) so, exactly like shell_command_tool, it taints on failure.
# --------------------------------------------------------------------------- #

def http_post_tool(url: str, payload: dict, timeout_s: float = 1.0,
                    post_condition=None, taint_reason=None):
    def do_post():
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return {"status": resp.status, "body": json.loads(resp.read().decode())}
        except urllib.error.HTTPError as e:
            # urllib's default behavior raises for ANY non-2xx status — this
            # conflates "the transport succeeded but the server said no" with
            # "the transport itself failed," which is exactly the wrong
            # granularity for a domain-level post_condition to reason about.
            # Normalizing it here, at the tool-author boundary, is the
            # correct fix — NOT a change to tool_executor.py itself. A 429 is
            # a successful round-trip with a semantically-failed result; a
            # connection reset (below) is a genuine execution failure. These
            # are different things and this tool now treats them as such.
            body = e.read().decode() if e.fp else ""
            return {"status": e.code, "body": {"error": body} if body else {}}

    contract = ToolContract(
        name="http_post", pre_condition=lambda: True, post_condition=post_condition,
        rollback=None, is_mutating=True, taints_state_on_failure=True,
        taint_reason=taint_reason,
    )
    return execute_contracted(contract, do_post)


# --------------------------------------------------------------------------- #
# The falsification tests
# --------------------------------------------------------------------------- #

def test_normal_request_succeeds(local_server):
    result = http_post_tool(local_server, {"x": 1},
                             post_condition=lambda r: r["status"] == 200)
    assert result["status"] == 200
    assert not GLOBAL_TAINT.is_tainted()


def test_rate_limit_is_a_domain_specific_post_condition_failure(local_server):
    """
    Rate limiting is a genuinely new kind of post-condition failure: the
    request SUCCEEDED at the transport level (got a response), but the
    domain semantics (429) mean it must be treated as a failure. This
    tests whether post_condition, unmodified, correctly expresses that.
    """
    _MODE["value"] = "rate_limited"
    with pytest.raises(ContractViolation) as exc:
        http_post_tool(local_server, {"x": 1},
                        post_condition=lambda r: r["status"] == 200,
                        taint_reason="HTTP_RATE_LIMITED")
    assert exc.value.recovery_state == "tainted"
    assert GLOBAL_TAINT.is_tainted()
    assert "HTTP_RATE_LIMITED" in GLOBAL_TAINT._tainted


def test_connection_reset_mid_request_taints_via_the_execution_exception_path(local_server):
    """
    THE key test. A connection reset partway through a POST is a real
    mid-execution failure — urlopen() raises before do_post() ever
    returns, exactly the shape the last round's bug was found in. This
    exercises that fix against a tool it wasn't designed for.
    """
    _MODE["value"] = "reset_mid_request"
    with pytest.raises(ExecutionFailure):
        http_post_tool(local_server, {"x": 1}, timeout_s=2.0,
                        post_condition=lambda r: True,
                        taint_reason="HTTP_CONNECTION_RESET")
    assert GLOBAL_TAINT.is_tainted(), (
        "a connection reset mid-POST must taint the session — we cannot know "
        "whether the server-side effect (if any) actually landed"
    )
    assert "HTTP_CONNECTION_RESET" in GLOBAL_TAINT._tainted


def test_timeout_mid_request_also_taints(local_server):
    """A socket timeout is a distinct failure mode from a reset (the
    connection never even resolves) but must be handled the same way."""
    _MODE["value"] = "hang"
    with pytest.raises(ExecutionFailure):
        http_post_tool(local_server, {"x": 1}, timeout_s=0.5,
                        post_condition=lambda r: True,
                        taint_reason="HTTP_TIMEOUT")
    assert GLOBAL_TAINT.is_tainted()
    assert "HTTP_TIMEOUT" in GLOBAL_TAINT._tainted


def test_taint_from_http_blocks_subsequent_mutating_calls(local_server, tmp_path):
    """The real proof this isn't a parallel, disconnected safety system:
    an HTTP-caused taint must block a completely unrelated mutating tool
    (file_write_tool), proving the guard is genuinely shared state, not
    something http_post_tool would need its own copy of."""
    from statecheck.tool_executor import file_write_tool

    _MODE["value"] = "reset_mid_request"
    with pytest.raises(ExecutionFailure):
        http_post_tool(local_server, {"x": 1}, timeout_s=2.0, taint_reason="HTTP_CONNECTION_RESET")

    target = tmp_path / "should_be_blocked.txt"
    with pytest.raises(ContractViolation) as exc:
        file_write_tool(str(target), "should never land")
    assert exc.value.predicate_name == "taint_guard"
    assert not target.exists()
