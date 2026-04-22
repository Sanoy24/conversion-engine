"""
Langfuse tracing client — compatible with Langfuse SDK v4.
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
    global _langfuse
    if _langfuse is None:
        _langfuse = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_endpoint,
        )
    return _langfuse


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
    """Log an LLM generation to Langfuse (SDK v4)."""
    try:
        lf = get_langfuse()
        with lf.start_as_current_observation(
            name=name,
            input=input_messages,
            output=output,
            model=model,
            metadata={**(metadata or {}), "trace_id": trace_id, "cost_usd": cost},
        ) as obs:
            if usage:
                obs.update(
                    usage_details={
                        "input": usage.get("prompt_tokens", 0),
                        "output": usage.get("completion_tokens", 0),
                    }
                )
        lf.flush()
    except Exception as e:
        logger.warning("Langfuse generation logging failed: %s", str(e))


def log_trace(
    trace_id: str,
    name: str,
    input_data: dict | None = None,
    output_data: dict | None = None,
    metadata: dict | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
):
    """Log a pipeline trace event to Langfuse (SDK v4)."""
    try:
        lf = get_langfuse()
        with lf.start_as_current_observation(
            name=name,
            input=input_data,
            output=output_data,
            metadata={**(metadata or {}), "trace_id": trace_id},
        ):
            pass
        lf.flush()
    except Exception as e:
        logger.warning("Langfuse trace logging failed for %s: %s", trace_id, str(e))


def flush():
    """Flush all pending Langfuse events."""
    try:
        get_langfuse().flush()
    except Exception as e:
        logger.warning("Langfuse flush failed: %s", str(e))
