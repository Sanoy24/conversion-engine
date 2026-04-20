"""
Langfuse tracing client.
Per-trace cost attribution and prompt versioning.
Every trace_id referenced in the evidence graph resolves here.
"""

from __future__ import annotations

import logging

from langfuse import Langfuse

from agent.config import settings

logger = logging.getLogger(__name__)

_langfuse: Langfuse | None = None


def get_langfuse() -> Langfuse:
    """Get or create the Langfuse client."""
    global _langfuse
    if _langfuse is None:
        _langfuse = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    return _langfuse


def log_trace(
    trace_id: str,
    name: str,
    input_data: dict | None = None,
    output_data: dict | None = None,
    metadata: dict | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
):
    """Log a trace to Langfuse."""
    try:
        lf = get_langfuse()
        return lf.trace(
            id=trace_id,
            name=name,
            input=input_data,
            output=output_data,
            metadata=metadata or {},
            user_id=user_id,
            session_id=session_id,
        )
    except Exception as e:
        logger.warning("Langfuse trace logging failed for %s: %s", trace_id, str(e))
        return None


def log_generation(
    trace_id: str,
    name: str,
    model: str,
    input_messages: list[dict],
    output: str,
    usage: dict | None = None,
    cost: float | None = None,
    metadata: dict | None = None,
):
    """Log an LLM generation event to Langfuse."""
    try:
        lf = get_langfuse()
        trace = lf.trace(id=trace_id, name=name)
        trace.generation(
            name=f"{name}_generation",
            model=model,
            input=input_messages,
            output=output,
            usage=usage,
            metadata=metadata or {},
        )
        return trace
    except Exception as e:
        logger.warning("Langfuse generation logging failed: %s", str(e))
        return None


def flush():
    """Flush all pending Langfuse events."""
    try:
        lf = get_langfuse()
        lf.flush()
    except Exception as e:
        logger.warning("Langfuse flush failed: %s", str(e))
