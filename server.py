"""Serve the graph to a browser, and stream every edge it takes as it happens.

The graph itself is untouched — this file only binds a client and an event sink
into the request's context, then forwards `emit()` events to the page as SSE.

    .venv/bin/python server.py     # then open http://127.0.0.1:8000

The API key arrives per request and lives only in memory for that request. It is
never written to disk, never logged, and never sent anywhere but api.anthropic.com.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import research_graph as graph

WEB = Path(__file__).parent / "web"

app = FastAPI(title="research graph")


class KeyRequest(BaseModel):
    key: str = ""


class RunRequest(BaseModel):
    key: str = ""
    question: str


def _resolve_key(supplied: str) -> str:
    """The key from the page, or the environment if the page left it blank."""
    return supplied.strip() or os.environ.get("ANTHROPIC_API_KEY", "")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


@app.post("/key")
async def check_key(request: KeyRequest) -> JSONResponse:
    """Cheapest possible auth check: one token from the worker model.

    Not `models.list` — gateways proxy /v1/messages and little else.
    """
    key = _resolve_key(request.key)
    if not key:
        return JSONResponse({"ok": False, "detail": "No key given."}, status_code=400)
    try:
        await graph.make_client(key).messages.create(
            model=graph.WORKER_MODEL,
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
    except Exception as error:
        return JSONResponse(
            {"ok": False, "detail": f"{type(error).__name__}: {error}"[:300]},
            status_code=400,
        )
    return JSONResponse({"ok": True, "source": "env" if not request.key.strip() else "page"})


@app.post("/run")
async def run(request: RunRequest) -> StreamingResponse:
    key = _resolve_key(request.key)
    question = request.question.strip()

    async def stream():
        if not key:
            yield _sse({"event": "error", "message": "Add a key to run the graph."})
            return
        if not question:
            yield _sse({"event": "error", "message": "Ask a question to run the graph."})
            return

        events: asyncio.Queue = asyncio.Queue()

        async def drive():
            # Bound to this task's context only — a second run with a different
            # key cannot see either of these.
            graph.use_client(graph.make_client(key))
            graph.use_emitter(events.put_nowait)
            try:
                await graph.run(question)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                events.put_nowait(
                    {"event": "error", "message": f"{type(error).__name__}: {error}"}
                )
            finally:
                events.put_nowait(None)

        task = asyncio.create_task(drive())
        try:
            while True:
                event = await events.get()
                if event is None:
                    break
                yield _sse(event)
        finally:
            task.cancel()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
