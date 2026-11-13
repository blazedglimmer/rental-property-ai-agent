"""Thin Claude wrapper.

* Structured extraction uses `beta.messages.parse` with a Pydantic schema (validated output).
* Server-side refusal fallbacks are enabled (`fallbacks="default"`).
* Every caller must handle `LLMUnavailable` and fall back to deterministic logic.
"""
from __future__ import annotations

import json
import logging
import re
from typing import TypeVar

from pydantic import BaseModel

from ..config import get_settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMUnavailable(RuntimeError):
    pass


class LLM:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._client = None
        if self.settings.anthropic_api_key:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.settings.anthropic_api_key, max_retries=3)

    @property
    def available(self) -> bool:
        return self._client is not None

    def _common(self, effort: str) -> dict:
        kw: dict = {"model": self.settings.llm_model, "output_config": {"effort": effort}}
        if self.settings.llm_enable_fallbacks:
            kw["betas"] = [FALLBACK_BETA]
            kw["fallbacks"] = "default"
        return kw

    @staticmethod
    def _check(resp) -> None:
        if resp.stop_reason == "refusal":
            raise LLMUnavailable(f"model declined: {getattr(resp, 'stop_details', None)}")
        if resp.stop_reason == "max_tokens":
            raise LLMUnavailable("response truncated (max_tokens)")

    def structured(self, system: str, user: str, schema: type[T], effort: str = "low",
                   max_tokens: int = 16000) -> T:
        if not self._client:
            raise LLMUnavailable("ANTHROPIC_API_KEY not configured")
        import anthropic
        try:
            resp = self._client.beta.messages.parse(
                max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}],
                output_format=schema, **self._common(effort))
        except anthropic.APIError as e:  # network, 4xx, 5xx after SDK retries
            raise LLMUnavailable(str(e)) from e
        self._check(resp)
        if resp.parsed_output is None:
            raise LLMUnavailable("no parsed output")
        return resp.parsed_output

    def text(self, system: str, user: str, effort: str = "medium", max_tokens: int = 16000) -> str:
        if not self._client:
            raise LLMUnavailable("ANTHROPIC_API_KEY not configured")
        import anthropic
        try:
            resp = self._client.beta.messages.create(
                max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}], **self._common(effort))
        except anthropic.APIError as e:
            raise LLMUnavailable(str(e)) from e
        self._check(resp)
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()

    def web_search_json(self, system: str, user: str, max_searches: int = 5,
                        allowed_domains: list[str] | None = None) -> tuple[list[dict], list[str]]:
        """Run Claude with the server-side web search tool; expects a final ```json array.

        Returns (items, cited_urls). Search runs on Anthropic's infrastructure — we never
        scrape the result sites ourselves.
        """
        if not self._client:
            raise LLMUnavailable("ANTHROPIC_API_KEY not configured")
        import anthropic
        tool: dict = {"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches,
                      "user_location": {"type": "approximate", "country": "IN"}}
        if allowed_domains:
            tool["allowed_domains"] = allowed_domains
        messages = [{"role": "user", "content": user}]
        try:
            for _ in range(4):  # handle pause_turn continuations
                with self._client.beta.messages.stream(
                        max_tokens=32000, system=system, messages=messages, tools=[tool],
                        **self._common("medium")) as stream:
                    resp = stream.get_final_message()
                if resp.stop_reason != "pause_turn":
                    break
                messages = messages + [{"role": "assistant", "content": resp.content}]
        except anthropic.APIError as e:
            raise LLMUnavailable(str(e)) from e
        self._check(resp)
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        urls: list[str] = []
        for b in resp.content:
            if getattr(b, "type", "") == "web_search_tool_result" and isinstance(b.content, list):
                urls += [r.url for r in b.content if getattr(r, "url", None)]
        return extract_json_array(text), urls


def extract_json_array(text: str) -> list[dict]:
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S) or re.search(r"(\[\s*\{.*\}\s*\])", text, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
        return [d for d in data if isinstance(d, dict)]
    except json.JSONDecodeError:
        return []


_llm: LLM | None = None


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        _llm = LLM()
    return _llm


def reset_llm() -> None:
    global _llm
    _llm = None
