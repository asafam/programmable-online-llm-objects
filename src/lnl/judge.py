"""LLM-as-judge — evaluate whether evidence satisfies a condition."""
from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

import yaml

_JUDGE_CONFIG: Optional[dict] = None
_PROMPTS_DIR = Path(__file__).parent.parent.parent / "config" / "prompts" / "lnl"

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "reasoning": {"type": "string"},
    },
    "required": ["passed", "reasoning"],
    "additionalProperties": False,
}


def _load_judge_config() -> dict:
    global _JUDGE_CONFIG
    if _JUDGE_CONFIG is None:
        with open(_PROMPTS_DIR / "judge.yaml") as f:
            _JUDGE_CONFIG = yaml.safe_load(f)
    return _JUDGE_CONFIG


def _judge_system_prompt() -> str:
    return _load_judge_config()["system_prompt"].strip()


def _user_msg(condition: str, evidence: str) -> str:
    return f"Condition: {condition}\n\nEvidence:\n{evidence}"


class LLMJudge(ABC):
    """Evaluate whether evidence satisfies a condition."""

    @abstractmethod
    def evaluate(self, condition: str, evidence: str) -> tuple[bool, str]:
        """Return (passed, reasoning)."""
        ...


class SubstringJudge(LLMJudge):
    """Fallback judge using substring matching — no API call needed."""

    def evaluate(self, condition: str, evidence: str) -> tuple[bool, str]:
        passed = condition.lower() in evidence.lower()
        return passed, f"Substring match: '{condition[:60]}' in evidence"


class OpenAIJudge(LLMJudge):
    """Judge backed by the OpenAI API."""

    def __init__(self, model: str = "gpt-4o-mini", api_key: Optional[str] = None) -> None:
        import os

        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai package required. Install with: pip install openai")

        self.model = model
        self._client = OpenAI(api_key=api_key or os.environ["OPENAI_API_KEY"])

    def evaluate(self, condition: str, evidence: str) -> tuple[bool, str]:
        messages = [
            {"role": "system", "content": _judge_system_prompt()},
            {"role": "user", "content": _user_msg(condition, evidence)},
        ]
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = _safe_json_loads(resp.choices[0].message.content or "{}")
        return bool(raw.get("passed", False)), str(raw.get("reasoning", ""))


class AnthropicJudge(LLMJudge):
    """Judge backed by the Anthropic API."""

    def __init__(self, model: str = "claude-haiku-4-5-20251001", api_key: Optional[str] = None) -> None:
        import os

        try:
            import anthropic as _anthropic
        except ImportError:
            raise ImportError("anthropic package required. Install with: pip install anthropic")

        self.model = model
        self._client = _anthropic.Anthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])

    def evaluate(self, condition: str, evidence: str) -> tuple[bool, str]:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=512,
            temperature=0.0,
            system=_judge_system_prompt(),
            messages=[{"role": "user", "content": _user_msg(condition, evidence)}],
            output_config={"format": {"type": "json_schema", "schema": _JUDGE_SCHEMA}},
        )
        content_str = "".join(block.text for block in resp.content if hasattr(block, "text"))
        raw = _safe_json_loads(content_str or "{}")
        return bool(raw.get("passed", False)), str(raw.get("reasoning", ""))


def _safe_json_loads(text: str) -> dict:
    text = text.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        if "Extra data" in str(e):
            decoder = json.JSONDecoder()
            result, _ = decoder.raw_decode(text)
            return result
        raise
