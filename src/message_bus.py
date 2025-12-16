from __future__ import annotations

from typing import Dict

from src.actors.base import Actor


class MessageBus:
    """Central router that holds actor instances and delivers messages."""

    def __init__(self) -> None:
        self.actors: Dict[str, Actor] = {}

    def register(self, actor: Actor) -> None:
        self.actors[actor.name] = actor
        actor.set_message_bus(self)

    def send(self, from_actor: str, to_actor: str, message: str) -> str:
        if to_actor not in self.actors:
            return f"Unknown actor: {to_actor}"
        # Don't send empty messages
        if not message or not message.strip():
            return ""
        print(f"[MessageBus] {from_actor} -> {to_actor}: {message}")
        recipient = self.actors[to_actor]
        response = recipient.receive(message, from_actor)
        if response:
            print(f"[MessageBus] {to_actor} -> {from_actor}: {response}")
        return response

    def broadcast(self, from_actor: str, message: str) -> Dict[str, str]:
        # Don't send empty messages
        if not message or not message.strip():
            return {}
        responses = {}
        for actor_name, actor in self.actors.items():
            if actor_name != from_actor:
                print(f"[MessageBus] {from_actor} -> {actor_name}: {message}")
                response = actor.receive(message, from_actor)
                print(f"[MessageBus] {actor_name} -> {from_actor}: {response}")
                responses[actor_name] = response
        return responses
