# External AI protocol

The game plays a battle; the harness decides what one side does. The harness is the **server**: it
listens, the game connects to it. That way the harness outlives the game and can keep an LLM session
warm across many battles.

Transport is a TCP stream of **newline-delimited JSON**, one object per line, UTF-8.

    fheroes2-battle --red external:tcp://127.0.0.1:9000

One connection is opened per externally controlled side, when that side first needs to act, and
closed when the battle ends.

## Handshake

The game opens with:

```json
{"type": "hello", "protocol": 1, "game": "fheroes2-battle", "side": "defender"}
```

The harness must answer:

```json
{"type": "ready"}
```

Anything else aborts the battle with a message. This is deliberate: a misconfigured harness is
caught before the battle starts rather than halfway through one.

## Each turn

The game sends the full state every turn — there are no diffs to track:

```json
{
  "type": "turn",
  "protocol": 1,
  "turn": 3,
  "side": "defender",
  "color": "red",
  "unit": {"uid": 7, "name": "Archer", "count": 40, "head": 55, "speed": 4, "shots": 12},
  "attacker_army": [ { ... }, ... ],
  "defender_army": [ { ... }, ... ],
  "attacker_commander": {
    "spell_points": 17, "max_spell_points": 17,
    "spells": [{"id": 15, "name": "Bless", "cost": 3}]
  },
  "defender_commander": { ... },
  "legal_actions": {
    "move": [12, 13, 14],
    "move_breaks_contact": [13, 14],
    "move_out_of_enemy_reach": [14],
    "attack": [{"target": 3, "name": "Peasant", "ranged": true}],
    "cast": [
      {"spell": 15, "name": "Bless", "cost": 3,
       "needs_target": true, "needs_destination": false, "targets": [7, 9]},
      {"spell": 24, "name": "Armageddon", "cost": 15,
       "needs_target": false, "needs_destination": false, "targets": []}
    ],
    "can_skip": true,
    "can_retreat": false,
    "can_surrender": false
  }
}
```

Each entry of an army array describes one stack:

| Field | Meaning |
|---|---|
| `uid` | stable id of the stack, used as an attack `target` |
| `name`, `count`, `color` | what it is, how many, whose |
| `head`, `tail` | board cell indices; `tail` only for wide units |
| `hit_points`, `hit_points_left` | total, and of the top creature |
| `attack`, `defense`, `speed` | current values, with spells applied |
| `is_wide`, `is_flying`, `is_archer`, `shots` | movement and ranged ability |
| `is_current` | whether this is the stack now acting |

`legal_actions` is computed by the engine, so the harness picks from a list rather than
reimplementing the rules. Cells in `move` are reachable this turn; units in `attack` can be hit this
turn, in melee or at range.

Two subsets of `move` answer the question a stack actually asks before walking away from a fight.
Both are computed with the board's parity-dependent neighbours, which a harness would otherwise
derive by hand for every cell it was offered:

| Field | Meaning |
|---|---|
| `move_breaks_contact` | cells with no enemy standing beside them **right now** |
| `move_out_of_enemy_reach` | of those, the cells no enemy can get beside before this stack acts again |

The second is always a subset of the first, and the difference between them matters: a cell that
merely breaks contact says nothing about the flier four cells away that will simply follow. A
shooter that walks has already given up its shot, so telling it "out of reach" on the strength of
the first list alone promises a safety that does not hold — and against a flying enemy, which can
land anywhere it fits, `move_out_of_enemy_reach` is correctly empty.

Enemy reach is worked out from the speed each enemy will have when it next acts, so a stack that has
already moved this round still counts as the threat it is. Blinded and paralysed stacks threaten
only what is already beside them.

A commander's `spells` is everything in the hero's book, with the spell points each costs. It is
informational: what can actually be cast this turn is `legal_actions.cast` below, which is narrower.

`cast` lists the spells this side's hero can cast **right now** — already filtered for spell points,
for spells the hero actually knows, for whether a spell has been cast this turn already, and for
whether the spell would have any effect on each candidate target. An entry means:

| Field | Meaning |
|---|---|
| `spell`, `name`, `cost` | the spell id to send back, its name, and what it costs in spell points |
| `needs_target` | false for battlefield-wide spells such as Armageddon |
| `needs_destination` | true only for Teleport, which also needs somewhere to move the unit to |
| `targets` | unit uids this spell may legally be aimed at |

The hero casts at most one spell per turn, and **casting does not use up the stack's own action** —
after a cast the same stack is asked again for a move or an attack.

## The reply

Exactly one action, as one line:

```json
{"action": "move",      "cell": 13}
{"action": "attack",    "target": 3}
{"action": "cast",      "spell": 15, "target": 7}
{"action": "cast",      "spell": 24}
{"action": "cast",      "spell": 5, "target": 7, "cell": 42}
{"action": "skip"}
{"action": "retreat"}
{"action": "surrender"}
```

A `cast` takes `spell` from `legal_actions.cast`; `target` when that entry says `needs_target`, and
additionally `cell` when it says `needs_destination` (Teleport).

For a melee `attack` the game walks the unit to a reachable cell next to the target itself, so the
harness never has to work out the approach square. For a ranged attack it simply shoots.

## When the reply is not usable

If the action is missing, malformed, or not in `legal_actions`, the game answers on the same
connection:

```json
{"type": "error", "protocol": 1, "message": "cell 40 is not in this turn's legal_actions.move"}
```

and waits for another reply. After **4 unusable replies in a row** the unit skips its turn and the
battle continues.

A skip is deliberate. The alternative — quietly handing the turn to the game's own AI — would look
exactly like the harness playing well, which is the one outcome that must never be ambiguous. A skip
is visible in the battle log and cannot be mistaken for competent play.

## Timeouts and disconnects

The game waits up to **300 seconds** for each reply, which is generous because the harness is calling
an LLM behind it. If the connection drops or times out, the battle is abandoned and the setup screen
returns with an explanation; the game never silently continues without the harness.

## The reference harness

`harness/harness.py` implements this protocol. It is only the server and the framing; the thinking
lives in `harness/agent.py`, a LangGraph agent that runs four steps per turn:

    assess ──> draft ──> validate ──┬──> (send)
                  ^                 │
                  └──── revise <────┘

`validate` is a plain function, not a model call: the game already sent `legal_actions`, so an
illegal move is caught locally and revised before it ever reaches the game. That matters because
every round-trip through the game's own rejection path costs one of the four retries above and ends
in a skipped turn.

`--random` plays legal moves with no model and no dependencies, which is the quickest way to check
that the game, the protocol and your setup work.

### What the model is actually shown

The agent does not forward this protocol's JSON to the model. It re-renders it, because what is
cheap to parse and what is cheap to read are different things, and on a local model the tokens
spent per turn set how long a battle takes.

A notation is declared once, in the system prompt, and reused for every stack:

    uid|count Name|c<cell>|A<attack> D<defense> S<speed>[|R<shots>][|F][|W][|*]

so a stack reads `4|60 Goblin|c55|A4 D2 S3|R8|*` — about 20 tokens where naming every field costs
36. After the first board only what changed is sent:

    1|20->17        that stack lost creatures
    4|c55->c44      that stack moved
    4|gone          that stack was wiped out

On a six-stack board that is 170 tokens a turn against 268 for a whole board. A full board is
re-sent every `refresh_every` turns, and whenever the context is trimmed, since a delta is
meaningless once the board it references has been dropped.

`legal_actions` is the one thing never abbreviated: it is the menu the model chooses from, so
shortening it would trade a few tokens for illegal moves.

Two things this is *not*. It is not a change to the wire format above — the game sends the same
JSON either way. And it is not, on its own, a speed fix: a prompt is 500-700 tokens against 800-3000
tokens of reasoning, so halving the prompt moves wall time by single digits. It matters most for
fitting a long battle into the context window.

## Scenario files

A battle can be described in JSON and fought without touching the setup screen, which is what makes
unattended runs possible. See `battle/scenarios/` for examples:

```json
{
  "terrain": "lava",
  "blue": {
    "hero": "Falagar",
    "troops": [{ "monster": "Centaur", "count": 30 }],
    "artifacts": ["Thunder Mace of Dominion"],
    "spells": ["Lightning Bolt", "Bless"]
  },
  "red": { "hero": "Ariel", "troops": [{ "monster": "Elf", "count": 30 }] }
}
```

A scenario may also carry the model settings it should be played with, in an optional `llm` block:

```json
{
  "llm": {
    "reasoning_effort": "low",
    "max_tokens": 12288,
    "assess_every": 4,
    "temperature": 0.2,
    "model": "bonsai-27b"
  }
}
```

The game ignores this block; the harness reads it, so one file describes a whole experiment — the
armies and how the model is asked to think about them. Anything given on the harness command line
wins over the file. An unknown setting is refused rather than ignored, because a silently dropped
setting looks exactly like a setting that did not work.

`reasoning_effort` is passed to the endpoint as-is (`minimal`, `low`, `medium`, `high`), except for
`off`, which asks llama.cpp to disable thinking entirely. Not every server understands either.

Heroes, monsters, artifacts, spells and terrain are named the way the game names them; matching
ignores case, spaces, underscores and dashes. `artifacts` and `spells` are optional. A hero whose
faction starts without magic — Knights and Barbarians have no spell book at all — is given one
automatically when `spells` is present.

Anything the file gets wrong is reported and the battle is refused, rather than being quietly
defaulted: a scenario that does not describe the battle you meant is a mistake worth seeing.

## What the engine does on its own

Several things that look like missing actions are not decisions at all — the engine resolves them,
whoever is playing, so the harness neither sees nor needs them:

- **Creature built-in spells** (a Ghost draining, an Archmage dispelling, and so on) are applied
  during attack resolution in `ApplyActionAttack`, and the engine picks the target itself when more
  than one is eligible.
- **Castle towers and the catapult** are driven by the Arena's own turn loop, which constructs and
  applies their commands internally. They are not offered to a player or to the AI.
- **Good and bad morale** are issued by the Arena as `Command::MORALE` around a unit's turn.
- The **Ballista of Quickness** is an artifact granting an extra catapult shot, not a battlefield
  action. A scenario can hand it to a hero through `artifacts` like any other.

## The battle log

`--battle-log <file.jsonl>` makes the engine record every choice both sides made, one JSON object
per line. It is not part of this protocol and the harness never reads it back mid-battle; it exists
because neither player's own account of a battle can be checked against the other's. The harness
knows what it decided to send but not what the engine made of it, and the game's own AI says nothing
at all.

The record is taken where a turn becomes engine commands, so a human, the game AI and the harness
are all logged the same way:

    {"event":"battle_start","seq":1,"attacker":[...],"defender":[...]}
    {"event":"turn","seq":2,"round":1,"side":"defender","controller":"external",
     "acting":{"uid":3,"name":"Goblins","count":60,"cell":"r0c10"},
     "commands":[{"type":"cast","spell_id":9,"spell":"Bless","at":"r0c10"},
                 {"type":"move","uid":3,"to":"r0c6"}],
     "board":{"attacker":[...],"defender":[...]}}
    {"event":"battle_end","seq":17,"rounds":4,"winner":"attacker","attacker":[...],"defender":[...]}

`controller` is `human`, `game-ai` or `external`. `commands` holds the engine's own parameters,
decoded: an `attack` carries `target_uid`, `move_to`, `strike_cell` and `direction`, a `cast`
carries the spell and its target cell, a `move` its destination.

Damage is deliberately absent. Each turn record carries the board **as it stood before its own
commands were applied**, so what a turn cost is whatever differs in the next record — which makes
the effect of a choice something read off the log rather than something the log asserts.

`harness/analyze_log.py` reads it back: a per-controller tally of what was chosen (including whether
a side ever cast a spell at all) followed by the battle as a list of turns with their losses. `make
analyze BATTLE_LOG=<file>` runs it.

## Not supported

**Sieges.** Both the setup screen and scenario files fight on open ground: the Arena takes its castle
from `world.getCastleEntrance()` for the tile the battle is on, and the battle-only map has no castle
on it. So walls, the moat, towers, the bridge and the catapult never come into play, and the
`CastleDefenseStructure` half of the engine is untested here.
