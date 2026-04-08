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


def _user_msg(condition: str, evidence: str, context: str = "") -> str:
    if context:
        return f"Condition: {condition}\n\n{context}\n\nEvidence:\n{evidence}"
    return f"Condition: {condition}\n\nEvidence:\n{evidence}"


class LLMJudge(ABC):
    """Evaluate whether evidence satisfies a condition."""

    @abstractmethod
    def evaluate(self, condition: str, evidence: str, context: str = "") -> tuple[bool, str]:
        """Return (passed, reasoning)."""
        ...

    def evaluate_with_votes(
        self, condition: str, evidence: str, context: str = ""
    ) -> tuple[bool, str, list[dict]]:
        """Return (passed, reasoning, votes).

        Default implementation wraps the single judge result in a one-item vote list.
        Override in panel judges to return per-judge breakdowns.
        """
        passed, reasoning = self.evaluate(condition, evidence, context)
        return passed, reasoning, [{"passed": passed, "reasoning": reasoning}]


class SubstringJudge(LLMJudge):
    """Fallback judge using substring matching — no API call needed."""

    def evaluate(self, condition: str, evidence: str, context: str = "") -> tuple[bool, str]:
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

    def evaluate(self, condition: str, evidence: str, context: str = "") -> tuple[bool, str]:
        messages = [
            {"role": "system", "content": _judge_system_prompt()},
            {"role": "user", "content": _user_msg(condition, evidence, context)},
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

    def evaluate(self, condition: str, evidence: str, context: str = "") -> tuple[bool, str]:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=512,
            temperature=0.0,
            system=_judge_system_prompt(),
            messages=[{"role": "user", "content": _user_msg(condition, evidence, context)}],
            output_config={"format": {"type": "json_schema", "schema": _JUDGE_SCHEMA}},
        )
        content_str = "".join(block.text for block in resp.content if hasattr(block, "text"))
        raw = _safe_json_loads(content_str or "{}")
        return bool(raw.get("passed", False)), str(raw.get("reasoning", ""))


class GeminiJudge(LLMJudge):
    """Judge backed by the Google Gemini API."""

    def __init__(self, model: str = "gemini-2.5-pro", api_key: Optional[str] = None) -> None:
        import os

        try:
            from google import genai
            from google.genai import types as genai_types
        except ImportError:
            raise ImportError("google-genai package required. Install with: pip install google-genai")

        self.model = model
        resolved_key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Google API key required. Set GOOGLE_API_KEY in your environment or .env file, "
                "or pass api_key to GeminiJudge."
            )
        self._client = genai.Client(api_key=resolved_key)
        self._types = genai_types

    def evaluate(self, condition: str, evidence: str, context: str = "") -> tuple[bool, str]:
        config = self._types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=512,
            response_mime_type="application/json",
            response_schema=_JUDGE_SCHEMA,
            system_instruction=_judge_system_prompt(),
        )
        user_content = self._types.Content(
            role="user",
            parts=[self._types.Part(text=_user_msg(condition, evidence, context))],
        )
        resp = self._client.models.generate_content(
            model=self.model,
            contents=[user_content],
            config=config,
        )
        raw = _safe_json_loads(resp.text or "{}")
        return bool(raw.get("passed", False)), str(raw.get("reasoning", ""))


class PanelJudge(LLMJudge):
    """Multi-judge panel with majority-vote agreement.

    With 2 judges: both must agree; disagreement → fail.
    With 3+ judges: simple majority vote; ties → fail.
    """

    def __init__(self, judges: list[LLMJudge], judge_labels: Optional[list[str]] = None) -> None:
        if not judges:
            raise ValueError("PanelJudge requires at least one judge")
        self._judges = judges
        self._labels = judge_labels or [f"judge{i + 1}" for i in range(len(judges))]

    def evaluate_with_votes(
        self, condition: str, evidence: str, context: str = ""
    ) -> tuple[bool, str, list[dict]]:
        raw = [j.evaluate(condition, evidence, context) for j in self._judges]
        votes_bool = [r[0] for r in raw]
        reasonings = [r[1] for r in raw]

        pass_count = sum(votes_bool)
        total = len(votes_bool)
        summaries = "; ".join(
            f"{label}={'PASS' if v else 'FAIL'}: {r[:80]}"
            for label, v, r in zip(self._labels, votes_bool, reasonings)
        )

        if pass_count == total - pass_count:
            reasoning = f"Judges tied ({pass_count}/{total} pass) — {summaries}"
            majority_passed = False
        else:
            majority_passed = pass_count > total - pass_count
            verdict = "PASS" if majority_passed else "FAIL"
            reasoning = f"{verdict} ({pass_count}/{total} judges agree) — {summaries}"

        votes = [
            {"judge": label, "passed": v, "reasoning": r}
            for label, v, r in zip(self._labels, votes_bool, reasonings)
        ]
        return majority_passed, reasoning, votes

    def evaluate(self, condition: str, evidence: str, context: str = "") -> tuple[bool, str]:
        passed, reasoning, _ = self.evaluate_with_votes(condition, evidence, context)
        return passed, reasoning


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
