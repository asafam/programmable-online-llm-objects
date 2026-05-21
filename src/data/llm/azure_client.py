"""Azure OpenAI LLM client for data generation.

Mirrors the OpenAIChatLLM surface but constructs an AzureOpenAI client from
AZURE_OPENAI_* env vars (key, endpoint, api version). All generation methods
are inherited unchanged — Azure's chat-completions API is API-compatible.
"""
from __future__ import annotations

import os
from typing import Optional

from .openai_client import OpenAIChatLLM

try:
    from openai import AzureOpenAI
except ImportError:
    AzureOpenAI = None


class AzureChatLLM(OpenAIChatLLM):
    """Azure OpenAI chat client. Same surface as OpenAIChatLLM, different SDK ctor."""

    def __init__(
        self,
        model: str = "gpt-5.4",
        api_key: Optional[str] = None,
        endpoint: Optional[str] = None,
        api_version: Optional[str] = None,
        temperature: float = 0.7,
        seed: Optional[int] = None,
    ) -> None:
        if AzureOpenAI is None:
            raise ImportError(
                "openai package not installed (need >=1.0 with AzureOpenAI). "
                "Run: pip install -U openai"
            )

        # NOTE: deliberately skipping the OpenAIChatLLM __init__ — it constructs
        # an OpenAI client; we want AzureOpenAI instead.
        self.model = model
        self.temperature = temperature
        self.seed = seed

        api_key = api_key or os.getenv("AZURE_OPENAI_API_KEY")
        endpoint = endpoint or os.getenv("AZURE_OPENAI_ENDPOINT")
        api_version = api_version or os.getenv("AZURE_OPENAI_API_VERSION")
        missing = [
            n for n, v in (
                ("AZURE_OPENAI_API_KEY", api_key),
                ("AZURE_OPENAI_ENDPOINT", endpoint),
                ("AZURE_OPENAI_API_VERSION", api_version),
            ) if not v
        ]
        if missing:
            raise ValueError(
                f"Azure OpenAI requires these env vars: {', '.join(missing)}"
            )
        self.client = AzureOpenAI(
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version=api_version,
        )
