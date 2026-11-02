"""
LLM client abstraction for the Conversion Engine.
Routes through OpenRouter for both dev-tier and eval-tier models.
Tracks cost and latency per call for the evidence graph.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from openai import AsyncOpenAI

from agent.config import settings
from agent.models import TraceRecord
from agent.observability.langfuse_client import log_generation

logger = logging.getLogger(__name__)


class LLMClient:
    """Async LLM client via OpenRouter with cost tracking."""

    def __init__(self, model: str | None = None):
        self.model = model or settings.active_model
        self.client = AsyncOpenAI(
            api_key=settings.openrouter_api_key,
            base_url=settings.openrouter_base_url,
        )
        self._total_cost = 0.0
        self._call_count = 0

    async def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        response_format: dict | None = None,
        trace_event: str = "llm_call",
        prospect_company: str | None = None,
        thread_id: str | None = None,
    ) -> tuple[str, TraceRecord]:
        """
        Send a chat completion request and return (response_text, trace_record).

        The trace record captures cost, latency, and model info for evidence-graph integrity.
        """
        trace_id = f"tr_{uuid.uuid4().hex[:8]}"
        start_time = time.monotonic()

        try:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
            if response_format:
                kwargs["response_format"] = response_format

            response = await self.client.chat.completions.create(**kwargs)

            elapsed_ms = (time.monotonic() - start_time) * 1000
            content = response.choices[0].message.content or ""

            # Extract cost from OpenRouter response headers if available
            usage = response.usage
            cost = 0.0
            if usage:
                # Estimate cost based on token counts (varies by model)
                prompt_tokens = usage.prompt_tokens or 0
                completion_tokens = usage.completion_tokens or 0
                # Conservative estimate — actual price from OpenRouter billing
                cost = (prompt_tokens * 0.000001) + (completion_tokens * 0.000002)

            self._total_cost += cost
            self._call_count += 1

            trace = TraceRecord(
                trace_id=trace_id,
                event_type=trace_event,
                prospect_company=prospect_company,
                thread_id=thread_id,
                input_data={
                    "model": self.model,
                    "messages_count": len(messages),
                    "temperature": temperature,
                    "prompt_tokens": usage.prompt_tokens if usage else 0,
                },
                output_data={
                    "completion_tokens": usage.completion_tokens if usage else 0,
                    "total_tokens": usage.total_tokens if usage else 0,
                    "response_length": len(content),
                },
                cost_usd=cost,
                latency_ms=elapsed_ms,
                model=self.model,
                success=True,
            )

            logger.info(
                "LLM call %s: model=%s, tokens=%s, cost=$%.4f, latency=%.0fms",
                trace_id,
                self.model,
                usage.total_tokens if usage else "?",
                cost,
                elapsed_ms,
            )

            # Emit to Langfuse — non-blocking, failures are swallowed in log_generation
            log_generation(
                trace_id=trace_id,
                name=trace_event,
                model=self.model,
                input_messages=messages,
                output=content,
                usage={"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens} if usage else None,
                cost=cost,
                metadata={"prospect_company": prospect_company, "thread_id": thread_id, "latency_ms": round(elapsed_ms, 1)},
            )

            return content, trace

        except Exception as e:
            elapsed_ms = (time.monotonic() - start_time) * 1000
            logger.error("LLM call %s failed: %s", trace_id, str(e))

            trace = TraceRecord(
                trace_id=trace_id,
                event_type=trace_event,
                prospect_company=prospect_company,
                thread_id=thread_id,
                input_data={"model": self.model, "messages_count": len(messages)},
                output_data={"error": str(e)},
                cost_usd=0.0,
                latency_ms=elapsed_ms,
                model=self.model,
                success=False,
                error=str(e),
            )
            raise

    async def chat_json(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        trace_event: str = "llm_call",
        prospect_company: str | None = None,
        thread_id: str | None = None,
    ) -> tuple[dict, TraceRecord]:
        """
        Chat completion that parses the response as JSON.
        Uses lower temperature for structured output.
        """
        content, trace = await self.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            trace_event=trace_event,
            prospect_company=prospect_company,
            thread_id=thread_id,
        )

        def _try_parse(s: str) -> dict | None:
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return None

        # Happy path: strict JSON
        parsed = _try_parse(content)
        if parsed is not None:
            return parsed, trace

        # Strip markdown code fences if the model wrapped JSON in ```json ... ```
        if "```json" in content:
            try:
                stripped = content.split("```json", 1)[1].split("```", 1)[0].strip()
                parsed = _try_parse(stripped)
                if parsed is not None:
                    return parsed, trace
            except IndexError:
                pass
        if "```" in content:
            try:
                stripped = content.split("```", 1)[1].split("```", 1)[0].strip()
                parsed = _try_parse(stripped)
                if parsed is not None:
                    return parsed, trace
            except IndexError:
                pass

        # Last resort: json_repair. Handles unterminated strings, unescaped
        # quotes, trailing commas, and truncated output — all common LLM
        # failures even when response_format=json_object is set.
        try:
            from json_repair import repair_json
            repaired = repair_json(content, return_objects=True)
            if isinstance(repaired, dict):
                logger.warning(
                    "LLM call %s: JSON repaired (original length=%d)",
                    trace.trace_id, len(content),
                )
                return repaired, trace
        except Exception as e:
            logger.error("json_repair fallback failed: %s", e)

        # Surface the first 300 chars of the raw content to make debugging easy
        logger.error(
            "LLM call %s: could not parse or repair JSON. content[:300]=%r",
            trace.trace_id, content[:300],
        )
        raise json.JSONDecodeError("Unparseable and unrepairable LLM JSON", content, 0)

    @property
    def total_cost(self) -> float:
        return self._total_cost

    @property
    def call_count(self) -> int:
        return self._call_count


# Module-level default client
_default_client: LLMClient | None = None


def get_llm_client(model: str | None = None) -> LLMClient:
    """Get or create the default LLM client."""
    global _default_client
    if model:
        return LLMClient(model=model)
    if _default_client is None:
        _default_client = LLMClient()
    return _default_client
