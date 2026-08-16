# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
source .venv/bin/activate                        # or prefix commands with .venv/bin/
pip install -r requirements.txt

python test_graph.py                             # whole suite, no API credits spent
python research_graph.py "your question"         # CLI run
python server.py                                 # panel at http://127.0.0.1:8000
uvicorn server:app --host 127.0.0.1 --port 8000 --reload   # same server, with reload
```

`test_graph.py` is plain asserts under `if __name__ == "__main__"` — no pytest, no
fixtures. To run one test, call it from a REPL or comment out the others in the
`__main__` block. Adding a test means adding a `test_*` function and a call there.

The server binds port 8000 unconditionally. A stale process is the most common
cause of "my fix didn't take": `kill $(lsof -nP -iTCP:8000 -sTCP:LISTEN -t)`.

## Architecture

A deep research agent built as a graph rather than a single agent loop. Four
nodes, wired by ordinary control flow — there is no orchestration framework, and
adding one would defeat the point of the repo.

```
plan ──fan-out──> research x N ──fan-in──> critique ──> synthesize
                      ^                        │
                      └────── loop back ───────┘
```

- **Nodes** are the four async functions in `research_graph.py` (`plan`,
  `research_one`, `critique`, `synthesize`).
- **Edges** are the `while` loop in `run()`. Fan-out is one `asyncio.gather`;
  fan-in is a dict merge; the loop-back is `queue = state.gaps`.
- **State** is the `State` dataclass. It is the only thing crossing edges.

Three design constraints hold the whole thing together:

**Workers return strings, not messages.** `research_one` joins its text blocks
into `notes` and returns that. Raw search results die with the worker's API call
and never enter the lead's context. Breaking this — returning the response
object, or passing transcripts between nodes — removes the reason the graph
exists.

**The client and the event sink live in `contextvars`, not parameters.** Nodes
call `client()` and `emit()` with no arguments. `asyncio.gather` copies the
context into every worker task, so the fan-out inherits both for free, and the
web server can run two graphs with two different API keys in one process without
either leaking. Bind them with `use_client()` / `use_emitter()`.

**`emit()` is the only output channel.** With no sink bound it prints to stderr
(the CLI trace); with one bound it pushes dicts into an `asyncio.Queue` that
`server.py` forwards as SSE, which `web/index.html` dispatches through a
`handlers` map keyed by event name. Adding a panel feature usually needs no
Python change — accumulate the events already being sent. Changing an event's
name or payload shape breaks the panel silently, since the handler map just
misses.

Routing logic worth protecting: `should_loop` and `merge_findings` are pure
functions, which is why the test suite covers the loop-back edge without an API
key. Keep new routing decisions pure and testable the same way.

Cost is bounded by `MAX_ROUNDS` (loop-back budget) and `MAX_SUBQUESTIONS`
(fan-out width). Both are hard slices, not suggestions — this pattern runs ~15x
the tokens of a chat turn.

## Backends

`LEAD_MODEL` and `WORKER_MODEL` read from the environment, defaulting to
`claude-sonnet-5` and `claude-haiku-4-5`. Per-node model choice is the design's
main cost lever, not an implementation detail.

The code is written to survive a gateway (`ANTHROPIC_BASE_URL`) as well as
`api.anthropic.com`, which drove three non-obvious choices:

- `make_client()` sends an `x-session-id` header on every request. Some gateways
  require it; Anthropic ignores it.
- `_create()` reads every response as a stream. Gateways that return
  `text/event-stream` for non-streaming requests make the plain `.create()` path
  return a raw string.
- `_json_call()` states the JSON schema twice — as `output_config.format` and in
  the system prompt — and `extract_json()` tolerates prose around the object.
  Gateways commonly accept the schema and ignore it.

Known limitation with proxies: the `web_search` tool may be accepted but never
execute (`searches: 0` on every worker), so findings come back unsourced while
the run still reports success. Check `searches` before trusting a brief.

The `/key` endpoint validates with a 1-token `messages.create`, not
`models.list` — gateways typically proxy `/v1/messages` and little else.

## Repo intent

This is a teaching artifact for the orchestrator-workers pattern, not a
production research system. Its value is that the entire control flow reads in
one sitting. Weigh additions against that: the README lists the intended
extensions (verifier node, diverse-lens fan-out, dynamic loop bound,
deterministic gates), and each is meant to stay a small diff.
