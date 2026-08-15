"""Check the graph's routing logic without spending API credits.

    python test_graph.py
"""

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


if __name__ == "__main__":
    test_fan_in_merges_and_ignores_empty()
    test_loop_edge()
    print("ok")
