"""CLI — thin wrapper over Runtime for interactive use."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .brain import AnthropicBrain, AzureBrain, MockBrain, OpenAIBrain
from .runtime import SystemConfig, Runtime


def _make_brain(args: argparse.Namespace):
    provider = args.provider
    model = args.model
    if provider == "openai":
        return OpenAIBrain(model=model or "gpt-4o-mini")
    elif provider == "azure":
        return AzureBrain(model=model or "gpt-5.4-mini")
    elif provider == "anthropic":
        return AnthropicBrain(model=model or "claude-3-5-sonnet-latest")
    elif provider == "mock":
        return MockBrain()
    else:
        print(f"Unknown provider: {provider}", file=sys.stderr)
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="lnl",
        description="Live Natural Language Programming Runtime",
    )
    parser.add_argument("--provider", default="openai", choices=["openai", "azure", "anthropic", "mock"])
    parser.add_argument("--model", default=None)
parser.add_argument("--max-chain-depth", type=int, default=10)

    sub = parser.add_subparsers(dest="command")

    # live — listed first; primary mode for persistent objects
    p_live = sub.add_parser("live", help="Start the runtime (recommended — objects stay live)")
    p_live.add_argument("path", help="Directory containing .md files")

    # load
    p_load = sub.add_parser("load", help="Load objects from directory (one-shot; use 'live' for persistence)")
    p_load.add_argument("path", help="Directory containing .md files")

    # new
    p_new = sub.add_parser("new", help="Create a new object from markdown file")
    p_new.add_argument("path", help="Path to .md file")

    # send
    p_send = sub.add_parser("send", help="Send a message to an object")
    p_send.add_argument("recipient")
    p_send.add_argument("content")
    p_send.add_argument("--sender", default="__user__")

    # event
    p_event = sub.add_parser("event", help="Send an event to an object")
    p_event.add_argument("recipient")
    p_event.add_argument("content")

    # modify
    p_modify = sub.add_parser("modify", help="Modify an object's definition")
    p_modify.add_argument("object_id")
    p_modify.add_argument("--role", default=None)
    p_modify.add_argument("--behavior", default=None)

    # state
    p_state = sub.add_parser("state", help="Show object state")
    p_state.add_argument("object_id")

    # snapshot
    p_snap = sub.add_parser("snapshot", help="Show full object snapshot")
    p_snap.add_argument("object_id")

    # topology
    sub.add_parser("topology", help="Show communication topology")

    # log
    sub.add_parser("log", help="Show message log")

    # save
    p_save = sub.add_parser("save", help="Save an object to disk")
    p_save.add_argument("object_id")
    p_save.add_argument("--path", default=None)

    # run
    p_run = sub.add_parser("run", help="Run a benchmark scenario")
    p_run.add_argument("path", help="Scenario directory or scenarios parent directory")

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return

    cfg = SystemConfig.load()
    brain = _make_brain(args)
    rt = Runtime(
        brain,
        # CLI flag overrides config file if explicitly provided (argparse default is 10)
        max_chain_depth=args.max_chain_depth,
        system_config=cfg,
    )

    if args.command == "load":
        objects = rt.load_directory(args.path)
        for obj in objects:
            print(f"Loaded: {obj.object_id}")

    elif args.command == "new":
        obj = rt.load_file(args.path)
        print(f"Created: {obj.object_id}")

    elif args.command == "send":
        results = rt.send(args.recipient, args.content, sender=args.sender)
        for r in results:
            print(f"[{r.object_id}] {r.reply}")

    elif args.command == "event":
        from .gateway import EventGateway
        gw = EventGateway(rt)
        results = gw.dispatch(args.recipient, args.content)
        for r in results:
            print(f"[{r.object_id}] {r.reply}")

    elif args.command == "modify":
        updates = {}
        if args.role:
            updates["role"] = args.role
        if args.behavior:
            updates["behavior"] = args.behavior

        if updates:
            rt.modify(args.object_id, **updates)
            print(f"Modified: {args.object_id}")
        else:
            print("No modifications specified.")

    elif args.command == "state":
        print(rt.state(args.object_id))

    elif args.command == "snapshot":
        snap = rt.snapshot(args.object_id)
        print(json.dumps(snap, indent=2))

    elif args.command == "topology":
        topo = rt.topology()
        for oid, peers in topo.items():
            peers_str = ", ".join(peers) if peers else "(none)"
            print(f"{oid} -> {peers_str}")

    elif args.command == "log":
        for entry in rt.message_log:
            status = "OK" if entry.delivered else f"FAIL: {entry.error}"
            print(
                f"{entry.message.sender} -> {entry.message.recipient}: "
                f"{str(entry.message.content)[:80]} [{status}]"
            )

    elif args.command == "save":
        path = rt.save_object(args.object_id, args.path)
        print(f"Saved: {path}")

    elif args.command == "live":
        objects = rt.load_directory(args.path)
        for obj in objects:
            print(f"  Loaded: {obj.object_id}")
        print()
        _live_repl(rt)

    elif args.command == "run":
        from .benchmark import BenchmarkHarness
        harness = BenchmarkHarness(brain)
        p = Path(args.path)
        if (p / "scenario.yaml").exists():
            scenario = harness.load_scenario(p)
            result = harness.run_scenario(scenario)
            _print_scenario_result(result)
        else:
            results = harness.run_directory(p)
            for result in results:
                _print_scenario_result(result)
                print()


def _live_repl(rt: Runtime) -> None:
    """Interactive REPL with a live background runtime."""
    from .gateway import EventGateway

    gw = EventGateway(rt)

    def _on_result(r):
        print(f"  [{r.object_id}] {r.reply}")

    rt.start(on_result=_on_result)
    print("Runtime started. Commands: send <id> <msg>, event <id> <msg>, state <id>, kill <id>, topology, objects, quit")
    print()

    try:
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                break
            if not line:
                continue

            parts = line.split(None, 2)
            cmd = parts[0].lower()

            if cmd == "quit":
                break
            elif cmd == "send" and len(parts) >= 3:
                rt.send(parts[1], parts[2])
            elif cmd == "event" and len(parts) >= 3:
                gw.dispatch(parts[1], parts[2])
            elif cmd == "state" and len(parts) >= 2:
                try:
                    print(rt.state(parts[1]))
                except KeyError as e:
                    print(f"Error: {e}")
            elif cmd == "kill" and len(parts) >= 2:
                rt.kill_object(parts[1])
                print(f"Killed: {parts[1]}")
            elif cmd == "topology":
                for oid, peers in rt.topology().items():
                    peers_str = ", ".join(peers) if peers else "(none)"
                    print(f"  {oid} -> {peers_str}")
            elif cmd == "objects":
                for oid in rt.topology():
                    print(f"  {oid}")
            elif cmd == "snapshot" and len(parts) >= 2:
                try:
                    print(json.dumps(rt.snapshot(parts[1]), indent=2))
                except KeyError as e:
                    print(f"Error: {e}")
            else:
                print(f"Unknown command: {line}")
    except KeyboardInterrupt:
        pass
    finally:
        print("\nStopping runtime...")
        rt.stop()
        print("Done.")


def _print_scenario_result(result):
    print(f"Scenario: {result.name}")
    print(f"  Pass rate: {result.pass_rate:.0%}")
    print(f"  Tokens: {result.total_input_tokens} in / {result.total_output_tokens} out")
    for ar in result.assertion_results:
        status = "PASS" if ar.passed else "FAIL"
        print(f"  [{status}] {ar.assertion.type}:{ar.assertion.target} — {ar.assertion.condition}")
        if not ar.passed:
            print(f"         Actual: {ar.actual[:100]}")


if __name__ == "__main__":
    main()
