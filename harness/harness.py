#!/usr/bin/env python3
"""Reference harness for the fheroes2 battle-only binary.

Listens for the game and plays one side of the battle. This module is only the
server and the protocol; the thinking lives in agent.py, a LangGraph agent that
assesses the board, drafts a move, checks it against the legal actions the game
sent, and revises if its own check fails.

See battle/PROTOCOL.md for the wire format.

    ./harness.py --port 9000 --model gpt-4o-mini
    fheroes2-battle --red external:tcp://127.0.0.1:9000

--random plays legal moves without a model and without any dependency beyond the
standard library, which is the quickest way to check that the game, the protocol
and your setup work before spending tokens.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path

PROTOCOL_VERSION = 1


class ProtocolError(RuntimeError):
    pass


class Connection:
    """One game connection, framed as newline-delimited JSON."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buffer = b""

    def receive(self) -> dict | None:
        """Returns the next message, or None when the game closes the connection."""
        while b"\n" not in self._buffer:
            chunk = self._sock.recv(65536)
            if not chunk:
                return None
            self._buffer += chunk

        line, _, self._buffer = self._buffer.partition(b"\n")

        try:
            return json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"the game sent something that is not JSON: {exc}") from exc

    def send(self, message: dict) -> None:
        self._sock.sendall((json.dumps(message) + "\n").encode("utf-8"))


def pick_random_action(turn: dict) -> dict:
    """A legal move chosen without a model. Prefers attacking, so battles actually end."""
    legal = turn["legal_actions"]

    if legal["attack"]:
        return {"action": "attack", "target": random.choice(legal["attack"])["target"]}
    if legal["move"]:
        return {"action": "move", "cell": random.choice(legal["move"])}

    return {"action": "skip"}


def play_connection(conn: Connection, agent, verbose: bool) -> None:
    """Serves one battle, until the game hangs up.

    'agent' is a BattleAgent, or None to play random legal moves.
    """
    while True:
        message = conn.receive()
        if message is None:
            print("Game disconnected; battle over.", file=sys.stderr)
            return

        kind = message.get("type")

        if kind == "hello":
            version = message.get("protocol")
            if version != PROTOCOL_VERSION:
                raise ProtocolError(f"the game speaks protocol {version}, this harness speaks {PROTOCOL_VERSION}")

            print(f"Game connected, playing the {message.get('side')}.", file=sys.stderr)
            conn.send({"type": "ready"})
            continue

        if kind == "error":
            # The previous action was rejected. Hand the reason back to the model so it can
            # correct itself rather than repeating the same mistake.
            reason = message.get("message", "unknown reason")
            print(f"  rejected: {reason}", file=sys.stderr)

            # Reaching here means the agent's own validation disagreed with the engine, which is
            # worth recording; the agent folds the reason into its notes for the next turn.
            conn.send({"action": "skip"} if agent is None else agent.on_game_rejection(reason))
            continue

        if kind != "turn":
            raise ProtocolError(f"unexpected message type {kind!r}")

        if verbose:
            print(f"\n--- turn {message['turn']} ---", file=sys.stderr)

        action = pick_random_action(message) if agent is None else agent.decide(message)

        if verbose:
            print(f"  sending: {action}", file=sys.stderr)

        conn.send(action)


ROOT_DIR = Path(__file__).resolve().parent.parent
BATTLE_DIR = ROOT_DIR / "battle"
SCENARIO_DIR = BATTLE_DIR / "scenarios"
GAME_BINARY = BATTLE_DIR / "fheroes2-battle"

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
RESET = "\033[0m"


def list_scenarios() -> list[Path]:
    if not SCENARIO_DIR.is_dir():
        return []

    return sorted(SCENARIO_DIR.glob("*.json"))


def describe_scenario(path: Path) -> str:
    """One line describing who fights whom, read out of the scenario itself."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return "(unreadable)"

    def side(key: str) -> str:
        entry = data.get(key) or {}
        troops = ", ".join(f"{t.get('count')}x {t.get('monster')}" for t in entry.get("troops", []))
        return f"{entry.get('hero', '?')} ({troops})"

    return f"{side('blue')} vs {side('red')}"


# Model settings a scenario may carry, and the agent argument each maps to. Keeping them in the
# scenario makes a file a complete description of an experiment - the armies and how the model is
# asked to think about them - so a run can be reproduced from one file.
SCENARIO_LLM_KEYS = {
    "reasoning_effort": "reasoning_effort",
    "max_tokens": "max_tokens",
    "assess_every": "assess_every",
    "refresh_every": "refresh_every",
    "board_map": "board_map",
    "temperature": "temperature",
    "model": "model",
}


def load_scenario_llm(path: Path | None) -> dict:
    """Reads the optional "llm" block of a scenario. Unknown keys are refused rather than ignored,
    because a silently dropped setting looks exactly like a setting that did not work."""
    if path is None or not path.exists():
        return {}

    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read the scenario '{path}': {exc}")

    block = data.get("llm")
    if block is None:
        return {}

    if not isinstance(block, dict):
        raise SystemExit(f"the \"llm\" block of '{path}' must be an object")

    unknown = sorted(set(block) - set(SCENARIO_LLM_KEYS))
    if unknown:
        raise SystemExit(f"the \"llm\" block of '{path}' has unknown settings: {', '.join(unknown)}. Known: {', '.join(sorted(SCENARIO_LLM_KEYS))}")

    return {SCENARIO_LLM_KEYS[key]: value for key, value in block.items()}


def print_banner(host: str, port: int, how: str, scenarios: list[Path]) -> None:
    print(f"\n{GREEN}{BOLD}Harness listening on {host}:{port}{RESET}, playing with {how}.\n", file=sys.stderr)

    print(f"{BOLD}Run a battle in another terminal:{RESET}", file=sys.stderr)
    print(f"  make run-vs-llm                      {DIM}# you play blue, the model plays red{RESET}", file=sys.stderr)

    if scenarios:
        print(f"\n{BOLD}Or a scripted battle, no clicking needed:{RESET}", file=sys.stderr)
        for scenario in scenarios:
            print(f"  make scenario-vs-llm SCENARIO={scenario}", file=sys.stderr)
            print(f"      {DIM}{describe_scenario(scenario)}{RESET}", file=sys.stderr)

    print(file=sys.stderr)


def stop_demo(demo: subprocess.Popen | None) -> None:
    """Closes the game we started.

    A game whose harness has gone is not useful: it will sit waiting for a move that can never
    arrive, or on a dialog nobody asked for. Since this process started it, this process ends it -
    politely first, so it can shut down cleanly, and firmly if it does not.
    """
    if demo is None or demo.poll() is not None:
        return

    print(f"{DIM}Closing the game...{RESET}", file=sys.stderr)

    demo.terminate()

    try:
        demo.wait(timeout=5)
    except subprocess.TimeoutExpired:
        demo.kill()
        demo.wait(timeout=5)


def run_demo(
    host: str,
    port: int,
    scenario: Path,
    reply_timeout: int,
    pause: bool = False,
    battle_log: str | None = None,
) -> subprocess.Popen | None:
    """Starts the game so the model plays the game's own AI, with nothing to click.

    Returns the process, or None if it could not be started. A demo that cannot start is reported
    rather than left to look like a harness waiting for a connection that will never come.
    """
    if not GAME_BINARY.exists():
        print(f"{YELLOW}Cannot start the demo: '{GAME_BINARY}' is not built. Run: make build{RESET}", file=sys.stderr)
        return None

    if not (BATTLE_DIR / "data" / "HEROES2.AGG").exists():
        print(f"{YELLOW}Cannot start the demo: game assets are missing. Run: make assets{RESET}", file=sys.stderr)
        return None

    command = [
        str(GAME_BINARY),
        "--blue", "ai",
        "--red", f"external:tcp://{host}:{port}",
        # Absolute: the game is started with its own directory as the working directory, so a
        # relative path given here would be resolved against the wrong place.
        "--scenario", str(scenario.resolve()),
        "--reply-timeout", str(reply_timeout),
    ]

    if pause:
        command.append("--pause")

    if battle_log:
        # Absolute for the same reason as the scenario above.
        command += ["--battle-log", str(Path(battle_log).resolve())]

    environment = dict(os.environ)
    # Without a display the battle still runs; with one, it can be watched.
    if not environment.get("DISPLAY") and not environment.get("WAYLAND_DISPLAY"):
        environment["SDL_VIDEODRIVER"] = "dummy"
        environment["SDL_AUDIODRIVER"] = "dummy"
        print(f"{DIM}No display detected, running the battle headless.{RESET}", file=sys.stderr)

    print(f"{GREEN}{BOLD}Demo: the model plays red against the game's own AI.{RESET}", file=sys.stderr)
    print(f"{DIM}  {describe_scenario(scenario)}{RESET}\n", file=sys.stderr)

    return subprocess.Popen(command, cwd=str(BATTLE_DIR), env=environment)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on (default: %(default)s)")
    parser.add_argument("--port", type=int, default=9000, help="port to listen on (default: %(default)s)")
    parser.add_argument("--random", action="store_true", help="play legal moves without calling a model")
    parser.add_argument("--relay", metavar="DIR", help="play by hand through files in DIR: the harness writes turn.md and waits for a file called move")
    parser.add_argument("--model", help="model name (default: gpt-4o-mini, or whatever the scenario asks for)")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        help="OpenAI-compatible base URL (default: $OPENAI_BASE_URL or %(default)s)",
    )
    parser.add_argument("--max-attempts", type=int, default=3, help="how many times the agent may revise an illegal move before sending it anyway (default: %(default)s)")
    parser.add_argument("--max-tokens", type=int, help="output budget per call; a reasoning model spends most of it thinking (default: 16384)")
    parser.add_argument("--context-window", type=int, default=0, help="context window in tokens; 0 asks the endpoint (default: %(default)s)")
    parser.add_argument("--board-map", dest="board_map", action="store_const", const=True, default=None,
                        help="draw the battlefield as an offset-hex map instead of bare cell numbers")
    parser.add_argument("--refresh-every", type=int, help="re-send the whole board every N turns instead of a delta (default: 10)")
    parser.add_argument("--assess-every", type=int, help="re-plan every N turns; 1 means every turn, which doubles the cost (default: 4)")
    parser.add_argument("--reasoning-effort", choices=["off", "minimal", "low", "medium", "high"],
                        help="how hard the model should think per move; 'off' disables thinking entirely. Not every server supports this")
    parser.add_argument("--no-reasoning", action="store_true", help="do not stream the model's thinking")
    parser.add_argument("--journal", default=str(ROOT_DIR / "harness" / "journal.jsonl"),
                        help="where to record rejected actions for later prompt work (default: %(default)s)")
    parser.add_argument("--demo", action="store_true", help="start the game too: the model plays the game's own AI, nothing to click")
    parser.add_argument("--scenario", help="scenario for --demo (default: the first one in battle/scenarios)")
    parser.add_argument("--pause", action="store_true",
                        help="have the game wait for a click before the first turn, so a screen recorder can be started")
    parser.add_argument("--battle-log", help="record every choice both sides make to this file, one JSON object per line, as the engine received it")
    parser.add_argument("--reply-timeout", type=int, default=1800, help="seconds the game waits for one move before giving up (default: %(default)s)")
    parser.add_argument("--once", action="store_true", help="serve a single battle and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="print timings and prompts")

    args = parser.parse_args()

    # SIGTERM would otherwise end this process outright, skipping the cleanup that closes the game
    # we started. Turning it into an exception lets the same path handle it as Ctrl-C.
    def on_terminate(signum, _frame):
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, on_terminate)

    scenarios = list_scenarios()

    # A scenario may carry model settings. Anything given on the command line wins over the file,
    # so a scenario is a starting point rather than a straitjacket.
    chosen_scenario = Path(args.scenario) if args.scenario else (scenarios[0] if (args.demo and scenarios) else None)
    settings = {"model": "gpt-4o-mini", "max_tokens": 16384, "assess_every": 4, "refresh_every": 10, "board_map": False, "temperature": 0.2, "reasoning_effort": None}
    settings.update(load_scenario_llm(chosen_scenario))

    for name in SCENARIO_LLM_KEYS.values():
        given = getattr(args, name, None)
        if given is not None:
            settings[name] = given

    agent_factory = None
    context_window = args.context_window

    # Only the LangGraph agent keeps one. --random and --relay leave it None, and the summary at the
    # end has to ask rather than assume: reading it unconditionally crashed every relayed battle on
    # exit, after the result had already been printed.
    journal = None

    if args.relay and args.random:
        parser.error("--relay and --random both answer the turns; pick one.")

    if args.relay:
        try:
            from relay import RelayAgent
        except ImportError as exc:
            parser.error(f"cannot import the relay ({exc}). It shares the agent's prompt code, so install it with:\n    make harness-deps")

        relay_dir = Path(args.relay)

        def agent_factory() -> "RelayAgent":
            return RelayAgent(relay_dir, verbose=args.verbose)

        print(f"{GREEN}Playing by hand.{RESET} Each turn is written to {relay_dir / 'turn.md'}; "
              f"answer by writing the number(s) to {relay_dir / 'move'}.", file=sys.stderr)

    if not args.random and not args.relay:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key and "localhost" not in args.base_url and "127.0.0.1" not in args.base_url:
            parser.error("OPENAI_API_KEY is not set. Export it, or pass --random to play without a model.")

        # Imported lazily so that --random keeps working without the LangGraph dependencies.
        try:
            from agent import BattleAgent
        except ImportError as exc:
            parser.error(f"cannot import the LangGraph agent ({exc}). Install it with:\n    make harness-deps\nor pass --random to play without a model.")

        from endpoint import EndpointError, verify
        from journal import Journal

        journal = Journal(args.journal)

        # Checked before anything else: a battle is long and mostly unattended, so an endpoint
        # problem should surface now rather than on turn 14.
        try:
            info = verify(args.base_url, settings["model"], api_key)
        except EndpointError as exc:
            parser.error(str(exc))

        if context_window <= 0:
            context_window = info.context_window or 8192

        effort = settings["reasoning_effort"]
        print(f"{GREEN}Endpoint OK{RESET}: {settings['model']} at {args.base_url}, context window {context_window} tokens, "
              f"max {settings['max_tokens']} output tokens per call, thinking effort {effort or 'default'}.", file=sys.stderr)

        if settings["max_tokens"] >= context_window:
            parser.error(f"max_tokens {settings['max_tokens']} does not fit in a {context_window}-token window.")

        # A fresh agent per battle, so its notes do not leak from one battle into the next.
        def agent_factory() -> "BattleAgent":  # noqa: F811
            return BattleAgent(
                model=settings["model"],
                base_url=args.base_url,
                api_key=api_key,
                temperature=settings["temperature"],
                max_attempts=args.max_attempts,
                max_tokens=settings["max_tokens"],
                context_window=context_window,
                assess_every=settings["assess_every"],
                refresh_every=settings["refresh_every"],
                board_map=settings["board_map"],
                reasoning_effort=settings["reasoning_effort"],
                show_reasoning=not args.no_reasoning,
                journal=journal,
                verbose=args.verbose,
            )

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.host, args.port))
        server.listen(1)

        how = "random legal moves" if args.random else ("moves played by hand through " + args.relay if args.relay else f"a LangGraph agent on {settings['model']}")
        print_banner(args.host, args.port, how, scenarios)

        demo = None
        if args.demo:
            scenario = chosen_scenario

            if scenario is None:
                parser.error(f"no scenarios found in {SCENARIO_DIR}; pass --scenario <file.json>")
            if not scenario.exists():
                parser.error(f"scenario '{scenario}' does not exist")

            demo = run_demo(args.host, args.port, scenario, args.reply_timeout, args.pause, args.battle_log)
            if demo is None:
                return 1

        try:
            while True:
                sock, peer = server.accept()
                print(f"{DIM}Connection from {peer[0]}:{peer[1]}{RESET}", file=sys.stderr)

                with sock:
                    try:
                        play_connection(Connection(sock), None if agent_factory is None else agent_factory(), args.verbose)
                    except (ProtocolError, RuntimeError, OSError) as exc:
                        print(f"Battle aborted: {exc}", file=sys.stderr)

                if demo is not None:
                    demo.wait()

                    if journal is not None:
                        print(f"\n{BOLD}Journal:{RESET} {journal.summary()}", file=sys.stderr)

                    return demo.returncode

                if args.once:
                    return 0
        except (KeyboardInterrupt, SystemExit):
            print(file=sys.stderr)
            return 130
        finally:
            # Whatever ended this - Ctrl-C, an error, or the battle finishing - the game we started
            # does not outlive us.
            stop_demo(demo)


if __name__ == "__main__":
    sys.exit(main())
