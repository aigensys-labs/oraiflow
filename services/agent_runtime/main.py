"""ASGI application entry point for the ORAIflow Agent Runtime service."""

from __future__ import annotations

import hmac
import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from services.agent_runtime.agent import run_demo_chat, run_llm_chat
from services.agent_runtime.agui import register_agui_routes, start_agui_runtime, stop_agui_runtime
from services.agent_runtime.interpretation import confidence_gate_configuration
from shared.version import __version__


def _configure_application_logging() -> None:
    """Emit ORAI Agent Runtime application logs to stdout at the configured level."""
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    namespace = logging.getLogger("oraiflow.agent_runtime")
    namespace.setLevel(level)
    namespace.propagate = False
    if not any(
        getattr(handler, "_oraiflow_runtime_handler", False) for handler in namespace.handlers
    ):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        handler._oraiflow_runtime_handler = True
        namespace.addHandler(handler)


_configure_application_logging()
logger = logging.getLogger("oraiflow.agent_runtime")
INTERNAL_TOKEN = os.getenv("AGENT_RUNTIME_INTERNAL_TOKEN", "local-agent-runtime-token-change-me")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize and release resources owned by the Agent Runtime application."""
    await start_agui_runtime(app)
    try:
        yield
    finally:
        await stop_agui_runtime(app)


app = FastAPI(
    title="ORAIflow Agent Runtime",
    description="Internal conversation and LangGraph runtime. No financial write capabilities.",
    version=__version__,
    lifespan=lifespan,
)


class ChatRequest(BaseModel):
    """Request contract for the retained direct-chat diagnostic endpoint."""

    message: str = Field(min_length=1, max_length=4000)
    history: list[dict[str, str]] = Field(default_factory=list, max_length=10)


@app.get("/healthz")
async def healthz():
    """Report the Agent Runtime process health."""
    return {
        "status": "ok",
        "service": "agent-runtime",
        "version": __version__,
        "agui": bool(getattr(app.state, "agui_agent", None)),
    }


def _require_internal(request: Request) -> None:
    supplied = request.headers.get("authorization", "")
    expected = f"Bearer {INTERNAL_TOKEN}"
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Unauthorized internal Agent Runtime caller")


@app.get("/internal/config")
async def internal_config(request: Request):
    """Expose the non-secret runtime configuration required by internal diagnostics."""
    _require_internal(request)
    provider = os.getenv("LLM_PROVIDER", "demo").lower()
    approved = os.getenv("LLM_EXTERNAL_DATA_APPROVED", "false").lower() == "true"
    configured = (
        provider == "demo"
        or (provider == "openai" and bool(os.getenv("OPENAI_API_KEY")) and approved)
        or (provider == "anthropic" and bool(os.getenv("ANTHROPIC_API_KEY")) and approved)
        or (provider == "ollama" and bool(os.getenv("OLLAMA_BASE_URL")))
    )
    gate = confidence_gate_configuration()
    return {
        "llm_provider": provider,
        "llm_model": os.getenv("LLM_MODEL", "demo"),
        "llm_configured": configured,
        "llm_external_data_approved": approved,
        "interpretation_engine": "streamed_tool_call_assessment",
        "interpretation_gate_active": True,
        "interpretation_confidence_gate_mode": gate["mode"],
        "interpretation_confidence_gate_ready": gate["valid"],
        "interpretation_confidence_thresholds_configured": gate["ready"],
        "missing_confidence_thresholds": gate.get("missing", []),
        "invalid_confidence_thresholds": gate.get("invalid", []),
        "model_data_policy": "minimized-redacted-demo",
    }


@app.post("/api/chat", include_in_schema=True)
async def chat(payload: ChatRequest, request: Request):
    """Direct diagnostic path; the customer UI uses CopilotKit/AG-UI instead."""
    _require_internal(request)

    provider = os.getenv("LLM_PROVIDER", "demo").lower()
    try:
        if provider == "demo":
            reply, tools_used = await run_demo_chat(payload.message)
        else:
            reply, tools_used = await run_llm_chat(payload.message, payload.history)
        # The retained direct endpoint is diagnostic only. It has no browser
        # context for the Contacts/source-account contract and must not synthesize
        # a transfer draft or choose a source account. Transfer preparation is
        # supported through the primary CopilotKit/AG-UI flow.
        return {"reply": reply, "tools_used": tools_used, "provider": provider}
    except Exception as exc:
        logger.exception("Chat request failed for provider=%s: %s", provider, type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail="The assistant could not reach the model or banking capability service. Check Agent Runtime logs and provider configuration.",
        ) from exc


register_agui_routes(app)
