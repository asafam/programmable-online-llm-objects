from __future__ import annotations

import argparse
import os
from typing import Callable

import yaml
from dotenv import load_dotenv

from src.actors.coordinator_actor import CoordinatorActor
from src.actors.user_actor import UserActor
from src.llm.anthropic_client import AnthropicChatLLM
from src.llm.openai_client import OpenAIChatLLM
from src.message_bus import MessageBus


def build_llm_factory(provider: str, openai_base_url: str = None) -> Callable[[str], object]:
    """Build LLM factory that reads config from system.yml automatically."""
    if provider == "openai":
        def factory(_: str):
            return OpenAIChatLLM(base_url=openai_base_url or None)
        return factory

    if provider == "anthropic":
        def factory(_: str):
            return AnthropicChatLLM()
        return factory

    raise ValueError(f"Unsupported provider: {provider}")


def interactive_loop(provider: str, openai_base_url: str, config: dict) -> None:
    bus = MessageBus()

    if config.get('heartbeat', {}).get('enabled', False):
        from src.actors.heartbeat_actor import HeartbeatActor
        heartbeat = HeartbeatActor(name="Heartbeat", interval=config['heartbeat']['interval'])
        bus.register(heartbeat)
        heartbeat.start()

    llm_factory = build_llm_factory(provider=provider, openai_base_url=openai_base_url)

    coordinator = CoordinatorActor(llm=llm_factory("Coordinator"), llm_factory=llm_factory)
    bus.register(coordinator)

    user_actor = UserActor()
    bus.register(user_actor)

    print("Natural language actor coordinator is live. Type natural-language requests like:")
    print("- Create a task manager to track my todos")
    print("- Add a task: 'Buy groceries' with priority high")
    print("- Set the status of 'Buy groceries' to completed")
    print("- What are my current tasks?")
    print("- Create a budget tracker with $100 initial budget")
    print("Type 'exit' to quit.\n")

    while True:
        try:
            raw = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not raw:
            continue
        if raw.lower() in {"exit", "quit"}:
            print("Goodbye!")
            break

        response = user_actor.send_user_message(raw)
        print(f"[Coordinator] {response}")


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Run the NL actor coordinator.")
    parser.add_argument("--provider", choices=["openai", "anthropic"], default="openai")
    parser.add_argument("--openai-base-url", default=os.getenv("OPENAI_BASE_URL", ""))
    parser.add_argument("--config", default="config/system.yml", help="Path to system config YAML file")
    args = parser.parse_args()

    with open(args.config, 'r') as f:
        config = yaml.safe_load(f)

    interactive_loop(
        provider=args.provider,
        openai_base_url=args.openai_base_url,
        config=config,
    )


if __name__ == "__main__":
    main()
