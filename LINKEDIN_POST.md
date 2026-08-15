# LinkedIn post

## Version A — the main one

Everyone's agent is a loop. The interesting ones stopped being loops.

"Graph engineering" started trending mid-2026, and like most new labels it sounds
like a discipline you have to go adopt. It isn't. Anthropic shipped the pattern
years ago under a plainer name: orchestrator-workers.

Strip it down and a graph is three things:

→ Nodes — an agent that does one job, in its own context window
→ Edges — the routing between them: fan-out, fan-in, and loops back
→ State — what flows along those edges

A single agent loop is the smallest possible graph: one node with an edge back to
itself. Everything past that is just deciding where to split.

Three things you get the moment you split:

1. Clean context per node. A researcher agent reads 40 pages and reports back a
summary. The raw pages never touch the lead agent's context. That's how a graph
reads more than one window could ever hold.

2. Speed, for free. Five researchers run at once. Wall-clock is your slowest
worker, not the sum of all five.

3. Cost control nobody talks about. You pick the model per node. Planning and
synthesis on a frontier model, bulk reading on a small one. A single-agent loop
has to run everything on one model — you're paying frontier rates to skim search
results.

And here's the number people skip. Anthropic's own multi-agent research system
beat a single-agent baseline by 90.2% on their internal eval — and burned roughly
15x the tokens of a normal chat turn. A normal agent uses about 4x.

That's the actual trade. You are buying quality and parallelism with tokens.

Which means the honest test isn't "can I build a graph." It's:

Does this task genuinely split into independent pieces?
Is each node already a loop that ships reliably on its own?
Is the answer worth 15x?

If one agent with a good verifier already does the job, wiring three of them
together costs more and buys nothing. A graph of weak nodes is just slop produced
in parallel.

I built the smallest honest example I could — a deep research agent as a graph,
running on the Anthropic API. Plan → fan out to parallel researchers → fan in →
critique → loop back on gaps → synthesise. About 200 lines, no orchestration
framework, so you can read the entire graph in one sitting.

Repo's below. Fork it, swap a node's model, watch cost and quality move.

[link]

#AIAgents #Anthropic #Claude #AIEngineering #LLM

---

## Version B — shorter, for a second post or a repost

Your agent is a loop. Should it be a graph?

Three questions, in order:

1. Does the task actually split into independent pieces? If step 2 needs step 1's
output, that's a chain, not a fan-out. Keep the loop.

2. Is every node already a loop that ships on its own? A graph of shaky agents
just multiplies the shakiness — in parallel, at higher cost.

3. Is the answer worth 15x the tokens? That's roughly what Anthropic measured for
their multi-agent research system. It also beat a single-agent baseline by 90.2%.
Both numbers are real. You're trading one for the other.

If you get three yeses, the build is smaller than you think. Nodes are agents,
edges are your routing code, state is what you pass between them. I put a working
deep research agent — plan, fan out, fan in, critique, loop, synthesise — in ~200
lines with no framework.

[link]

#AIAgents #Claude #AIEngineering
