"""Check the graph's routing logic without spending API credits.

    python test_graph.py
"""

import asyncio
import json
from types import SimpleNamespace

import research_graph as g


def test_fan_in_merges_and_ignores_empty():
    state = g.State(question="q", findings={"a": "old notes"})
    g.merge_findings(state, {"a": "new notes", "b": "  ", "c": "notes"})
    assert state.findings == {"a": "new notes", "c": "notes"}, state.findings


def test_loop_edge():
    # gaps + budget left -> loop back
    assert g.should_loop(g.State(question="q", gaps=["g"], round=1))
    # no gaps -> done, even with budget left
    assert not g.should_loop(g.State(question="q", gaps=[], round=1))
    # budget spent -> done, even with gaps (this is what stops runaway cost)
    assert not g.should_loop(g.State(question="q", gaps=["g"], round=g.MAX_ROUNDS))


def test_extract_json():
    assert g.extract_json('{"gaps": []}') == {"gaps": []}
    # a backend that ignored the schema: prose around the object
    assert g.extract_json('Here you go:\n```json\n{"gaps": ["a"]}\n```\nDone.') == {
        "gaps": ["a"]
    }
    try:
        g.extract_json("no object here")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on a reply with no JSON")


class FakeMessages:
    """Enough of the Anthropic client to drive the graph through both rounds."""

    def __init__(self):
        self.critiques = 0

    def _text(self, model, kwargs):
        if model == g.WORKER_MODEL:
            return "notes for: " + kwargs["messages"][0]["content"]
        config = json.dumps(kwargs.get("output_config", {}))
        if "schema" not in config:  # synthesize: free text, no schema
            return "THE BRIEF"
        if "subquestions" in config:
            return json.dumps({"subquestions": ["a", "b"]})
        self.critiques += 1
        return json.dumps({"gaps": ["c"] if self.critiques == 1 else []})

    def stream(self, *, model, **kwargs):
        # Every call goes through the streaming path — that is what the graph
        # does now, because some proxies return SSE for every request.
        text = self._text(model, kwargs)
        message = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )

        class Stream:
            text_stream = _aiter([text])

            async def __aenter__(self_):
                return self_

            async def __aexit__(self_, *exc):
                return False

            async def get_final_message(self_):
                return message

        return Stream()


async def _aiter(items):
    for item in items:
        yield item


def test_event_stream_reports_every_edge():
    """The web panel is drawn entirely from these events, so pin their shape."""
    events = []

    async def drive():
        g.use_client(SimpleNamespace(messages=FakeMessages()))
        g.use_emitter(events.append)
        return await g.run("q")

    state = asyncio.run(drive())
    names = [e["event"] for e in events]

    assert names[0] == "graph.start", names[:1]
    assert names[-1] == "graph.done", names[-1:]

    # One fan-out per round: two workers first, then the single gap.
    fanouts = [e for e in events if e["event"] == "fanout"]
    assert [e["workers"] for e in fanouts] == [["r1w0", "r1w1"], ["r2w0"]], fanouts

    # The critic routes back once, then forward once.
    assert [e["to"] for e in events if e["event"] == "route"] == [
        "research",
        "synthesize",
    ]

    # The brief reaches the page as deltas, not one lump at the end.
    assert "".join(e["text"] for e in events if e["event"] == "report.delta") == "THE BRIEF"
    assert state.report == "THE BRIEF", state.report

    # Every model call is costed, and tagged with the node that made it.
    usage = {(e["node"], e["model"]) for e in events if e["event"] == "usage"}
    assert ("r1w0", g.WORKER_MODEL) in usage, usage
    assert ("plan", g.LEAD_MODEL) in usage, usage


if __name__ == "__main__":
    test_fan_in_merges_and_ignores_empty()
    test_loop_edge()
    test_extract_json()
    test_event_stream_reports_every_edge()
    print("ok")
