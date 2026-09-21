"""Lets a person - or an agent driving this repository - play a side by hand.

Stands in for the LangGraph agent, and is asked the same questions in the same words: the frozen
briefing on the first turn, then a delta, the two numbered menus, the damage estimates. Nothing is
translated or summarised on the way through, so a move made here is made from exactly what the model
would have been given, and the two are comparable.

It talks through two files rather than a terminal prompt, because whoever is answering may not be
sitting at one:

    <dir>/turn.md   written by the harness; the question, rewritten each turn
    <dir>/move      written by the answerer; one or two numbers, then deleted by the harness

One number is the stack action. Two numbers are the spell and then the stack action, in the order
the menus appear - "0 5" casts nothing and takes action 5.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from agent import (
    Action,
    build_briefing,
    enumerate_spells,
    enumerate_unit_actions,
    render_board,
    snapshot,
    validate_action,
)

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RESET = "\033[0m"


class RelayAgent:
    """Asks a file for each move instead of asking a model."""

    def __init__(self, directory: Path, poll_seconds: float = 2.0, verbose: bool = False) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

        self._question = self._dir / "turn.md"
        self._answer = self._dir / "move"

        self._poll = poll_seconds
        self._verbose = verbose

        self._briefed = False
        self._last_shown: dict | None = None

        # Set while a turn is being played, so a rejection from the game can be re-posed against the
        # same board rather than an empty one.
        self._turn: dict | None = None
        self._menu: list[tuple[str, dict]] = []
        self._spells: list[tuple[str, dict]] = []

        # Held when a spell is cast: casting does not use up the stack's action, so the game asks
        # again for the same stack and the action chosen alongside the spell is played then.
        self._pending: dict | None = None

    def decide(self, turn: dict) -> dict:
        # The stack that just cast already said what it wanted to do; do that instead of asking again.
        if self._pending is not None and self._pending["uid"] == turn["unit"]["uid"] and self._pending["turn"] == turn["turn"]:
            action = self._pending["wire"]
            self._pending = None

            if self._verbose:
                print(f"  {DIM}playing the action held from the cast: {action}{RESET}", file=sys.stderr)

            return action

        self._turn = turn
        self._menu = enumerate_unit_actions(turn)
        self._spells = enumerate_spells(turn)

        self._pose(self._render(turn))

        while True:
            choice = self._await_answer()

            spell_number, action_number = choice

            if self._spells and 1 <= spell_number <= len(self._spells):
                label, wire = self._spells[spell_number - 1]

                if not 1 <= action_number <= len(self._menu):
                    self._pose(self._render(turn, complaint=f"{action_number} is not on the stack action menu; pick 1 to {len(self._menu)}."))
                    continue

                # Both halves are legal, so the spell goes now and the action is held for when the
                # game comes back round to the same stack.
                self._pending = {"wire": self._menu[action_number - 1][1], "uid": turn["unit"]["uid"], "turn": turn["turn"]}

                print(f"  {GREEN}casting{RESET} {label}", file=sys.stderr)
                print(f"  {DIM}then: {self._menu[action_number - 1][0]}{RESET}", file=sys.stderr)

                return wire

            if not 1 <= action_number <= len(self._menu):
                self._pose(self._render(turn, complaint=f"{action_number} is not on the stack action menu; pick 1 to {len(self._menu)}."))
                continue

            label, wire = self._menu[action_number - 1]

            # The same check the model's answers go through, so a hand-played move cannot reach the
            # game in a state the agent's would have been stopped in.
            rejection = validate_action(Action(reasoning=label, **wire), turn)
            if rejection:
                self._pose(self._render(turn, complaint=f"That move was refused: {rejection}"))
                continue

            print(f"  {GREEN}playing{RESET} {label}", file=sys.stderr)

            return wire

    def on_game_rejection(self, reason: str) -> dict:
        """The game refused the last action; ask again against the same board."""
        self._pending = None

        if self._turn is None:
            return {"action": "skip"}

        return self.decide(self._turn)

    def _render(self, turn: dict, complaint: str | None = None) -> str:
        """The turn, written exactly as the model would be given it."""
        parts: list[str] = []

        if not self._briefed:
            parts.append(build_briefing(turn))
            self._briefed = True

        # render_board() already ends with the two menus, so they are not appended again here.
        parts.append(render_board(turn, full=self._last_shown is None, previous=self._last_shown))
        self._last_shown = snapshot(turn)

        if complaint:
            parts.append(f"!! {complaint}")

        parts.append(
            f"Write your answer to '{self._answer}': the stack action number on its own, "
            "or the spell number and the stack action number separated by a space."
        )

        return "\n\n".join(parts)

    def _pose(self, question: str) -> None:
        # Any answer sitting there is an answer to the previous question. Clearing it before the new
        # one is written is what stops a stale file being read as a reply to this turn.
        self._answer.unlink(missing_ok=True)
        self._question.write_text(question, encoding="utf-8")

        turn = self._turn["turn"] if self._turn else "?"
        unit = self._turn["unit"] if self._turn else {}

        print(f"\n{BOLD}Your move{RESET} - turn {turn}, {unit.get('count', '?')}x {unit.get('name', '?')}", file=sys.stderr)
        print(f"  read  {self._question}", file=sys.stderr)
        print(f"  write {self._answer}", file=sys.stderr)

    def _await_answer(self) -> tuple[int, int]:
        """Waits for the answer file and reads two numbers out of it.

        Anything unreadable is reported back into the question rather than raised: the answerer is
        working blind against a file, and a crashed harness is a worse way to learn about a typo.
        """
        while True:
            if not self._answer.exists():
                time.sleep(self._poll)
                continue

            try:
                text = self._answer.read_text(encoding="utf-8").strip()
            except OSError:
                # Caught mid-write. It will be there a moment later.
                time.sleep(self._poll)
                continue

            self._answer.unlink(missing_ok=True)

            numbers = [word for word in text.replace(",", " ").split() if word.lstrip("-").isdigit()]

            if not numbers:
                self._pose(self._render(self._turn, complaint=f"Could not read a number from {text!r}."))
                continue

            if len(numbers) == 1:
                return 0, int(numbers[0])

            return int(numbers[0]), int(numbers[1])
