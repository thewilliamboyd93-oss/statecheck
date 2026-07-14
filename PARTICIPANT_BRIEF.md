# Umpire — External Wrap Test

## What this is

`umpire` is a small primitive: a deterministic gate that decides
whether a tool call is allowed to run (`pre_condition`), and whether its
result can be trusted afterward (`post_condition`) — with real rollback
where possible, and taint-tracking where it isn't. Full spec: `PROTOCOL.md`.
Real code, real tests: `umpire/tool_executor.py`.

Every test proving this so far was written by the person who designed it.
That's the gap this exercise exists to close.

## The task

Pick **one** tool from the candidates below (or bring your own — see
criteria) that you haven't seen used with this project before. Wrap it
using only the public API in `tool_executor.py` — `ToolContract` +
`execute_contracted` — with **zero changes to `tool_executor.py` itself**.

**Do this before reading anything else in `tests/`.** Specifically, don't
look at `test_primitive_generalization_git.py` or `_http.py` first — those
are worked examples, and seeing them defeats the point of this exercise.
Read `PROTOCOL.md` and `tool_executor.py`'s own docstrings; that's the
intended amount of context.

## Constraints

- Real I/O. No mocking the tool itself — if it's an API, actually call it
  (a sandbox/test-mode key is fine and preferred; never real money or
  real production data).
- Test at least one **materially different failure mode**, not just the
  happy path. "It succeeds" tells us nothing new. "It fails in a way none
  of the existing tests cover" is the actual signal.
- If you need to change `tool_executor.py` to make it work, **that's a
  valid and valuable outcome** — write down exactly what and why. Do not
  force a fit by working around a real gap; the gap is more useful to us
  than a clean result.

## What to report back

Open a GitHub Issue on the repo, tagged `external-validation`, with:

1. Which tool, and why you picked it.
2. What failure mode(s) you tested and how (real repro steps, like the
   existing HTTP/git tests — connection resets, timeouts, partial
   failures, whatever's native to your tool).
3. What worked without modification.
4. Anything that needed a workaround, a new escape hatch, or genuinely
   didn't fit — including if you concluded it doesn't fit at all.
5. Rough time spent. (We want to know if this is a 20-minute exercise or
   a 3-hour one — that's useful data too.)

No prep needed beyond reading `PROTOCOL.md`. If something is unclear
enough that you have to guess, note where — that's a documentation gap
worth knowing about independent of the test itself.
