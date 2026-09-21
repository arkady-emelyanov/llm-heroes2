"""LangGraph agent that plays one side of a fheroes2 battle.

Each turn runs as a small graph rather than a single completion:

    assess ──> draft ──> validate ──┬──> (done)
                  ^                 │
                  └──── revise <────┘

* **assess** looks at the board and writes a short tactical note. It also carries a running
  memory of the battle forward, so the agent knows what it was trying to do three turns ago.
* **draft** proposes one action, schema-constrained so it cannot be malformed.
* **validate** checks the action against the `legal_actions` the game sent. This is a plain
  function, no model: the rules are known exactly, so there is no reason to ask.
* **revise** feeds the rejection back and loops, bounded by ``max_attempts``.

The point of validating locally is that an illegal move never reaches the game. The game has its
own rejection path, but every round-trip through it costs a turn of the retry budget and ends in
a skipped turn; catching it here is cheaper and keeps the model's mistake in its own context.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Literal, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from journal import Journal
from llm import ModelError, StreamingClient

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

# Everything that never changes goes in one block at the very front of every request. Servers that
# cache prompt prefixes - llama.cpp does - then re-use it instead of re-reading it each turn, and a
# compact notation defined once costs far less than spelling the same fields out on every line.
SYSTEM_RULES = """You are a tactical commander in a Heroes of Might and Magic II battle.

Positions are written r<row>c<column> on an 11-by-9 hex battlefield: r0c0 is the top-left corner,
r8c10 the bottom-right. You never need to work out which cells touch - every legal move, attack and
spell is listed for you each turn, and you answer with the number of the one you pick.

THE DAMAGE NUMBERS ARE GIVEN TO YOU. Every attack on the menu already says what it deals, how
many it kills, and what it costs you in return - worked out with the game's own formula. Do not
recompute them and do not estimate: read them off the menu and compare.

They are honest averages. They leave out luck and morale, which nobody can predict, so treat a
small difference between two options as a tie and a large one as real. What drives them, if you
need it for planning: damage rises 10% per point your attack exceeds their defense, and falls 5%
per point it falls short.

WHO ACTS WHEN (this decides what you must survive before your next turn):
- Within a round, the fastest stack on the field acts first, then the next fastest, and so on.
- Stacks of equal speed alternate between the two sides, the attacker's going first.
- So a stack slower than an enemy will be acted upon before it gets to act. Compare speeds before
  assuming you will strike first.

WHAT YOUR HERO'S NUMBERS MEAN:
- Attack and Defense are added to every creature in the army. The creature figures you are given
  already include them.
- Spell Power sets both the damage and the DURATION of your spells. Most spells that help or
  hinder a stack last one round per point of Spell Power; damage and healing spells act at once.
- Knowledge sets the maximum spell points: ten per point of Knowledge. They do not come back
  during a battle.

OTHER RULES THAT DECIDE FIGHTS:
- The defender retaliates once per round, and only if it survives. Kill a stack outright and it
  never retaliates - which is why concentrating on one stack usually beats spreading damage.
- That retaliation is spent by striking back, not by acting. A stack keeps it whether it attacks,
  moves or holds, and gets it back at the start of every round.
- YOUR SHOOTERS MUST NOT WALK. A stack takes one action per round, so a shooter that moves has
  shot nothing: it deals zero damage that round and arrives no safer. A shooter's range covers the
  whole battlefield - if the menu offers "shoot it from here", shooting is almost always the best
  line on the menu, and walking instead throws the round away. Move a shooter only to escape an
  adjacent enemy, and even then prefer shooting if the target is worth it.
- A ranged stack does HALF damage while an enemy is adjacent to it, and has a limited number of
  shots. So it is your MELEE stacks that should close on an enemy archer - never your own archers,
  who already reach it from where they stand.
- Once your melee stack is adjacent to an enemy, walking away does nothing but give up a free
  round: you take the same damage next round and deal none now. Stay and strike unless moving
  escapes something that would kill the stack outright.
- NEVER retreat and NEVER surrender. Both end the battle immediately in defeat and cost you your
  whole army, however many creatures are still standing. You fight until one side has nothing left;
  a losing round is not a lost battle.
- IF AN ENEMY IS ON YOU AND YOU CANNOT GET AWAY, ATTACK IT. Holding does keep your retaliation -
  a stack strikes back whether or not it acted - but the enemy attacks you either way, so a round
  spent holding is a round in which you deal only that retaliation and nothing of your own. Doing
  that repeatedly loses the battle one stack at a time, however poor each individual exchange looks.
  A bad exchange you cannot avoid is still better than no exchange.
- Declining an exchange is worth it only when the round buys something specific: a move that breaks
  contact and lets a shooter shoot again next round, or leaving the stack for a heavier stack of
  yours to handle. "The trade looks bad" on its own is not a reason - check what else the round
  could do first, and if the answer is nothing, strike.
- Good luck doubles a stack's damage for one attack; bad luck halves it. High morale can grant a
  second action immediately; low morale can freeze a stack for the round.
- A hero's spell has NO RANGE. It reaches any stack on the battlefield, however far away, and it
  does not care what is in between. Distance is never a reason not to cast: if a target appears on
  the spell list below, the spell can be cast on it this round, full stop. The only things that
  stop a cast are having no spell points, having already cast this round, or the rare Sphere of
  Negation.
- A hero casts at most one spell per round. Casting does NOT use up the acting stack's action, so
  a spell never costs you a move: if a spell is worth its points, take it and still act. You are
  asked for both at once, the spell's number and the stack action's number.
- Casting is optional, but hoarding is not thrift. Spell points do not come back and are worth
  nothing once the battle ends, so a point unspent at the last round was simply wasted. Cast
  whenever a spell changes the round at all; 0 is for when nothing on the list would.
- A spell that stops damage is usually worth more than one that deals it. Shield halves incoming
  ranged damage, Slow makes a fast enemy act after you instead of before, Bless raises every hit
  that stack lands for several rounds. Compare those against Magic Arrow's one-off damage before
  reaching for the damage spell.

HOW TO CHOOSE, AND WHEN TO STOP:
1. Name the single enemy stack that will hurt you most before you act again.
2. Read the menu. Each attack states its damage, its kills, and the retaliation it invites. An
   exchange that kills fewer of theirs than it loses of yours is a bad trade however tempting the
   target - and for a shooter forced into melee, almost every exchange is that trade.
3. Take the best of them. If two are close, take the one that wipes a stack out - a stack wiped
   out never retaliates and never acts again. If every attack looks like a losing trade, look for a
   move that breaks contact and take that; if no move does, attack anyway. Standing still while
   being attacked is the worst of the three.
4. Answer with two numbers, one from each menu:
   - spell_number: from the SPELL menu. 0 means cast nothing.
   - stack_action_number: from the STACK ACTION menu. That menu starts at 1 and never contains 0,
     so 0 here is always wrong - the stack has to do something.

Work through those four steps once, in order, and then answer. Do not restate the board, the
armies, the options or these rules - you have just been given all of it and none of it changes
while you think. Do not re-derive the damage figures; they are on the menu. If you find yourself
weighing the same two options a second time, stop and take the one the menu scores higher."""


def build_briefing(turn: dict) -> str:
    """Everything about this battle that will not change, written once.

    The rules, the coordinate convention and the full roster - who is on the field, what they are,
    what they can do - are fixed for the whole battle. Repeating them every turn is not just wasted
    tokens: a model that is handed the same facts again and again spends its thinking restating
    them. Said once and never again, they stay available without being re-read aloud.

    What is deliberately *not* here is anything that depletes: creature counts, positions, shots and
    spell points all change, and arrive as deltas instead.
    """
    side = turn["side"]
    other = "attacker" if side == "defender" else "defender"
    labels = stack_labels(turn)

    lines = [SYSTEM_RULES, "", "--- This battle ---", ""]

    for label, key in ((f"Your army (the {side})", side), (f"Enemy army (the {other})", other)):
        lines.append(f"{label}:")
        for stack in turn[f"{key}_army"]:
            # Everything a damage calculation needs, stated once: these are properties of the
            # creature type and do not change during the battle.
            traits = [f"atk{stack['attack']} def{stack['defense']} spd{stack['speed']}"]

            low, high = stack.get("damage_min"), stack.get("damage_max")
            if low is not None and high is not None:
                average = (low + high) / 2
                traits.append(f"damage {low}-{high} each (avg {average:g})")

            if stack.get("hit_points_each"):
                traits.append(f"{stack['hit_points_each']} hit points each")
            if stack.get("is_archer"):
                traits.append("ranged")
            if stack.get("is_flying"):
                traits.append("flies")
            if stack.get("is_wide"):
                traits.append("occupies two cells")

            lines.append(f"  {labels[stack['uid']]} - {', '.join(traits)}")

            # Abilities are properties of the creature type, so they are stated here once rather
            # than being left to be guessed at from a name.
            for ability in stack.get("abilities", []):
                lines.append(f"      {ability}")
        lines.append("")

    commander = turn.get(f"{side}_commander")

    if commander:
        name = commander.get("name") or "Your hero"
        primary = (f"attack {commander.get('attack', 0)}, defense {commander.get('defense', 0)}, "
                   f"power {commander.get('power', 0)}, knowledge {commander.get('knowledge', 0)}")
        lines.append(f"{name}: {primary}.")
        lines.append("A hero's attack and defense are added to every creature in their army.")

        for skill in commander.get("skills", []):
            lines.append(f"  {skill['name']} - {skill['description']}")

        lines.append("")

    if commander and commander.get("spells"):
        lines.append("Your hero knows these spells. What each does is fixed, so it is said once:")
        for spell in commander["spells"]:
            description = spell.get("description")
            summary = f" - {description}" if description else ""
            lines.append(f"  {spell['name']} ({spell['cost']} spell points){summary}")
        lines.append("")

    lines += [
        "Starting positions and strengths:",
    ]
    for key in (side, other):
        for stack in turn[f"{key}_army"]:
            lines.append(f"  {labels[stack['uid']]}: {stack['count']} strong at {position(stack['head'])}")

    lines += [
        "",
        "From here you will be told only what has changed since the previous turn, and which of your",
        "stacks is acting. Everything not mentioned is exactly as you last knew it.",
    ]

    return "\n".join(lines)


class Action(BaseModel):
    """One battle action, in the shape the protocol expects."""

    reasoning: str = Field(default="", description="Why this move.")
    action: Literal["move", "attack", "cast", "skip", "retreat", "surrender"]
    cell: int | None = Field(default=None, description="Destination cell, for action=move, or Teleport's destination.")
    target: int | None = Field(default=None, description="Stack uid, for action=attack or a targeted cast.")
    spell: int | None = Field(default=None, description="Spell id from legal_actions.cast, for action=cast only.")


class Choice(BaseModel):
    """What the model is actually asked for: one number from the menu it was shown.

    Asking for the action's fields directly invites a half-filled answer - an `attack` with no
    `target` is well-formed against the schema and useless in the game, and each such round trip
    costs a full model call. A number cannot be half-filled, and every number on the menu was built
    from the engine's own list of legal actions, so a valid pick is a legal move by construction.
    """

    # Two numbers, because a turn holds two independent decisions: the hero's spell does not use up
    # the stack's action, so choosing one must not cost the other.
    #
    # No prose field. The deliberation already streams separately as reasoning_content, so asking
    # for it here duplicates it, spends output budget the reasoning budget has made scarce, and -
    # observed - gets truncated mid-string, failing the whole answer over a field nothing reads.
    # The two fields are named apart rather than being 'spell' and 'choice'. Observed: the model
    # answered a bare {"choice": 0} five times in one battle - writing the spell menu's "0 means
    # cast nothing" into the stack's action field, where 0 is not a menu entry at all. Two fields
    # that both read as "the number" invite exactly that; names that each say which menu they index
    # do not.
    spell_number: int = Field(default=0, description="Number from the SPELL menu, or 0 to cast nothing this turn.")
    stack_action_number: int = Field(description="Number from the STACK ACTION menu. Never 0 - that menu starts at 1, and the stack must act.")


class Assessment(BaseModel):
    """The tactical read of the board, carried forward between turns.

    Both fields are kept short on purpose. They are re-read on every turn that reuses them, and a
    long 'situation' has been observed running past the output budget and arriving as unterminated
    JSON, which costs the whole assessment.
    """

    situation: str = Field(description="One sentence: who is winning and why.")
    # The plan outlives the turn that wrote it, so it has to be about the battle rather than about
    # one move. Observed: a plan reading "cast Magic Arrow on the Gargoyles, then attack them" was
    # replayed for seven turns after the Gargoyles were wiped out.
    plan: str = Field(
        description=(
            "One sentence naming which enemy stack to destroy first and which of your stacks does it. "
            "A standing aim for the next few rounds - NOT this turn's move, and no spell names."
        )
    )


def estimate_tokens(text: str) -> int:
    """Rough token count. Deliberately crude and deliberately an over-estimate.

    Getting a real count means a tokeniser round-trip per turn, which is not worth it when the
    number is only used to decide when to drop old turns. Erring high just trims sooner.
    """
    return len(text) // 3 + 1


class TurnState(TypedDict, total=False):
    """State of one turn's graph run."""

    turn: dict[str, Any]
    board: str
    menu: list
    spells: list
    assessment: Assessment
    action: Action
    chosen_number: int
    pending: dict | None
    rejection: str
    attempts: int
    max_attempts: int


def enumerate_spells(turn: dict) -> list[tuple[str, dict]]:
    """The spells the hero may cast this round, as (label, wire message) pairs.

    Kept apart from the stack's own actions because they are not alternatives: a hero's spell does
    not use up the acting stack's action, so a turn can hold both. Offering them on one list would
    make the model trade one against the other and give up a free move.
    """
    labels = stack_labels(turn)
    menu: list[tuple[str, dict]] = []

    for spell in turn["legal_actions"].get("cast", []):
        if not spell["needs_target"]:
            menu.append((f"cast {spell['name']} ({spell['cost']} sp) on the battlefield", {"action": "cast", "spell": spell["spell"]}))
            continue

        for uid in spell["targets"]:
            # Teleport also needs a destination, which this menu cannot express; it is left out
            # rather than offered as a choice that would be rejected.
            if spell["needs_destination"]:
                continue
            menu.append((f"cast {spell['name']} ({spell['cost']} sp) on the {labels.get(uid, 'stack ' + str(uid))}", {"action": "cast", "spell": spell["spell"], "target": uid}))

    return menu


def estimate_damage(attacker: dict, defender: dict) -> float:
    """Expected damage one stack deals another, by the engine's own formula.

    Mirrors Battle::Unit::CalculateDamageUnit: average base damage times the stack size, scaled by
    +10% per point of attack over defense (capped at +20 points) or -5% per point under (floored at
    -16). Luck, morale and the shooting distance penalty are left out - this is a comparison aid,
    not a prediction, and the things it omits apply about equally to the options being compared.
    """
    per_creature = (attacker["damage_min"] + attacker["damage_max"]) / 2
    base = per_creature * attacker["count"]

    difference = attacker["attack"] - defender["defense"]
    modifier = 1 + (0.10 * min(difference, 20) if difference > 0 else 0.05 * max(difference, -16))

    return base * modifier


def estimate_kills(damage: float, defender: dict) -> int:
    """How many creatures that damage removes.

    The top creature of a stack is the only wounded one, so it dies first and for less; every one
    behind it takes a full complement of hit points.
    """
    if damage < defender["hit_points_left"]:
        return 0

    remainder = damage - defender["hit_points_left"]

    return min(defender["count"], 1 + int(remainder // defender["hit_points_each"]))


def describe_exchange(attacker: dict, defender: dict, ranged: bool) -> str:
    """What an attack is worth, and what it costs, as a phrase for the menu.

    The model was spending its entire reasoning budget re-deriving these numbers and still getting
    them wrong - it guessed at the formula outright in one trace. The harness has every input the
    engine uses, so the arithmetic is done here and the model is left with the judgement.
    """
    damage = estimate_damage(attacker, defender)
    killed = estimate_kills(damage, defender)

    summary = f"~{damage:.0f} damage, kills ~{killed} of {defender['count']}"

    if killed >= defender["count"]:
        # Wiping a stack out is the one outcome worth naming rather than leaving to be inferred: it
        # is also the only way to take no retaliation at all.
        return summary + " - WIPES IT OUT, so no retaliation"

    if ranged:
        return summary + ", no retaliation (you are shooting)"

    # Melee only. The survivors strike back, and against a shooter forced into melee that return
    # blow is usually the larger half of the exchange.
    survivors = dict(defender, count=defender["count"] - killed)
    back = estimate_damage(survivors, attacker)

    return summary + f"; they retaliate for ~{back:.0f}, killing ~{estimate_kills(back, attacker)} of your {attacker['count']}"


def enumerate_unit_actions(turn: dict) -> list[tuple[str, dict]]:
    """What the acting stack itself may do. Exactly one of these happens each turn."""
    legal = turn["legal_actions"]
    labels = stack_labels(turn)
    menu: list[tuple[str, dict]] = []

    # Every stack on the field, so an attack option can be costed against the real target.
    stacks = {stack["uid"]: stack for side in ("attacker_army", "defender_army") for stack in turn[side]}
    acting = stacks.get(turn["unit"]["uid"], {})

    for target in legal["attack"]:
        how = "shoot it from here" if target["ranged"] else "walk up to it and strike"
        label = f"attack the {labels.get(target['target'], target['name'])} - {how}"

        defender = stacks.get(target["target"])
        if defender and acting:
            label += f": {describe_exchange(acting, defender, target['ranged'])}"

        menu.append((label, {"action": "attack", "target": target["target"]}))

    # A stack takes exactly one action, so a shooter that walks does not shoot. Said once in the
    # rules it was ignored: the Elves in one battle walked three turns running with 22 shots left
    # and an enemy in range, doing nothing at all. What a choice costs belongs on the choice.
    #
    # 'is_archer' lives on the army entry rather than the acting-stack summary.
    forfeits_shot = acting.get("is_archer") and turn["unit"].get("shots", 0) > 0 and any(target["ranged"] for target in legal["attack"])

    # The engine says which cells have no enemy beside them, and which of those no enemy can reach
    # before the stack acts again; without that the model derives hex adjacency by hand, cell by
    # cell, across the whole move list.
    #
    # An older game does not send them at all, and that is not the same as sending an empty list -
    # one means "unknown", the other "none of them". Labelling every cell "still within reach" on
    # the strength of a missing key would state as fact something nobody checked.
    safe_cells = legal.get("move_breaks_contact")
    breaks_contact = set(safe_cells) if safe_cells is not None else None

    unreachable_cells = legal.get("move_out_of_enemy_reach")
    out_of_reach = set(unreachable_cells) if unreachable_cells is not None else None

    in_contact = bool(legal["attack"]) and not any(target["ranged"] for target in legal["attack"])
    fleeing_archer = in_contact and acting.get("is_archer")

    for cell in legal["move"]:
        if forfeits_shot:
            cost = " - GIVES UP THIS ROUND'S SHOT, does no damage"
        elif breaks_contact is None:
            cost = ""
        elif out_of_reach is not None and cell in out_of_reach:
            # Only worth pointing out when there is contact to break. Said on every move of a stack
            # nobody is near, "out of reach of every enemy" reads as a recommendation to run away.
            cost = " - out of reach of every enemy" + (", so you can shoot again next round" if fleeing_archer else "")
        elif cell in breaks_contact:
            # Nothing is beside this cell now, but something can be there before the stack acts
            # again - which for a shooter is the whole question, since a stack that walks has
            # already given up its shot. Told only that the cell "breaks contact", a shooter reads
            # safety into it, walks, is caught anyway and fires nothing: the promise has to say
            # which of the two things it means.
            if out_of_reach is None:
                cost = " - nothing is beside it now"
            elif fleeing_archer:
                cost = " - nothing is beside it now, but an enemy can reach you there before you act again, so you would walk for nothing"
            else:
                cost = " - nothing is beside it now, but an enemy can reach you there before you act again"
        elif in_contact:
            cost = " - still within reach of an enemy"
        else:
            cost = ""

        menu.append((f"move to {position(cell)}{cost}", {"action": "move", "cell": cell}))

    if legal["can_skip"]:
        # Checked against the engine: Unit::GetDefense() never consults TR_SKIP, so waiting earns no
        # defensive bonus - unlike some games in the genre. It does keep the stack's retaliation,
        # which TR_RETALIATED shows is spent by striking back rather than by acting, so holding is
        # only a wasted round when some attack is actually worth taking. Saying "wasted" whenever an
        # attack merely exists pushed the model into exchanges the menu itself scored as losing.
        worth_taking = any(
            estimate_kills(estimate_damage(acting, stacks[target["target"]]), stacks[target["target"]]) > 0
            for target in legal["attack"]
            if acting and target["target"] in stacks
        )

        if not legal["attack"]:
            # Nothing in reach at all. Holding here is not patience, it is a round spent standing
            # still while the enemy closes on its own terms - so it must not read as the safe option.
            cost = " - nothing is in reach, so this round buys you nothing; close the distance instead"
        elif worth_taking:
            cost = " - no damage and no defensive bonus; a wasted round"
        else:
            cost = " - you keep your retaliation, but deal nothing of your own; worth it only if a move here would break contact"

        menu.append((f"hold position and do nothing{cost}", {"action": "skip"}))

    # Retreat and surrender end the battle in defeat, there and then. The old labels said only
    # "retreat from the battle", which reads like a repositioning move: with it on the menu the
    # model abandoned a battle it was winning 32 creatures to 11. What an option does belongs in
    # its label, and this is the one option that cannot be undone.
    if legal["can_retreat"]:
        menu.append(("retreat - ENDS THE BATTLE NOW IN DEFEAT and loses your whole army; never while you can still fight", {"action": "retreat"}))
    if legal["can_surrender"]:
        menu.append(("surrender - ENDS THE BATTLE NOW IN DEFEAT; never while you can still fight", {"action": "surrender"}))

    return menu


def enumerate_actions(turn: dict) -> list[tuple[str, dict]]:
    """Every legal action, spells included. Used where a single flat list is wanted."""
    return enumerate_spells(turn) + enumerate_unit_actions(turn)


def stack_labels(turn: dict, also: list | None = None) -> dict[int, str]:
    """A name for each stack, as the model should refer to it.

    Plain names, because that is what a person would say and what the model reads without effort.
    A bare number next to a plural noun does not survive contact: "#1 Centaurs" was read as "one
    Centaur", and every calculation after that was wrong. A number is only added when two stacks
    share a name and something has to tell them apart.
    """
    # 'also' carries stacks that are no longer on the field - a stack wiped out this turn still has
    # to be named, to say that it was wiped out.
    everything = [stack for side in ("attacker_army", "defender_army") for stack in turn[side]]
    known = {stack["uid"] for stack in everything}
    everything += [stack for stack in (also or []) if stack["uid"] not in known]

    names: dict[str, int] = {}
    for stack in everything:
        names[stack["name"]] = names.get(stack["name"], 0) + 1

    labels: dict[int, str] = {}
    seen: dict[str, int] = {}

    for stack in everything:
        name = stack["name"]
        if names[name] == 1:
            labels[stack["uid"]] = name
            continue

        seen[name] = seen.get(name, 0) + 1
        labels[stack["uid"]] = f"{name} (stack {seen[name]})"

    return labels


def position(cell: int) -> str:
    """A cell as r<row>c<column>. The model is never shown a raw index: it would have to convert
    one to reason about it, and that conversion is pure overhead it pays on every single call."""
    return f"r{cell // BOARD_WIDTH}c{cell % BOARD_WIDTH}"


def describe_stack(stack: dict, marker: str | None = None) -> str:
    """One stack, written so that nothing has to be explained first.

    An abbreviated notation is cheaper per line but needs a legend, and a legend is something the
    model re-derives out loud every time it thinks. Spelling the fields out costs a few tokens here
    and saves far more there.
    """
    bits = [f"{stack['count']}x {stack['name']}", position(stack["head"])]

    if marker is not None and marker != str(stack["uid"]):
        bits.append(f"drawn as {marker}")

    bits.append(f"atk{stack['attack']} def{stack['defense']} spd{stack['speed']}")

    if stack.get("is_archer"):
        bits.append(f"{stack['shots']} shots")
    if stack.get("is_flying"):
        bits.append("flying")
    if stack.get("is_wide"):
        bits.append("wide")
    if stack.get("is_current"):
        bits.append("<- acting now")

    return "  " + "  ".join(bits)


BOARD_WIDTH = 11
BOARD_HEIGHT = 9

# Markers for stacks beyond uid 9, so a crowded board still reads unambiguously.
_OVERFLOW_MARKERS = "abcdefghijklmnopqrstuvwxyz"


def stack_markers(turn: dict) -> dict[int, str]:
    """A one-character marker per stack, keyed by uid.

    A single-digit uid is its own marker, so the map needs no lookup to aim an action. Beyond that
    the digits run out and letters take over, and every stack line states its marker.
    """
    markers: dict[int, str] = {}
    spare = iter(_OVERFLOW_MARKERS)

    for side in ("attacker_army", "defender_army"):
        for stack in turn[side]:
            uid = stack["uid"]
            markers[uid] = str(uid) if 0 <= uid <= 9 else next(spare, "?")

    return markers


def render_map(turn: dict) -> str:
    """The battlefield as an offset-hex map.

    Even rows are indented half a cell, which is exactly what the engine's parity-dependent
    neighbour offsets describe: cells drawn touching are cells that touch in the game. That spares
    the model the arithmetic it is worst at, and it is the only part of the prompt that conveys
    shape rather than facts.
    """
    markers = stack_markers(turn)

    occupied: dict[int, str] = {}
    for side in ("attacker_army", "defender_army"):
        for stack in turn[side]:
            marker = markers[stack["uid"]]
            occupied[stack["head"]] = marker
            if stack.get("is_wide") and stack.get("tail") is not None:
                occupied[stack["tail"]] = marker

    reachable = set(turn["legal_actions"]["move"])

    # Cells sit two characters apart, so a one-character indent on even rows is exactly the half-cell
    # offset the hexagons have. Packing them tighter would be cheaper but would misrepresent that:
    # at one character per cell there is no half to offset by, and neighbours would read wrongly.
    lines = ["   c" + "".join(str(column % 10) for column in range(BOARD_WIDTH))]

    for row in range(BOARD_HEIGHT):
        cells = []
        for column in range(BOARD_WIDTH):
            index = row * BOARD_WIDTH + column
            cells.append(occupied.get(index, "+" if index in reachable else "."))

        indent = " " if row % 2 == 0 else ""
        lines.append(f"r{row} {indent}" + " ".join(cells))

    return "\n".join(lines)


def snapshot(turn: dict) -> dict:
    """The parts of a board worth diffing between turns."""
    stacks = {}

    for side in ("attacker_army", "defender_army"):
        for stack in turn[side]:
            stacks[stack["uid"]] = dict(stack)

    return stacks


def render_delta(turn: dict, previous: dict, *, positions_shown: bool = False) -> str:
    """What changed since the board the model was last shown.

    Only differences are sent, so a turn costs a handful of tokens instead of a whole board. The
    lines reference stacks by uid against the last full board, which the model still has in its
    context - which is also why a full board has to be re-sent whenever that context is trimmed.
    """
    current = snapshot(turn)
    labels = stack_labels(turn, list(previous.values()))
    lines = []

    for uid, stack in current.items():
        before = previous.get(uid)

        if before is None:
            lines.append(f"  new: {describe_stack(stack).strip()}")
            continue

        if stack["count"] != before["count"]:
            lines.append(f"  {labels[uid]}: {before['count']} -> {stack['count']}")
        # A move is only worth a line when there is no map; otherwise it is already on screen.
        if not positions_shown and stack["head"] != before["head"]:
            lines.append(f"  {labels[uid]} moved {position(before['head'])} -> {position(stack['head'])}")
        if stack.get("shots") != before.get("shots"):
            lines.append(f"  {labels[uid]}: shots {before.get('shots')} -> {stack.get('shots')}")

    for uid in previous:
        if uid not in current:
            lines.append(f"  {labels[uid]}: wiped out")

    return "\n".join(lines) if lines else "  (nothing changed)"


def render_legal_actions(turn: dict) -> str:
    """The numbered menus the model chooses from.

    Never abbreviated: this is the one part of the prompt where saving tokens would cost legality.
    A melee attack says it walks up first, because the list already accounts for movement and
    without saying so it contradicts the map - and a model that can see the contradiction will
    invent a rule to explain it rather than doubt the list.
    """
    lines = []

    spells = enumerate_spells(turn)
    if spells:
        lines.append("SPELL MENU - answer with spell_number. Your hero casts one of these as well as,")
        lines.append("not instead of, the stack's action.")
        # 0 is listed as an entry of its own rather than described in prose. It is the one number
        # the model reaches for without being told, and a 0 that appears on a menu is far harder to
        # mistake for an entry on the other menu.
        lines.append("  0. cast nothing this round")
        for number, (label, _) in enumerate(spells, start=1):
            lines.append(f"  {number}. {label}")
        lines.append("")

    lines.append("STACK ACTION MENU - answer with stack_action_number. The acting stack does exactly")
    lines.append("one of these, and it always does one: this menu has no 0.")
    for number, (label, _) in enumerate(enumerate_unit_actions(turn), start=1):
        lines.append(f"  {number}. {label}")

    return "\n".join(lines)


def render_board(turn: dict, *, full: bool = True, previous: dict | None = None, with_map: bool = False) -> str:
    """The per-turn message: a whole board, or only what changed since the last one.

    The map, when drawn, is sent every turn rather than diffed: it *is* the positions, and redrawing
    it costs about what a handful of move lines would.
    """
    side = turn["side"]
    other = "attacker" if side == "defender" else "defender"
    unit = turn["unit"]
    markers = stack_markers(turn)

    lines = [f"Turn {turn['turn']}. You play the {side}."]

    if with_map:
        lines += ["", render_map(turn)]

    if full or previous is None:
        # A full restatement, used only to re-anchor: the first turn works off the briefing, and
        # after that this appears on a refresh or when the conversation has been trimmed.
        lines += ["", "Where everything stands:"]
        lines += [describe_stack(s, markers.get(s["uid"])) for s in turn[f"{side}_army"]]
        lines += [describe_stack(s, markers.get(s["uid"])) for s in turn[f"{other}_army"]]
    else:
        lines += ["", "Since your last turn:"]
        lines.append(render_delta(turn, previous, positions_shown=with_map))

    commander = turn.get(f"{side}_commander")
    if commander:
        lines += ["", f"Hero: {commander['spell_points']} spell points left."]

    # Identity only: its strength and position are already known from the briefing and the deltas.
    lines += ["", f"Acting now: your {stack_labels(turn).get(unit['uid'], unit['name'])}", ""]
    lines.append(render_legal_actions(turn))

    return "\n".join(lines)


def validate_action(action: Action, turn: dict) -> str:
    """Returns an empty string if the action is legal, or the reason it is not."""
    legal = turn["legal_actions"]

    if action.action == "move":
        if action.cell is None:
            return "action 'move' needs a cell."
        if action.cell not in legal["move"]:
            return f"cell {action.cell} is not reachable this turn. Reachable cells: {legal['move']}."
        return ""

    if action.action == "attack":
        if action.target is None:
            return "action 'attack' needs a target uid."

        targets = [t["target"] for t in legal["attack"]]
        if action.target not in targets:
            return f"uid {action.target} cannot be attacked this turn. Attackable uids: {targets}."
        return ""

    if action.action == "cast":
        if action.spell is None:
            return "action 'cast' needs a spell id."

        castable = {s["spell"]: s for s in legal.get("cast", [])}
        spell = castable.get(action.spell)

        if spell is None:
            return f"spell {action.spell} cannot be cast this turn. Castable spell ids: {sorted(castable)}."

        if not spell["needs_target"]:
            return ""

        if action.target is None:
            return f"casting {spell['name']} needs a target uid, one of {spell['targets']}."
        if action.target not in spell["targets"]:
            return f"{spell['name']} cannot be aimed at uid {action.target}. Legal targets: {spell['targets']}."
        if spell["needs_destination"] and action.cell is None:
            return f"casting {spell['name']} also needs a destination cell."

        return ""

    if action.action == "skip":
        return "" if legal["can_skip"] else "this stack cannot skip."
    if action.action == "retreat":
        return "" if legal["can_retreat"] else "this side cannot retreat."
    if action.action == "surrender":
        return "" if legal["can_surrender"] else "this side cannot surrender."

    return f"unknown action '{action.action}'."


class BattleAgent:
    """Plays one side of one battle, keeping its notes across turns."""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str | None,
        temperature: float = 0.2,
        max_attempts: int = 3,
        max_tokens: int = 4096,
        context_window: int = 100_000,
        assess_every: int = 4,
        refresh_every: int = 10,
        # Off by default. The map is verified to match the engine's hex adjacency, but it is a
        # change to how the model is asked to reason, and the cheapest default is the one whose
        # behaviour is already measured. Turn it on per scenario with "board_map": true.
        board_map: bool = False,
        reasoning_effort: str | None = None,
        show_reasoning: bool = True,
        journal: Journal | None = None,
        verbose: bool = False,
    ) -> None:
        self._llm = StreamingClient(
            base_url=base_url, model=model, api_key=api_key, max_tokens=max_tokens, temperature=temperature, reasoning_effort=reasoning_effort
        )

        self._max_attempts = max_attempts
        self._assess_every = max(1, assess_every)
        # How often a whole board is re-sent instead of a delta. Deltas reference the last full
        # board, so periodically restating it stops small mistakes from compounding silently.
        self._refresh_every = max(1, refresh_every)
        self._board_map = board_map
        self._verbose = verbose
        self._show_reasoning = show_reasoning
        self._journal = journal or Journal(None)

        # Budget for everything we send. The reply has to fit in the same window, and a reasoning
        # model spends most of its output budget thinking, so the reservation is generous.
        self._prompt_budget = max(2_000, context_window - max_tokens - 2_000)

        # Carried between turns, which is why these live on the agent and not in the graph state.
        self._assessment: Assessment | None = None

        # The set of stacks the current plan was written against; a change forces a re-assessment.
        self._last_roster: set[int] | None = None
        self._dropped_turns = 0
        self._turns_seen = 0

        # The turn now being played, so a rejection can be journalled with the board that caused it.
        self._current_turn: dict[str, Any] | None = None

        # An action already decided for a stack that cast a spell first. Casting does not use up
        # the stack's action, so the game asks again for the same stack in the same turn; the plan
        # made alongside the spell is carried out then, without spending another model call on it.
        self._pending: dict | None = None

        # The board the model was last shown in full, which every delta since is relative to.
        self._last_shown: dict | None = None
        self._turns_since_refresh = 0

        # The running conversation. A delta only means anything if the board it is relative to is
        # still in front of the model, so turns accumulate here instead of each call starting over.
        # It is also the cheapest shape for a server that caches prompt prefixes: a turn appends,
        # so everything before it is already processed.
        self._messages: list[dict] = []

        self._graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(TurnState)

        graph.add_node("assess", self._assess)
        graph.add_node("draft", self._draft)
        graph.add_node("validate", self._validate)

        graph.set_entry_point("assess")
        graph.add_edge("assess", "draft")
        graph.add_edge("draft", "validate")
        graph.add_conditional_edges("validate", self._route_after_validate, {"revise": "draft", "done": END})

        return graph.compile()

    def _ask(self, messages: list[dict], schema: type[BaseModel], label: str, reasoning_effort: str | None = None) -> BaseModel:
        """One schema-constrained call, streamed so the model's thinking can be watched live."""
        started = time.monotonic()
        state = {"started": False}

        def show(fragment: str) -> None:
            if not self._show_reasoning:
                return

            if not state["started"]:
                print(f"\n  {DIM}[{label} is thinking]{RESET} ", end="", flush=True)
                state["started"] = True

            # Dim, so the thinking is clearly separate from the decision it leads to.
            print(f"{DIM}{fragment}{RESET}", end="", flush=True)

        parsed, reasoning_chars = self._llm.complete_json(
            messages, schema.model_json_schema(), schema.__name__.lower(), on_reasoning=show, reasoning_effort=reasoning_effort
        )

        if state["started"]:
            print(flush=True)

        elapsed = time.monotonic() - started

        if self._verbose:
            print(f"  {DIM}[{label} took {elapsed:.0f}s, {reasoning_chars} chars of reasoning]{RESET}", flush=True)

        try:
            return schema.model_validate(parsed)
        except Exception as exc:
            raise ModelError(f"the reply did not match the {schema.__name__} schema: {exc}") from exc

    def _begin(self, turn: dict) -> None:
        """Opens the conversation with the briefing, the first time a turn is seen."""
        if self._messages:
            return

        self._messages.append({"role": "system", "content": build_briefing(turn)})

        # The briefing states the starting positions and strengths, so the first turn is already a
        # delta against it rather than a board in its own right.
        self._last_shown = snapshot(turn)
        self._turns_since_refresh = 0

    def _board_message(self, turn: dict) -> str:
        """What to add to the conversation this turn: a whole board, or only what changed.

        A whole board is sent when there is nothing to be relative to - the first turn, or after the
        conversation has been trimmed - and periodically so that small errors cannot accumulate
        unnoticed. Otherwise the turn is purely additive: what the opponent did, what it cost, and
        the fresh list of options.
        """
        full = self._last_shown is None or self._turns_since_refresh >= self._refresh_every

        message = render_board(turn, full=full, previous=self._last_shown, with_map=self._board_map)

        # The delta is relative to the board most recently *shown*, not the previous turn, so a
        # trimmed conversation cannot leave a delta pointing at something the model can no longer see.
        self._last_shown = snapshot(turn)
        self._turns_since_refresh = 0 if full else self._turns_since_refresh + 1

        return message

    def _trim_conversation(self) -> None:
        """Drops the oldest turns when the conversation outgrows the context budget.

        Turns are dropped in user/assistant pairs so the thread stays well-formed. Whatever is
        dropped takes its board with it, so the next turn has to restate one in full - otherwise a
        delta would be relative to something no longer on screen.
        """
        while estimate_tokens("".join(m["content"] for m in self._messages)) > self._prompt_budget and len(self._messages) > 3:
            del self._messages[1:3]

            self._last_shown = None
            self._dropped_turns += 1

            if self._verbose:
                print(f"  {DIM}[context: dropped an older turn; the next board will be sent in full]{RESET}", flush=True)

    def _assess(self, state: TurnState) -> TurnState:
        # A stack being wiped out is the one change that can invalidate a plan outright rather than
        # gradually - a plan that names a stack which no longer exists is worse than no plan, since
        # it is re-read as guidance every turn it is reused. So the roster is what forces a refresh,
        # independently of the turn count.
        roster = {stack["uid"] for side in ("attacker_army", "defender_army") for stack in state["turn"][side]}
        roster_changed = self._last_roster is not None and roster != self._last_roster
        self._last_roster = roster

        # Re-assessing costs a full model call, which on a local reasoning model is the dominant
        # expense of a turn. Otherwise the board changes gradually, so the plan is refreshed
        # periodically rather than every turn, and reused in between.
        if self._assessment is not None and not roster_changed and self._turns_seen % self._assess_every != 0:
            if self._verbose:
                print(f"  assess: reusing plan - {self._assessment.plan}", flush=True)
            return {"assessment": self._assessment}

        board = state["board"]
        try:
            assessment = self._ask(
                self._messages
                + [
                    {
                        "role": "user",
                        "content": f"{board}\n\nIn one sentence each, say how the battle stands and what you are aiming for. Answer straight away - do not deliberate, the decision comes next.",
                    }
                ],
                Assessment,
                "assess",
                # The assessment shares one output budget with its own thinking, and a long think
                # leaves no room for the answer: observed three times as truncated JSON, which
                # loses the whole assessment. It is a one-sentence read of the board, so it is asked
                # for with minimal deliberation and the deliberation saved for the decision.
                reasoning_effort="low",
            )
        except ModelError as exc:
            # The assessment is a convenience, not a requirement - the decision is made from the
            # board and the menu either way. Losing it should not cost the turn, let alone the
            # battle, so the previous plan is carried on with.
            print(f"  {DIM}assessment skipped: {exc}{RESET}", flush=True)

            self._journal.write("assess_failed", turn=state["turn"]["turn"], reason=str(exc))

            return {"assessment": self._assessment or Assessment(situation="unknown", plan="press the attack")}

        print(f"  {BOLD}assess:{RESET} {assessment.situation}\n  {BOLD}plan:{RESET} {assessment.plan}", flush=True)

        self._assessment = assessment

        return {"assessment": assessment}

    def _draft(self, state: TurnState) -> TurnState:
        board = state["board"]
        assessment = state["assessment"]

        # The turn is appended to the running conversation rather than replacing it, so the board
        # this delta refers to is still on screen.
        messages = self._messages + [{"role": "user", "content": board}]

        # On a retry, show the model its own rejected pick so it corrects rather than repeats.
        rejection = state.get("rejection")
        if rejection:
            messages.append({"role": "assistant", "content": json.dumps({"stack_action_number": state["action"].__dict__.get("_out_of_range", 0)})})
            messages.append({"role": "user", "content": f"{rejection} Choose again."})

        menu = state["menu"]
        spells = state["spells"]

        try:
            choice = self._ask(messages, Choice, "decide")
            chosen = choice.stack_action_number
            spell_number = choice.spell_number
        except ModelError as exc:
            # Usually the model thinking until its output budget ran out without ever answering.
            # That is a failed attempt at this turn, not a failed battle: it goes round the revise
            # loop like any other unusable answer, and the bounded retries end in a skip rather
            # than abandoning the fight.
            print(f"  {DIM}no answer: {exc}{RESET}", flush=True)

            action = Action(reasoning="no answer", action="skip")
            action.__dict__["_no_answer"] = str(exc)

            return {"action": action, "chosen_number": 0, "attempts": state.get("attempts", 0) + 1}

        if 1 <= chosen <= len(menu):
            action = Action(reasoning=menu[chosen - 1][0], **menu[chosen - 1][1])

            # A spell is cast in the same turn as the stack acts, so both are kept: the spell goes
            # to the game now and the stack's action is held for when the game asks again.
            if spells and 1 <= spell_number <= len(spells):
                held = action
                action = Action(reasoning=spells[spell_number - 1][0], **spells[spell_number - 1][1])

                return {
                    "action": action,
                    "pending": {"wire": menu[chosen - 1][1], "label": menu[chosen - 1][0], "turn": state["turn"]["turn"], "uid": state["turn"]["unit"]["uid"]},
                    "chosen_number": chosen,
                    "attempts": state.get("attempts", 0) + 1,
                }
        else:
            # Out of range is the only way a number can be wrong, and it is worth reporting in the
            # model's own terms rather than as a schema error.
            action = Action(reasoning="out of range", action="skip")
            action.__dict__["_out_of_range"] = choice.stack_action_number

        if action.action == "move":
            detail = f" cell {action.cell}"
        elif action.action == "attack":
            detail = f" uid {action.target}"
        elif action.action == "cast":
            detail = f" spell {action.spell}" + (f" at uid {action.target}" if action.target is not None else "")
        else:
            detail = ""
        print(f"  {BOLD}decision:{RESET} {action.action}{detail} - {action.reasoning}", flush=True)

        return {"action": action, "pending": None, "chosen_number": chosen, "attempts": state.get("attempts", 0) + 1}

    def _validate(self, state: TurnState) -> TurnState:
        action = state["action"]
        no_answer = action.__dict__.get("_no_answer")
        out_of_range = action.__dict__.get("_out_of_range")

        if no_answer is not None:
            rejection = "You spent the whole thinking budget without answering. Answer immediately with just the number of your chosen option."
        elif out_of_range == 0:
            # Almost always the spell menu's "0 means cast nothing" written into the wrong field,
            # so say that rather than repeating the generic range - a model told only "0 is not on
            # the menu" tends to answer 0 again.
            rejection = (
                "0 belongs to spell_number, not stack_action_number. Casting nothing is spell_number 0; "
                f"the stack must still act, so stack_action_number has to be between 1 and {len(state['menu'])}."
            )
        elif out_of_range is not None:
            rejection = f"{out_of_range} is not on the menu; pick a number between 1 and {len(state['menu'])}."
        else:
            rejection = validate_action(action, state["turn"])

        if rejection:
            print(f"  {DIM}caught before sending: {rejection}{RESET}", flush=True)

            # Journalled even though the game never sees it: a move the model believed in but the
            # rules forbid is exactly the kind of misunderstanding a better prompt should fix.
            self._journal.write(
                "local_rejection",
                turn=state["turn"]["turn"],
                attempt=state.get("attempts", 0),
                reason=rejection,
                action=action.model_dump(),
                board=render_board(state["turn"], full=True, with_map=self._board_map),
            )

        return {"rejection": rejection}

    def _route_after_validate(self, state: TurnState) -> str:
        if not state["rejection"]:
            return "done"

        # Out of attempts: let the action through anyway. The game will reject it and say so, which
        # is the same answer arrived at more slowly, and it keeps one authority on the rules.
        if state["attempts"] >= state["max_attempts"]:
            if self._verbose:
                print(f"  validate: out of attempts, sending anyway", flush=True)
            return "done"

        return "revise"

    def decide(self, turn: dict) -> dict:
        """Runs one turn of the graph and returns the wire-format action."""
        self._current_turn = turn
        self._begin(turn)

        # The stack that just cast is being asked for its own action now.
        held = self._pending
        self._pending = None

        if held is not None and held["turn"] == turn["turn"] and held["uid"] == turn["unit"]["uid"]:
            # The spell may have changed what is possible, so the plan is only carried out if it is
            # still on offer; otherwise it falls through and the model decides afresh.
            if any(wire == held["wire"] for _, wire in enumerate_unit_actions(turn)):
                print(f"  {BOLD}as planned:{RESET} {held['label']}", flush=True)
                return held["wire"]

            print(f"  {DIM}the planned action is no longer available; deciding again{RESET}", flush=True)

        self._trim_conversation()

        result = self._graph.invoke(
            {
                "turn": turn,
                "board": self._board_message(turn),
                "menu": enumerate_unit_actions(turn),
                "spells": enumerate_spells(turn),
                "attempts": 0,
                "max_attempts": self._max_attempts,
            }
        )

        self._turns_seen += 1

        # Keep the turn and the answer, so the next turn can be a delta against them.
        self._messages.append({"role": "user", "content": result["board"]})
        self._messages.append({"role": "assistant", "content": json.dumps({"stack_action_number": result["chosen_number"]})})

        self._pending = result.get("pending")

        action = result["action"]

        message: dict[str, Any] = {"action": action.action}
        if action.action == "move":
            message["cell"] = action.cell
        elif action.action == "attack":
            message["target"] = action.target
        elif action.action == "cast":
            message["spell"] = action.spell
            if action.target is not None:
                message["target"] = action.target
            if action.cell is not None:
                message["cell"] = action.cell

        return message

    def on_game_rejection(self, reason: str) -> dict:
        """The game rejected an action that our own validation had passed.

        This is the most interesting failure the harness can see: the engine and our understanding
        of the rules disagree, which means either the board description or the legal-action list is
        missing something the model needed. It is journalled with the board that produced it so the
        prompt can be revised against real cases rather than guesses.
        """
        print(f"  \033[0;33mthe game rejected that: {reason}\033[0m", flush=True)

        self._journal.write(
            "game_rejection",
            turn=None if self._current_turn is None else self._current_turn["turn"],
            reason=reason,
            board=None if self._current_turn is None else render_board(self._current_turn, full=True, with_map=self._board_map),
            legal_actions=None if self._current_turn is None else self._current_turn["legal_actions"],
        )

        # Carried into the conversation so the next turn is aware of it.
        self._messages.append({"role": "user", "content": f"The game rejected that action: {reason}. Do not repeat it."})

        return {"action": "skip"}



