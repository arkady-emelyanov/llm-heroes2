#!/usr/bin/env python3
"""Reads back the engine's battle log.

The log is written by the game itself, at the point where a turn becomes engine commands, so it
records what each side actually did rather than what either side says it did. That is the whole
reason it exists: the harness can only report what it decided to send, and the game's own AI
reports nothing at all.

Two views, both from the same file:

    analyze_log.py battle-log.jsonl             a summary per controller, then the turn list
    analyze_log.py battle-log.jsonl --turns     only the turn list
    analyze_log.py battle-log.jsonl --summary   only the summary
    analyze_log.py battle-log.jsonl --follow    print turns as they are played, and keep watching

Damage is not in the log and is not meant to be: each turn record carries the board as it stood
before its commands were applied, so what a turn cost is whatever changed by the next record.
That is worked out here rather than trusted to anyone's account of it.
"""

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def load(path):
    """Reads the log, skipping anything that is not a JSON object.

    A run that was killed mid-write leaves a truncated last line; that is normal and not worth
    refusing to read the rest of the battle over.
    """
    records = []

    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"{YELLOW}Skipping line {number}: not valid JSON.{RESET}", file=sys.stderr)

    return records


def stacks(board):
    """Flattens one board snapshot to {uid: record}."""
    return {stack["uid"]: stack for side in ("attacker", "defender") for stack in board.get(side, [])}


def board_of(record):
    """The board a record carries, whatever kind of record it is."""
    if record["event"] == "turn":
        return record["board"]

    return {"attacker": record.get("attacker", []), "defender": record.get("defender", [])}


def losses(before, after):
    """Creatures lost between two board snapshots, per stack.

    Only losses are reported. Resurrection and summoning also move these numbers, and calling
    those 'negative losses' would read as a mistake rather than as what happened, so they are
    listed separately by the caller.
    """
    gone = {}

    for uid, stack in stacks(before).items():
        now = stacks(after).get(uid)
        killed = stack["count"] - (now["count"] if now else 0)

        if killed > 0:
            gone[uid] = (stack["name"], killed, now is None)

    return gone


def describe(command):
    """One command as a line of prose."""
    kind = command["type"]

    if kind == "attack":
        where = "" if command["move_to"] == "-" else f" after moving to {command['move_to']}"
        return f"attack #{command['target_uid']}{where}"

    if kind == "move":
        return f"move to {command['to']}"

    if kind == "cast":
        at = "" if command["at"] == "-" else f" at {command['at']}"
        return f"{BOLD}cast {command['spell']}{RESET}{at}"

    if kind == "skip":
        return "skip"

    if kind == "morale":
        return "morale"

    return kind


def turn_line(turn, following=None):
    """One turn as a line, with what it cost the other side.

    The cost comes from the record that follows this one, which carries the board after these
    commands were applied - so a turn with nothing after it yet simply has no cost shown.
    """
    effect = ""

    if following is not None:
        gone = losses(board_of(turn), board_of(following))
        if gone:
            effect = "  ->  " + ", ".join(
                f"{RED}-{killed} {name}{' (wiped out)' if wiped else ''}{RESET}" for name, killed, wiped in gone.values()
            )

    actor = turn["acting"]
    colour = CYAN if turn["controller"] == "external" else DIM
    who = f"{colour}{turn['controller']:<8}{RESET}"
    acting = f"{actor['count']}x {actor['name']} at {actor['cell']}"
    chose = "; ".join(describe(command) for command in turn["commands"]) or "nothing"

    return f"  {who} {turn['side']:<9} {acting:<28} {chose}{effect}"


def print_turns(records):
    """The battle as a list of turns, each with what it cost the other side."""
    start = next((record for record in records if record["event"] == "battle_start"), None)

    if start is not None:
        print(f"{BOLD}Armies{RESET}")
        for side in ("attacker", "defender"):
            for stack in start[side]:
                print(f"  {side:<9} #{stack['uid']} {stack['count']:>4}x {stack['name']} at {stack['cell']}")
        print()

    turns = [record for record in records if record["event"] == "turn"]
    end = next((record for record in records if record["event"] == "battle_end"), None)

    round_number = None

    for index, turn in enumerate(turns):
        if turn["round"] != round_number:
            round_number = turn["round"]
            print(f"{DIM}--- round {round_number} ---{RESET}")

        # What this turn cost is the difference between the board it saw and the board the next
        # record saw. The last turn is bracketed by the battle_end record instead.
        following = turns[index + 1] if index + 1 < len(turns) else end

        print(turn_line(turn, following))

    if end is not None:
        print()
        print(f"{BOLD}Result{RESET}: {end['winner']} wins after {end['rounds']} rounds")
        for side in ("attacker", "defender"):
            survivors = ", ".join(f"{stack['count']}x {stack['name']}" for stack in end[side]) or "nothing left"
            print(f"  {side:<9} {survivors}")


def print_summary(records):
    """What each controller chose to do, counted.

    Grouped by controller rather than by side, because the question this log was added to answer is
    about a player ('does the model ever cast?'), not about a colour.
    """
    turns = [record for record in records if record["event"] == "turn"]

    if not turns:
        print(f"{YELLOW}No turns recorded.{RESET}")
        return

    controllers = {}

    for turn in turns:
        entry = controllers.setdefault(turn["controller"], {"turns": 0, "commands": Counter(), "spells": Counter(), "sides": set()})
        entry["turns"] += 1
        entry["sides"].add(turn["side"])

        for command in turn["commands"]:
            entry["commands"][command["type"]] += 1

            if command["type"] == "cast":
                entry["spells"][command["spell"]] += 1

    for name, entry in sorted(controllers.items()):
        sides = "/".join(sorted(entry["sides"]))
        print(f"{BOLD}{name}{RESET} ({sides}): {entry['turns']} turns")

        for kind, count in entry["commands"].most_common():
            print(f"  {kind:<8} {count}")

        casts = sum(entry["spells"].values())
        if casts:
            spells = ", ".join(f"{spell} x{count}" for spell, count in entry["spells"].most_common())
            print(f"  {GREEN}spells cast: {spells}{RESET}")
        else:
            # Worth stating rather than leaving as an absent line: a player that never casts is
            # usually a bug in how it was asked, not a strategy.
            print(f"  {YELLOW}never cast a spell{RESET}")

        print()


def follow(path):
    """Prints turns as the game writes them.

    A turn's cost is only visible once the *next* record lands, so each line is held back until
    something follows it. Waiting is what makes the losses appear at all, and a battle that is
    mid-turn genuinely does not yet know what the turn cost.
    """
    held = None
    round_number = None
    position = 0

    print(f"{DIM}Watching {path}. Ctrl-C to stop.{RESET}")

    while True:
        with path.open(encoding="utf-8") as handle:
            handle.seek(position)
            lines = handle.readlines()
            position = handle.tell()

        for line in lines:
            line = line.strip()
            if not line:
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # A line caught half-written. Rewind to it and read it again next time round.
                position -= len(line) + 1
                continue

            if record["event"] == "battle_start":
                print(f"{BOLD}Armies{RESET}")
                for side in ("attacker", "defender"):
                    for stack in record[side]:
                        print(f"  {side:<9} #{stack['uid']} {stack['count']:>4}x {stack['name']} at {stack['cell']}")
                print()
                continue

            if held is not None:
                print(turn_line(held, record))
                held = None

            if record["event"] == "battle_end":
                print()
                print(f"{BOLD}Result{RESET}: {record['winner']} wins after {record['rounds']} rounds")
                for side in ("attacker", "defender"):
                    survivors = ", ".join(f"{stack['count']}x {stack['name']}" for stack in record[side]) or "nothing left"
                    print(f"  {side:<9} {survivors}")
                return

            if record["round"] != round_number:
                round_number = record["round"]
                print(f"{DIM}--- round {round_number} ---{RESET}")

            held = record

        # The pending turn is the one being thought about right now, so there is nothing to do but
        # wait for the record that will reveal what it cost.
        time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", type=Path, help="the file written by the game's --battle-log")
    parser.add_argument("--turns", action="store_true", help="only the turn list")
    parser.add_argument("--summary", action="store_true", help="only the per-controller summary")
    parser.add_argument("-f", "--follow", action="store_true", help="print turns as they are played and keep watching until the battle ends")
    args = parser.parse_args()

    if not args.log.exists():
        parser.error(f"no such file: {args.log}. Run a battle with --battle-log {args.log} first.")

    if args.follow:
        follow(args.log)
        return

    records = load(args.log)

    if not records:
        parser.error(f"{args.log} is empty.")

    show_summary = args.summary or not args.turns
    show_turns = args.turns or not args.summary

    if show_summary:
        print_summary(records)

    if show_turns:
        print_turns(records)


if __name__ == "__main__":
    main()
