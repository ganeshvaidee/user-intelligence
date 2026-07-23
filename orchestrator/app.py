# orchestrator/app.py
#
# FastAPI wrapper around the flow functions in flows/run_flow.py.
# This is the service boundary — it adds no flow logic, just exposes
# the existing functions over HTTP so a client can call them remotely.
#
# Requires MCP_URL to point at a running MCP server:
#   MCP_URL=http://localhost:8001 python orchestrator/app.py
#
# Run: python orchestrator/app.py [--port 8000]

import sys
import json
import logging
import traceback
import argparse
from pathlib import Path

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# flows/ must be on the path so we can import from it
sys.path.insert(0, str(Path(__file__).parent.parent / "flows"))

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import uvicorn

from run_flow import (
    run_flow, run_flow_until_complete, run_flow_with_reflection,
    run_flow_stream, run_flow_until_complete_stream, run_flow_with_reflection_stream,
    run_flow_parallel_risk, run_flow_parallel_risk_with_memory,
    run_flow_offboard_prepare, run_flow_offboard_confirm,
)


# ── App ───────────────────────────────────────────────────────────

app = FastAPI(title="User Intelligence Orchestrator")


# ── Request / response models ─────────────────────────────────────

class FlowRequest(BaseModel):
    user_request: str
    skill_names:  list[str]
    flow_type:    str = "single"   # single | convergence | reflection
    max_rounds:   int = 3          # only used when flow_type == convergence


class FlowResponse(BaseModel):
    response:  str
    flow_type: str


class OffboardRequest(BaseModel):
    user_id: str
    reason:  str


# ── Routes ────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/flow", response_model=FlowResponse)
async def run_flow_endpoint(req: FlowRequest):
    """
    Run a skill flow and return the result.

    flow_type options:
      single      — single-shot, Claude calls tools until done
      convergence — loop until a completeness check passes
      reflection  — run once, critique, revise if issues found
    """
    try:
        if req.flow_type == "single":
            result = await run_flow(req.user_request, req.skill_names, verbose=False)

        elif req.flow_type == "convergence":
            result = await run_flow_until_complete(
                req.user_request, req.skill_names,
                max_rounds=req.max_rounds, verbose=False,
            )

        elif req.flow_type == "reflection":
            result = await run_flow_with_reflection(
                req.user_request, req.skill_names, verbose=False,
            )

        elif req.flow_type == "risk-parallel":
            result, _ = await run_flow_parallel_risk(
                req.user_request, verbose=False, thinking=False,
            )

        elif req.flow_type == "risk-parallel-thinking":
            result, _ = await run_flow_parallel_risk(
                req.user_request, verbose=False, thinking=True,
            )

        elif req.flow_type == "risk-parallel-memory":
            result = await run_flow_parallel_risk_with_memory(
                req.user_request, verbose=False,
            )

        else:
            raise HTTPException(status_code=400, detail=f"Unknown flow_type: {req.flow_type}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Flow error:\n%s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

    return FlowResponse(response=result, flow_type=req.flow_type)


@app.post("/flow/stream")
async def run_flow_stream_endpoint(req: FlowRequest):
    """
    Stream any flow — yields SSE events as Claude generates text.
    Each event:  data: {"text": "..."}\n\n
    Final event: data: {"done": true}\n\n
    Error event: data: {"error": "..."}\n\n

    Judge/critic calls run silently between rounds — only Claude's text is streamed.
    """
    async def generate():
        try:
            if req.flow_type == "single":
                gen = run_flow_stream(req.user_request, req.skill_names)
            elif req.flow_type == "convergence":
                gen = run_flow_until_complete_stream(
                    req.user_request, req.skill_names, max_rounds=req.max_rounds
                )
            elif req.flow_type == "reflection":
                gen = run_flow_with_reflection_stream(req.user_request, req.skill_names)
            elif req.flow_type in ("risk-parallel", "risk-parallel-thinking"):
                # Parallel risk runs synchronously then returns; wrap in an async generator
                _thinking = req.flow_type == "risk-parallel-thinking"
                async def _parallel_gen():
                    result, _ = await run_flow_parallel_risk(req.user_request, verbose=False, thinking=_thinking)
                    yield result
                gen = _parallel_gen()
            else:
                yield f"data: {json.dumps({'error': f'Unknown flow_type: {req.flow_type}'})}\n\n"
                return

            async for chunk in gen:
                yield f"data: {json.dumps({'text': chunk})}\n\n"
            yield f"data: {json.dumps({'done': True})}\n\n"
        except Exception as e:
            logger.error("Stream error:\n%s", traceback.format_exc())
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── Human-in-the-Loop offboarding endpoints ───────────────────────

@app.post("/offboard/prepare")
async def offboard_prepare_endpoint(req: OffboardRequest):
    """
    Phase 1: lookup → risk → flag. Returns report for human review.
    Does NOT deactivate. The client owns the confirmation gate.
    """
    try:
        result = await run_flow_offboard_prepare(req.user_id, req.reason, verbose=False)
    except Exception as e:
        logger.error("Offboard prepare error:\n%s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
    return {"response": result, "user_id": req.user_id, "phase": "prepare"}


@app.post("/offboard/prepare/stream")
async def offboard_prepare_stream_endpoint(req: OffboardRequest):
    """Streaming variant of /offboard/prepare."""
    async def generate():
        try:
            result = await run_flow_offboard_prepare(req.user_id, req.reason, verbose=False)
            yield f"data: {json.dumps({'text': result})}\n\n"
            yield f"data: {json.dumps({'done': True, 'user_id': req.user_id})}\n\n"
        except Exception as e:
            logger.error("Offboard prepare stream error:\n%s", traceback.format_exc())
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/offboard/confirm")
async def offboard_confirm_endpoint(req: OffboardRequest):
    """
    Phase 2: deactivate. Called only after the human has confirmed.
    The account is already flagged from Phase 1.
    """
    try:
        result = await run_flow_offboard_confirm(req.user_id, req.reason, verbose=False)
    except Exception as e:
        logger.error("Offboard confirm error:\n%s", traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))
    return {"response": result, "user_id": req.user_id, "phase": "confirm"}


@app.post("/offboard/confirm/stream")
async def offboard_confirm_stream_endpoint(req: OffboardRequest):
    """Streaming variant of /offboard/confirm."""
    async def generate():
        try:
            result = await run_flow_offboard_confirm(req.user_id, req.reason, verbose=False)
            yield f"data: {json.dumps({'text': result})}\n\n"
            yield f"data: {json.dumps({'done': True, 'user_id': req.user_id})}\n\n"
        except Exception as e:
            logger.error("Offboard confirm stream error:\n%s", traceback.format_exc())
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    return StreamingResponse(generate(), media_type="text/event-stream")


# ── Entry point ───────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="User Intelligence Orchestrator")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    uvicorn.run(app, host=args.host, port=args.port, timeout_keep_alive=600)
