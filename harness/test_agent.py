"""Runs the real LangGraph graph with a scripted fake model, so the wiring is
exercised (assess -> draft -> validate -> revise -> done) without any API calls."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json

import agent
from agent import Action, Assessment, BattleAgent, Choice
from llm import ModelError
from journal import Journal

TURN = {
 "turn":1,"side":"defender","color":"red",
 "unit":{"uid":7,"name":"Archer","count":40,"head":55,"speed":4,"shots":12},
 "attacker_army":[{"uid":3,"name":"Peasant","color":"blue","count":100,"head":10,
   "hit_points":100,"hit_points_left":1,"attack":1,"defense":1,"speed":2,
   "damage_min":1,"damage_max":1,"hit_points_each":1,
   "is_wide":False,"is_flying":False,"is_archer":False,"shots":0,"is_current":False}],
 "defender_army":[{"uid":7,"name":"Archer","color":"red","count":40,"head":55,
   "hit_points":160,"hit_points_left":4,"attack":5,"defense":3,"speed":4,
   "damage_min":2,"damage_max":3,"hit_points_each":15,
   "is_wide":False,"is_flying":False,"is_archer":True,"shots":12,"is_current":True}],
 "defender_commander":{"spell_points":10,"max_spell_points":10,
   "spells":[{"id":15,"name":"Bless","cost":3},{"id":24,"name":"Armageddon","cost":15}]},
 "legal_actions":{"move":[12,13,14],"attack":[{"target":3,"name":"Peasant","ranged":True}],
   "cast":[{"spell":15,"name":"Bless","cost":3,"needs_target":True,"needs_destination":False,"targets":[7]},
           {"spell":24,"name":"Armageddon","cost":15,"needs_target":False,"needs_destination":False,"targets":[]}],
   "can_skip":True,"can_retreat":False,"can_surrender":False}}

class Scripted:
    """Stands in for the model: hands back prepared answers and counts the calls."""

    def __init__(self, assessments, actions):
        self.assessments = list(assessments)
        self.actions = list(actions)
        self.assess_calls = 0
        self.action_calls = 0
        self.last_assessment = Assessment(situation="unchanged", plan="unchanged")

    def __call__(self, messages, schema, label, reasoning_effort=None):
        if schema is Assessment:
            self.assess_calls += 1
            # Reuse the last one when the script runs dry: tests care about the decisions.
            nxt = self.assessments.pop(0) if self.assessments else self.last_assessment
            if isinstance(nxt, Exception):
                raise nxt
            self.last_assessment = nxt
            return nxt

        self.action_calls += 1
        picked = self.actions.pop(0)

        # A scripted exception stands in for the model running out of budget.
        if isinstance(picked, Exception):
            raise picked

        # Tests name a menu number, which is what the model now answers with.
        if isinstance(picked, Choice):
            return picked
        # A tuple is (spell number, action number); a bare number casts nothing.
        if isinstance(picked, tuple):
            return Choice(spell_number=picked[0], stack_action_number=picked[1])
        return Choice(stack_action_number=picked)


def build(assessments, actions, assess_every=1):
    a = BattleAgent.__new__(BattleAgent)
    a._ask = Scripted(assessments, actions)
    a._max_attempts = 3
    a._assess_every = assess_every
    a._verbose = False
    a._show_reasoning = False
    a._assessment = None
    a._last_roster = None
    a._dropped_turns = 0
    a._turns_seen = 0
    a._prompt_budget = 100000
    a._refresh_every = 10
    a._last_shown = None
    a._turns_since_refresh = 0
    a._board_map = False
    a._current_turn = None
    a._journal = Journal(None)     # counts things, writes nowhere
    a._messages = []
    a._pending = None
    a._graph = a._build_graph()
    return a

print("=== case 0: the rule checks, one per rule ===")
A = Action
rules = [
    (A(reasoning="r", action="attack", target=3), ""),
    (A(reasoning="r", action="move", cell=13), ""),
    (A(reasoning="r", action="skip"), ""),
    (A(reasoning="r", action="move", cell=40), "not reachable"),
    (A(reasoning="r", action="move"), "needs a cell"),
    (A(reasoning="r", action="attack", target=99), "cannot be attacked"),
    (A(reasoning="r", action="attack"), "needs a target"),
    (A(reasoning="r", action="retreat"), "cannot retreat"),
    (A(reasoning="r", action="surrender"), "cannot surrender"),
    (A(reasoning="r", action="cast", spell=15, target=7), ""),
    (A(reasoning="r", action="cast", spell=24), ""),
    (A(reasoning="r", action="cast"), "needs a spell id"),
    (A(reasoning="r", action="cast", spell=99), "cannot be cast this turn"),
    (A(reasoning="r", action="cast", spell=15), "needs a target uid"),
    (A(reasoning="r", action="cast", spell=15, target=3), "cannot be aimed at uid 3"),
]
for candidate, expected in rules:
    got = agent.validate_action(candidate, TURN)
    ok = (got == "" and expected == "") or (bool(expected) and expected in got)
    assert ok, f"{candidate.action}/{candidate.spell}: expected {expected!r}, got {got!r}"
    print(f"  PASS  {candidate.action:<10} -> {got or '(legal)'}")

# The menu order is: attacks, then casts, then moves, then skip/retreat/surrender.
MENU = agent.enumerate_unit_actions(TURN)
SPELLS = agent.enumerate_spells(TURN)
print("\n=== the two menus the model is offered ===")
print("  spells (optional, do not use the stack's action):")
for i, (label, wire) in enumerate(SPELLS, 1):
    print(f"    {i}. {label}  ->  {json.dumps(wire)}")
print("  stack actions (exactly one happens):")
for i, (label, wire) in enumerate(MENU, 1):
    print(f"    {i}. {label}  ->  {json.dumps(wire)}")
ATTACK, MOVE_12, MOVE_13 = 1, 2, 3
SKIP = len(MENU)
BLESS, ARMAGEDDON = 1, 2

print("\n=== case 1: first draft is legal ===")
a = build([Assessment(situation="winning", plan="shoot")], [ATTACK])
out = a.decide(TURN)
print("wire action:", out)
assert out == {"action":"attack","target":3}, out
assert a._ask.action_calls == 1, a._ask.action_calls
print("conversation after 1 turn:", [m["role"] for m in a._messages])
assert [m["role"] for m in a._messages] == ["system", "user", "assistant"], a._messages

print("\n=== case 2: a number off the end of the menu is caught and revised ===")
a = build([Assessment(situation="even", plan="reposition")], [99, MOVE_13])
out = a.decide(TURN)
print("wire action:", out)
assert out == {"action":"move","cell":13}, out
assert a._ask.action_calls == 2, a._ask.action_calls
print("-> illegal move never reached the game")

print("\n=== case 3: keeps failing, sends anyway after max_attempts ===")
a = build([Assessment(situation="confused", plan="flail")], [99, 98, 97])
out = a.decide(TURN)
print("wire action:", out)
assert a._ask.action_calls == 3, a._ask.action_calls
print("-> bounded at max_attempts, game becomes the authority")

print("\n=== case 4: game rejection folds into notes ===")
a = build([Assessment(situation="s", plan="p")], [SKIP])
a.decide(TURN)
r = a.on_game_rejection("cell 13 is occupied")
print("reply:", r)
assert r == {"action":"skip"}
assert any("cell 13 is occupied" in m["content"] for m in a._messages)
print("rejection recorded in the conversation")

print("\n=== case 4b: a cast reaches the wire in the right shape ===")
a = build([Assessment(situation="s", plan="p")], [(BLESS, SKIP)])
out = a.decide(TURN)
print("wire action:", out)
assert out == {"action":"cast","spell":15,"target":7}, out
a = build([Assessment(situation="s", plan="p")], [(ARMAGEDDON, SKIP)])
out = a.decide(TURN)
print("global cast:", out)
assert out == {"action":"cast","spell":24}, out
print("-> targeted and untargeted casts both serialise correctly")

print("\n=== case 4c: the briefing is frozen, turns are deltas ===")
a = build([Assessment(situation="s", plan="p")], [SKIP])
a._begin(TURN)
briefing = a._messages[0]["content"]
# Static facts belong in the briefing and must never be repeated.
for expected in ("Archer", "atk5", "ranged", "Bless (3 spell points)", "Archer: 40 strong at r5c0",
                 "damage 2-3 each", "15 hit points each"):
    assert expected in briefing, f"briefing is missing {expected!r}"
print("  briefing carries rules, roster, base stats, spell book and starting positions")

# A bare number beside a plural noun reads as a count: "#1 Centaurs" was taken to mean one Centaur.
assert "#" not in briefing, "stack identifiers must not look like quantities"
duplicates = {"turn":1,"side":"defender","unit":{"uid":1,"name":"Archer","count":5,"head":0},
              "attacker_army":[dict(TURN["defender_army"][0], uid=1, name="Archer"),
                               dict(TURN["defender_army"][0], uid=2, name="Archer")],
              "defender_army":[dict(TURN["defender_army"][0], uid=3, name="Ogre")],
              "legal_actions":{"move":[],"attack":[],"cast":[],"can_skip":True,"can_retreat":False,"can_surrender":False}}
labels = agent.stack_labels(duplicates)
assert labels[3] == "Ogre", labels
assert labels[1] == "Archer (stack 1)" and labels[2] == "Archer (stack 2)", labels
print("  same-named stacks are numbered only when they must be:", labels[1], "/", labels[3])

moved = json.loads(json.dumps(TURN))
moved["defender_army"][0]["count"] = 31
moved["defender_army"][0]["head"] = 44
first = a._board_message(moved)
assert "Since your last turn" in first, first
assert "atk5" not in first, "base stats are static and must not be repeated per turn"
assert "Bless (3 spell points)" not in first, "the spell book is static and must not be repeated"
print("  turn message:", " / ".join(l.strip() for l in first.splitlines() if "->" in l or "moved" in l))
assert "Archer: 40 -> 31" in first and "moved" in first

a._turns_since_refresh = 99
assert "Where everything stands" in a._board_message(moved), "a refresh should re-anchor"
print("  a refresh re-anchors without repeating static facts")

b = build([Assessment(situation="s", plan="p")], [SKIP])
b._board_map = True
b._begin(TURN)
assert "moved" not in b._board_message(moved), "with a map, moves are read off it"
print("  with a map the move is not also spelled out")

print("\n=== case 4c2: a stack wiped out this turn can still be named ===")
gone = json.loads(json.dumps(TURN))
gone["attacker_army"] = []          # the Peasants were destroyed
delta = agent.render_delta(gone, agent.snapshot(TURN))
print("  delta:", delta.strip())
assert "Peasant: wiped out" in delta, delta
# It is no longer on the field, so its label has to come from what was there before.
labels = agent.stack_labels(gone, list(agent.snapshot(TURN).values()))
assert labels[3] == "Peasant", labels
print("-> a dead stack keeps its name for the turn that reports it")

print("\n=== case 4d: the map matches the engine's hex adjacency ===")
W, H = agent.BOARD_WIDTH, agent.BOARD_HEIGHT
def neighbours(i):
    x, y = i % W, i // W; odd = y % 2; out = {}
    if not (y == 0 or (x == 0 and odd)):           out["TL"] = i - (W + 1 if odd else W)
    if not (y == 0 or (x == W - 1 and not odd)):   out["TR"] = i - (W if odd else W - 1)
    if x != 0:                                     out["L"] = i - 1
    if x != W - 1:                                 out["R"] = i + 1
    if not (y == H-1 or (x == 0 and odd)):         out["BL"] = i + (W - 1 if odd else W)
    if not (y == H-1 or (x == W-1 and not odd)):   out["BR"] = i + (W if odd else W + 1)
    return out
blank = {"turn":1,"side":"defender","attacker_army":[],"defender_army":[],"legal_actions":{"move":[]}}
drawn = agent.render_map(blank)
column = {}
for line in drawn.splitlines()[1:]:
    # Rows are labelled r0..r8, then a space, then a half-cell indent on even rows.
    row = int(line[1]); indent = 1 if line[3] == " " else 0
    for col in range(W):
        column[row * W + col] = 3 + indent + col * 2
wrong = [(i, d, n) for i in range(W*H) for d, n in neighbours(i).items()
         if not ((abs(n//W - i//W) == 0 and abs(column[n]-column[i]) == 2)
              or (abs(n//W - i//W) == 1 and abs(column[n]-column[i]) == 1))]
assert not wrong, wrong[:5]
print(f"  {W*H} cells x 6 directions: cells drawn touching are cells that touch in the engine")

print("\n=== case 4e: wide stacks occupy both cells; map can be turned off ===")
wide = json.loads(json.dumps(TURN))
wide["defender_army"][0].update({"is_wide": True, "tail": 56, "head": 55})
drawn = agent.render_map(wide)
row5 = [l for l in drawn.splitlines() if l.startswith("r5")][0]
assert row5.count("7") == 2, row5
print("  wide stack drawn in both cells:", row5.strip())
assert "\n" in agent.render_board(TURN, full=True, with_map=True)
assert agent.render_map(TURN) not in agent.render_board(TURN, full=True, with_map=False)
print("  with_map=False omits the map")

print("\n=== case 4f: running out of budget costs a turn, not the battle ===")
exhausted = ModelError("the model produced 30000 characters of reasoning but no answer.")
a = build([Assessment(situation="s", plan="p")], [exhausted, ATTACK])
out = a.decide(TURN)
print("  recovered to:", out)
assert out == {"action": "attack", "target": 3}, out
print("-> a failed attempt is retried, the battle continues")

a = build([Assessment(situation="s", plan="p")], [exhausted, exhausted, exhausted, exhausted])
out = a.decide(TURN)
print("  after every attempt failed:", out)
assert out == {"action": "skip"}, out
print("-> bounded failures end in a skip, never an abandoned battle")

a = build([exhausted], [SKIP])
out = a.decide(TURN)
print("  assessment failed, decision still made:", out)
assert out == {"action": "skip"}, out
print("-> losing the assessment does not cost the turn")

print("\n=== case 4g: a spell does not cost the stack its action ===")
a = build([Assessment(situation="s", plan="p")], [(BLESS, ATTACK)])
first = a.decide(TURN)
print("  first reply (the spell):", first)
assert first == {"action": "cast", "spell": 15, "target": 7}, first
assert a._ask.action_calls == 1

# The engine asks again for the same stack, because casting did not use up its action.
again = a.decide(TURN)
print("  second reply (the stack acts):", again)
assert again == {"action": "attack", "target": 3}, again
assert a._ask.action_calls == 1, "the held action must not cost another model call"
print("-> one model call produced both the spell and the attack")

print("\n=== case 4h: a held action that is no longer legal is re-decided ===")
a = build([Assessment(situation="s", plan="p")], [(BLESS, ATTACK), 1])
a.decide(TURN)
narrowed = json.loads(json.dumps(TURN))
narrowed["legal_actions"]["attack"] = []      # the target died to the spell
# With no attack left, the stack menu starts at the moves, so 1 is now "move to r1c1".
out = a.decide(narrowed)
print("  re-decided to:", out)
assert out == {"action": "move", "cell": 12}, out
assert a._ask.action_calls == 2, "a stale plan should cost a fresh decision"
print("-> a plan invalidated by the spell is not blindly replayed")

print("\n=== case 5: the conversation is trimmed, and a full board follows ===")
a = build([Assessment(situation="s", plan="p")], [SKIP])
a._begin(TURN)
for i in range(20):
    a._messages.append({"role": "user", "content": f"turn {i} " + "x" * 400})
    a._messages.append({"role": "assistant", "content": '{"stack_action_number": 1}'})
a._last_shown = {"something": "stale"}
a._prompt_budget = 200
before = len(a._messages)
a._trim_conversation()
print(f"  messages {before} -> {len(a._messages)}, dropped {a._dropped_turns} turns")
assert len(a._messages) < before, "nothing was trimmed"
assert a._messages[0]["role"] == "system", "the briefing must survive trimming"
assert a._last_shown is None, "after trimming there is nothing to diff against"
assert "Where everything stands" in a._board_message(TURN), "a trimmed conversation must be re-anchored"
print("-> trimming forces the next board to be sent in full")

print("\n=== case 6: assessment is reused between refreshes ===")
a = build([Assessment(situation="s", plan="p")], [SKIP, SKIP], assess_every=4)
a.decide(TURN); a.decide(TURN)
print(f"assess calls for 2 turns with assess_every=4: {a._ask.assess_calls}")
assert a._ask.assess_calls == 1, a._ask.assess_calls
print("-> re-planning does not cost a call every turn")

print("\n=== case 6b: a stack being wiped out forces a fresh plan ===")
# A plan that names a stack which no longer exists is re-read as guidance every turn it is reused,
# so the roster changing has to override the refresh interval.
WIPED = json.loads(json.dumps(TURN))
WIPED["turn"] = 2
WIPED["attacker_army"] = []          # the stack the plan named is gone
WIPED["legal_actions"]["attack"] = []

# With nothing left to attack the menu is shorter, so "skip" sits at a different number.
WIPED_SKIP = len(agent.enumerate_unit_actions(WIPED))

a = build([Assessment(situation="s", plan="kill the Peasants")], [SKIP, WIPED_SKIP], assess_every=99)
a.decide(TURN)
a.decide(WIPED)

print(f"assess calls across the wipe-out: {a._ask.assess_calls}")
assert a._ask.assess_calls == 2, a._ask.assess_calls
print("-> the plan is not replayed against stacks that no longer exist")

print("\nALL PASS")
