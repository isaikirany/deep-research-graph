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
import json
import os
import sys
import textwrap
from dataclasses import dataclass, field

from anthropic import AsyncAnthropic

# One of the reasons to build a graph: you pick the model per node. Planning and
# synthesis are judgment work; reading search results is bulk work. Paying Opus
# rates for the bulk work is the mistake a single-agent loop can't avoid.
LEAD_MODEL = "claude-opus-5"
WORKER_MODEL = "claude-haiku-4-5"

MAX_ROUNDS = 2  # loop-back budget: how many times critique may send work back
MAX_SUBQUESTIONS = 5  # fan-out width

client = AsyncAnthropic()  # reads ANTHROPIC_API_KEY


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


async def _json_call(system: str, prompt: str, schema: dict) -> dict:
    """Lead-model call constrained to a JSON schema. No tools, no free text."""
    response = await client.messages.create(
        model=LEAD_MODEL,
        max_tokens=4000,
        system=system,
        output_config={
            "effort": "medium",
            "format": {"type": "json_schema", "schema": schema},
        },
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


# ---------------------------------------------------------------------- nodes


async def plan(state: State) -> None:
    """Node: split one question into independent, searchable subquestions."""
    result = await _json_call(
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


async def research_one(subquestion: str) -> tuple[str, str]:
    """Node: one worker, one subquestion, its own context window.

    Worker context never touches the lead's — that isolation is the whole point
    of a node. Only the summary crosses the edge back.
    """
    response = await client.messages.create(
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
    notes = "\n".join(b.text for b in response.content if b.type == "text")
    return subquestion, notes


async def critique(state: State) -> None:
    """Node: a reviewer that never wrote the draft. Its verdict is an edge."""
    result = await _json_call(
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


async def synthesize(state: State) -> None:
    """Node: everything fans in here. One writer, one voice, all the findings."""
    async with client.messages.stream(
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
        message = await stream.get_final_message()
    state.report = "\n".join(b.text for b in message.content if b.type == "text")


# ----------------------------------------------------------------- the runtime
# The edges. Deliberately plain control flow — you can read the whole graph here.


async def run(question: str) -> State:
    state = State(question=question)

    await plan(state)
    log(f"planned {len(state.subquestions)} subquestions")

    queue = list(state.subquestions)
    while True:
        state.round += 1

        # fan-out: N workers, concurrently, one per subquestion
        log(f"round {state.round}: researching {len(queue)} subquestions")
        results = await asyncio.gather(*(research_one(q) for q in queue))

        # fan-in: merge every worker's notes back into shared state
        merge_findings(state, dict(results))

        await critique(state)
        if not should_loop(state):
            log("no gaps left" if not state.gaps else "loop budget spent")
            break

        # conditional edge: the gaps become the next round's subquestions
        log(f"critique found {len(state.gaps)} gaps, looping back")
        queue = state.gaps

    await synthesize(state)
    return state


def log(message: str) -> None:
    print(f"  [graph] {message}", file=sys.stderr)


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Set ANTHROPIC_API_KEY first (see .env.example).")
    if len(sys.argv) < 2:
        sys.exit(f'Usage: python {sys.argv[0]} "your research question"')

    state = asyncio.run(run(" ".join(sys.argv[1:])))
    print("\n" + textwrap.dedent(state.report).strip())


if __name__ == "__main__":
    main()
