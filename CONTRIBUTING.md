# Contributing to StateCheck

## Quick start

```bash
git clone https://github.com/thewilliamboyd93-oss/statecheck.git
cd statecheck
pip install -r requirements.txt
python -m pytest tests/ -v
```

You should see 52 tests pass, all exercising real behavior — real
subprocess sandboxing, real sockets, real git repos, real filesystem/sqlite
rollback, real thread serialization. Nothing here is mocked when the real
thing was cheap enough to just run. If anything fails on a clean clone,
that's a real bug in this repo, not something on your end to work around —
open an issue with the actual output.

Read `PROTOCOL.md` first for the spec. It's versioned and kept in lockstep
with the code, so it's more reliable than this file for anything about
*how the primitive behaves*. `COMPARISON.md` explains how this differs
from adjacent projects (tollgate, gatekit) if you're wondering why this
exists alongside them.

## Two different kinds of open issues — read the label

Issues here fall into two genuinely different categories. Check the label
before picking one:

- **`good first issue`** — deliberately easy, zero infrastructure. No
  cloud accounts, no local services to stand up. The point of these is
  external validation: proving the public API is usable by someone who's
  never seen this project before, not proving you can stand up Kubernetes.
- **`help wanted`** (e.g. #2, "External wrap test — pick a tool") —
  genuinely harder, and expects real infrastructure (a payment sandbox, a
  local cluster, a message broker). These test structurally different
  failure modes on purpose. Don't feel obligated to start here if you're
  new — start with a `good first issue` instead.

## The one rule that matters for the wrap-task issues

**If an issue asks you to wrap a tool with `ToolContract`, do that before
reading `test_primitive_generalization_git.py` or
`test_primitive_generalization_http.py`.** Those are worked examples of
the same kind of task, written by the person who built the primitive.
Seeing them first defeats the actual point of the exercise, which is
finding out whether the *documentation* (not someone else's example code)
is enough on its own. If you get stuck, say where — "I couldn't tell
whether X" is more useful to this project than a submission that quietly
worked around a confusing part.

## What a good PR looks like

- For a wrap-task issue: zero changes to `tool_executor.py` itself. If you
  found yourself needing to change it, that's interesting — say so in the
  PR description, don't just make the change and move on.
- Tests pass locally (`python -m pytest tests/ -v`) before you open the PR.
- If you're fixing a specific gap (like the `ContractViolation.note`
  issue), match the existing pattern in the file rather than introducing
  a new one — there's usually a working example a few lines away.
- Commit messages describe what changed and why, not just what. This repo
  keeps `PROTOCOL.md`'s changelog in the same spirit — see it for examples
  of "what changed and why" done well (and occasionally, done wrong and
  then corrected on the record rather than silently).

## Getting help

Comment on the issue you're working. A stuck contributor pointing at
exactly where the docs stopped making sense is useful signal, not a
failed attempt — this project explicitly wants that feedback.
