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
    def evaluate(self, condition: str, evidence: str, context: str = "") -> tuple[bool, str, int, int]:
        """Return (passed, reasoning, input_tokens, output_tokens)."""
        ...

    def evaluate_with_votes(
        self, condition: str, evidence: str, context: str = ""
    ) -> tuple[bool, str, list[dict], int, int]:
        """Return (passed, reasoning, votes, input_tokens, output_tokens).

        Default implementation wraps the single judge result in a one-item vote list.
        Override in panel judges to return per-judge breakdowns.
        """
        passed, reasoning, in_tok, out_tok = self.evaluate(condition, evidence, context)
        model = getattr(self, "model", "unknown")
        vote = {"judge": model, "passed": passed, "reasoning": reasoning,
                "input_tokens": in_tok, "output_tokens": out_tok}
        return passed, reasoning, [vote], in_tok, out_tok


class SubstringJudge(LLMJudge):
    """Fallback judge using substring matching — no API call needed."""

    def evaluate(self, condition: str, evidence: str, context: str = "") -> tuple[bool, str, int, int]:
        passed = condition.lower() in evidence.lower()
        return passed, f"Substring match: '{condition[:60]}' in evidence", 0, 0


class OpenAIJudge(LLMJudge):
    """Judge backed by the OpenAI API."""

    def __init__(self, model: str = "gpt-4o-mini", api_key: Optional[str] = None,
                 system_prompt: Optional[str] = None) -> None:
        import os

        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError("openai package required. Install with: pip install openai")

        self.model = model
        self._client = OpenAI(api_key=api_key or os.environ["OPENAI_API_KEY"])
        self._system_prompt = system_prompt

    def evaluate(self, condition: str, evidence: str, context: str = "") -> tuple[bool, str, int, int]:
        messages = [
            {"role": "system", "content": self._system_prompt or _judge_system_prompt()},
            {"role": "user", "content": _user_msg(condition, evidence, context)},
        ]
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = _safe_json_loads(resp.choices[0].message.content or "{}")
        in_tok = resp.usage.prompt_tokens if resp.usage else 0
        out_tok = resp.usage.completion_tokens if resp.usage else 0
        return bool(raw.get("passed", False)), str(raw.get("reasoning", "")), in_tok, out_tok


class AzureJudge(LLMJudge):
    """Judge backed by Azure OpenAI."""

    def __init__(self, model: str = "gpt-5.4-mini", api_key: Optional[str] = None,
                 endpoint: Optional[str] = None, api_version: Optional[str] = None,
                 system_prompt: Optional[str] = None) -> None:
        import os

        try:
            from openai import AzureOpenAI
        except ImportError:
            raise ImportError("openai package required. Install with: pip install openai")

        self.model = model
        resolved_endpoint = endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")
        if not resolved_endpoint:
            raise ValueError("Azure endpoint required. Set AZURE_OPENAI_ENDPOINT or pass endpoint=.")
        resolved_version = api_version or os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
        resolved_key = api_key or os.environ.get("AZURE_OPENAI_API_KEY")
        if not resolved_key:
            raise ValueError("Azure API key required. Set AZURE_OPENAI_API_KEY or pass api_key=.")
        self._client = AzureOpenAI(
            api_key=resolved_key,
            azure_endpoint=resolved_endpoint,
            api_version=resolved_version,
        )
        self._system_prompt = system_prompt

    def evaluate(self, condition: str, evidence: str, context: str = "") -> tuple[bool, str, int, int]:
        messages = [
            {"role": "system", "content": self._system_prompt or _judge_system_prompt()},
            {"role": "user", "content": _user_msg(condition, evidence, context)},
        ]
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.0,
            response_format={"type": "json_object"},
        )
        raw = _safe_json_loads(resp.choices[0].message.content or "{}")
        in_tok = resp.usage.prompt_tokens if resp.usage else 0
        out_tok = resp.usage.completion_tokens if resp.usage else 0
        return bool(raw.get("passed", False)), str(raw.get("reasoning", "")), in_tok, out_tok


class AnthropicJudge(LLMJudge):
    """Judge backed by the Anthropic API."""

    def __init__(self, model: str = "claude-haiku-4-5-20251001", api_key: Optional[str] = None,
                 system_prompt: Optional[str] = None) -> None:
        import os

        try:
            import anthropic as _anthropic
        except ImportError:
            raise ImportError("anthropic package required. Install with: pip install anthropic")

        self.model = model
        self._client = _anthropic.Anthropic(
            api_key=api_key or os.environ["ANTHROPIC_API_KEY"],
            timeout=120.0,  # 2 min HTTP timeout for judge (short prompts, shouldn't need more)
        )
        self._system_prompt = system_prompt

    def evaluate(self, condition: str, evidence: str, context: str = "") -> tuple[bool, str, int, int]:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=512,
            temperature=0.0,
            system=self._system_prompt or _judge_system_prompt(),
            messages=[{"role": "user", "content": _user_msg(condition, evidence, context)}],
            output_config={"format": {"type": "json_schema", "schema": _JUDGE_SCHEMA}},
        )
        content_str = "".join(block.text for block in resp.content if hasattr(block, "text"))
        raw = _safe_json_loads(content_str or "{}")
        in_tok = resp.usage.input_tokens if resp.usage else 0
        out_tok = resp.usage.output_tokens if resp.usage else 0
        return bool(raw.get("passed", False)), str(raw.get("reasoning", "")), in_tok, out_tok


class GeminiJudge(LLMJudge):
    """Judge backed by the Google Gemini API."""

    def __init__(self, model: str = "gemini-2.5-pro", api_key: Optional[str] = None,
                 system_prompt: Optional[str] = None) -> None:
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
        self._system_prompt = system_prompt

    def evaluate(self, condition: str, evidence: str, context: str = "") -> tuple[bool, str, int, int]:
        config = self._types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=512,
            response_mime_type="application/json",
            response_schema=_JUDGE_SCHEMA,
            system_instruction=self._system_prompt or _judge_system_prompt(),
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
        usage = getattr(resp, "usage_metadata", None)
        in_tok = getattr(usage, "prompt_token_count", 0) or 0
        out_tok = getattr(usage, "candidates_token_count", 0) or 0
        return bool(raw.get("passed", False)), str(raw.get("reasoning", "")), in_tok, out_tok


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
    ) -> tuple[bool, str, list[dict], int, int]:
        raw = [j.evaluate(condition, evidence, context) for j in self._judges]
        votes_bool = [r[0] for r in raw]
        reasonings = [r[1] for r in raw]
        total_in = sum(r[2] for r in raw)
        total_out = sum(r[3] for r in raw)

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
            {"judge": label, "passed": v, "reasoning": r, "input_tokens": ri, "output_tokens": ro}
            for label, v, r, ri, ro in zip(self._labels, votes_bool, reasonings, [x[2] for x in raw], [x[3] for x in raw])
        ]
        return majority_passed, reasoning, votes, total_in, total_out

    def evaluate(self, condition: str, evidence: str, context: str = "") -> tuple[bool, str, int, int]:
        passed, reasoning, _, in_tok, out_tok = self.evaluate_with_votes(condition, evidence, context)
        return passed, reasoning, in_tok, out_tok


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
