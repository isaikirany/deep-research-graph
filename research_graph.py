"""A deep research agent, built as a graph instead of a loop.

Nodes are agents, edges are the routing between them, state is what flows along
the edges. Everything here runs on the Anthropic API — no orchestration
framework.

    plan ──fan-out──> research x N ──fan-in──> critique ──> synthesize
                          ^                        │
                          └────── loop back ───────┘
                              (while gaps remain)

Run:  python research_graph.py "your question"
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import sys
import textwrap
import uuid
from dataclasses import dataclass, field

from anthropic import AsyncAnthropic

# One of the reasons to build a graph: you pick the model per node. Planning and
# synthesis are judgment work; reading search results is bulk work. Paying Opus
# rates for the bulk work is the mistake a single-agent loop can't avoid.
# Override per environment — some proxies do not permit every model.
LEAD_MODEL = os.environ.get("LEAD_MODEL", "claude-sonnet-5")
WORKER_MODEL = os.environ.get("WORKER_MODEL", "claude-haiku-4-5")

MAX_ROUNDS = 2  # loop-back budget: how many times critique may send work back
MAX_SUBQUESTIONS = 5  # fan-out width

# The client and the event sink both live in context variables so one process can
# run several graphs at once — each with its own key — without any node needing
# to know it. asyncio.gather copies the context into every worker task, so the
# fan-out inherits both for free.
_client_var: contextvars.ContextVar[AsyncAnthropic] = contextvars.ContextVar("client")
_emit_var: contextvars.ContextVar = contextvars.ContextVar("emit", default=None)


def make_client(api_key: str | None = None) -> AsyncAnthropic:
    """A client that also works behind a gateway (ANTHROPIC_BASE_URL).

    Some gateways require a session identifier on every request. The header is
    ignored by api.anthropic.com, so it costs nothing to always send it.
    """
    return AsyncAnthropic(
        api_key=api_key, default_headers={"x-session-id": uuid.uuid4().hex}
    )


def client() -> AsyncAnthropic:
    """The Anthropic client for this run. Defaults to ANTHROPIC_API_KEY."""
    try:
        return _client_var.get()
    except LookupError:
        created = make_client()
        _client_var.set(created)
        return created


def use_client(instance: AsyncAnthropic) -> None:
    """Bind a client to the current context — how the web server passes a key."""
    _client_var.set(instance)


def use_emitter(sink) -> None:
    """Bind an event sink to the current context. Sink takes one dict."""
    _emit_var.set(sink)


def emit(event: str, **data) -> None:
    """Every state change the graph makes, announced on one channel."""
    sink = _emit_var.get()
    if sink is None:
        print(f"  [graph] {event} {data or ''}".rstrip(), file=sys.stderr)
    else:
        sink({"event": event, **data})


def _report_usage(node: str, model: str, response) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    emit(
        "usage",
        node=node,
        model=model,
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
    )


# ---------------------------------------------------------------- shared state


@dataclass
class State:
    """What flows along the edges. Each node reads it and appends to it."""

    question: str
    subquestions: list[str] = field(default_factory=list)
    findings: dict[str, str] = field(default_factory=dict)  # subquestion -> notes
    gaps: list[str] = field(default_factory=list)
    round: int = 0
    report: str = ""


# --------------------------------------------------------------- graph helpers
# Pure functions — the parts worth unit-testing without spending API credits.


def merge_findings(state: State, new: dict[str, str]) -> None:
    """Fan-in. Later rounds overwrite earlier notes for the same subquestion."""
    state.findings.update({k: v for k, v in new.items() if v.strip()})


def should_loop(state: State) -> bool:
    """The conditional edge from critique back to research."""
    return bool(state.gaps) and state.round < MAX_ROUNDS


async def _create(**kwargs):
    """One non-streaming message.

    Read as a stream regardless: some proxies return SSE for every request, and
    the plain `.create()` path cannot parse that body.
    """
    async with client().messages.stream(**kwargs) as stream:
        return await stream.get_final_message()


def extract_json(text: str) -> dict:
    """The JSON object in a reply, whether or not prose came with it.

    The API's json_schema format makes this unnecessary. Proxies that drop the
    format do not, and then the model answers in prose with the JSON inside.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"no JSON object in reply: {text[:200]}")
        return json.loads(text[start : end + 1])


async def _json_call(node: str, system: str, prompt: str, schema: dict) -> dict:
    """Lead-model call constrained to a JSON schema. No tools, no free text."""
    response = await _create(
        model=LEAD_MODEL,
        max_tokens=4000,
        # The schema is stated twice on purpose: as the API's output format, and
        # in the system prompt for backends that ignore it.
        system=f"{system}\n\nReply with JSON only, matching:\n{json.dumps(schema)}",
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": schema},
        },
        messages=[{"role": "user", "content": prompt}],
    )
    _report_usage(node, LEAD_MODEL, response)
    text = "\n".join(b.text for b in response.content if b.type == "text")
    return extract_json(text)


# ---------------------------------------------------------------------- nodes


async def plan(state: State) -> None:
    """Node: split one question into independent, searchable subquestions."""
    emit("node.start", node="plan", model=LEAD_MODEL)
    result = await _json_call(
        node="plan",
        system=(
            "You break a research question into independent subquestions. "
            "Each one must be answerable on its own, by someone who cannot see "
            "the others. No overlap, no meta-questions about the research."
        ),
        prompt=(
            f"Research question: {state.question}\n\n"
            f"Produce at most {MAX_SUBQUESTIONS} subquestions."
        ),
        schema={
            "type": "object",
            "properties": {
                "subquestions": {
                    "type": "array",
                    "items": {"type": "string"},
                }
            },
            "required": ["subquestions"],
            "additionalProperties": False,
        },
    )
    state.subquestions = result["subquestions"][:MAX_SUBQUESTIONS]
    emit("node.done", node="plan", subquestions=state.subquestions)


async def research_one(subquestion: str, node: str = "research") -> tuple[str, str]:
    """Node: one worker, one subquestion, its own context window.

    Worker context never touches the lead's — that isolation is the whole point
    of a node. Only the summary crosses the edge back.
    """
    emit("node.start", node=node, model=WORKER_MODEL, subquestion=subquestion)
    response = await _create(
        model=WORKER_MODEL,
        max_tokens=4000,
        system=(
            "You are a researcher. Search the web, read what you find, and "
            "report concise findings. Every claim gets a source URL. If the "
            "sources disagree, say so. Answer only the question you were given."
        ),
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
        messages=[{"role": "user", "content": subquestion}],
    )
    _report_usage(node, WORKER_MODEL, response)
    notes = "\n".join(b.text for b in response.content if b.type == "text")
    searches = sum(1 for b in response.content if b.type == "server_tool_use")
    emit("node.done", node=node, notes=notes, searches=searches)
    return subquestion, notes


async def critique(state: State) -> None:
    """Node: a reviewer that never wrote the draft. Its verdict is an edge."""
    emit("node.start", node="critique", model=LEAD_MODEL, reviewing=len(state.findings))
    result = await _json_call(
        node="critique",
        system=(
            "You review research for gaps. A gap is a claim that is unsourced, "
            "a subquestion that came back empty, or a contradiction nobody "
            "resolved. Phrase each gap as a new searchable question. If the "
            "research answers the original question, return no gaps."
        ),
        prompt=(
            f"Original question: {state.question}\n\n"
            + "\n\n".join(f"## {q}\n{notes}" for q, notes in state.findings.items())
        ),
        schema={
            "type": "object",
            "properties": {
                "gaps": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["gaps"],
            "additionalProperties": False,
        },
    )
    state.gaps = result["gaps"][:MAX_SUBQUESTIONS]
    emit("node.done", node="critique", gaps=state.gaps)


async def synthesize(state: State) -> None:
    """Node: everything fans in here. One writer, one voice, all the findings."""
    emit("node.start", node="synthesize", model=LEAD_MODEL, sources=len(state.findings))
    async with client().messages.stream(
        model=LEAD_MODEL,
        max_tokens=16000,
        system=(
            "You write the final research brief. Lead with the answer, then the "
            "evidence. Keep every source URL. Say plainly where the evidence is "
            "thin — do not paper over it."
        ),
        output_config={"effort": "high"},
        messages=[
            {
                "role": "user",
                "content": (
                    f"Question: {state.question}\n\n"
                    + "\n\n".join(
                        f"## {q}\n{notes}" for q, notes in state.findings.items()
                    )
                ),
            }
        ],
    ) as stream:
        async for chunk in stream.text_stream:
            emit("report.delta", text=chunk)
        message = await stream.get_final_message()
    _report_usage("synthesize", LEAD_MODEL, message)
    state.report = "\n".join(b.text for b in message.content if b.type == "text")
    emit("node.done", node="synthesize")


# ----------------------------------------------------------------- the runtime
# The edges. Deliberately plain control flow — you can read the whole graph here.


async def run(question: str) -> State:
    state = State(question=question)
    emit("graph.start", question=question, max_rounds=MAX_ROUNDS)

    await plan(state)

    queue = list(state.subquestions)
    while True:
        state.round += 1

        # fan-out: N workers, concurrently, one per subquestion
        workers = [f"r{state.round}w{i}" for i in range(len(queue))]
        emit("fanout", round=state.round, workers=workers, subquestions=queue)
        results = await asyncio.gather(
            *(research_one(q, node) for q, node in zip(queue, workers))
        )

        # fan-in: merge every worker's notes back into shared state
        merge_findings(state, dict(results))
        emit("fanin", round=state.round, findings=len(state.findings))

        await critique(state)
        if not should_loop(state):
            emit(
                "route",
                to="synthesize",
                reason="no gaps left" if not state.gaps else "loop budget spent",
            )
            break

        # conditional edge: the gaps become the next round's subquestions
        emit("route", to="research", reason=f"{len(state.gaps)} gaps", gaps=state.gaps)
        queue = state.gaps

    await synthesize(state)
    emit("graph.done")
    return state


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY first (see .env.example).")
    if len(sys.argv) < 2:
        sys.exit(f'Usage: python {sys.argv[0]} "your research question"')

    state = asyncio.run(run(" ".join(sys.argv[1:])))
    print("\n" + textwrap.dedent(state.report).strip())


if __name__ == "__main__":
    main()
