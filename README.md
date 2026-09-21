# llm-heroes2

An LLM plays Heroes of Might and Magic II battles.

The game is the real one: [fheroes2](https://github.com/ihhub/fheroes2) running your own HoMM2 data
files. A standalone binary boots straight into a battle, and one or both sides can be handed to a
harness that translates the battlefield into a prompt, asks a model for a move, and translates the
answer back into engine commands. You can watch it play, play against it yourself, or have a coding
agent play a side through two files.

Nothing here modifies the fheroes2 sources. The engine is a pinned git submodule; the four files
that needed changing are vendored under `battle/vendor/` with every deviation marked `LOCAL CHANGE`.

---

## What you need

**A legal copy of Heroes of Might and Magic II.** The game data is not in this repository and
cannot be — it is copyrighted. Buy it from
[GOG](https://www.gog.com/en/game/heroes_of_might_and_magic_2_gold_edition) (Gold Edition), then
**download the offline installer** rather than installing through GOG Galaxy:

> On the GOG page → *Download offline backup game installers* → the Windows `.exe`.
> It will be named something like `setup_heroes_of_might_and_magic_2_gold_1.01_(2.1)_(33438).exe`.

Put that `.exe` in the root of this repository. You do **not** need Windows or Wine — the installer
is unpacked, never run. `setup.sh` finds any file matching `setup_heroes_of_might_and_magic_2*.exe`
in the repo root on its own; anything else, pass the path as an argument.

**A Linux machine** with a display if you want to watch battles (headless works without one).

**System packages.** On Debian/Ubuntu:

```bash
sudo apt-get update && sudo apt-get install -y \
    git cmake make g++ gettext pkg-config \
    libsdl2-dev libsdl2-mixer-dev \
    innoextract libarchive-tools python3 python3-venv
```

What each is for: `cmake`/`make`/`g++`/`gettext`/`pkg-config` and the SDL2 packages build the
engine; `innoextract` unpacks the GOG installer; `libarchive-tools` provides `bsdtar`, which reads
the CD image GOG ships the animations inside; `python3` runs the harness.

`setup.sh` checks all of these before doing anything and tells you the exact `apt-get` line for
whatever is missing, so a wrong guess here is not fatal.

---

## Build it

```bash
git clone --recurse-submodules <this repo> llm-heroes2
cd llm-heroes2
# put the GOG installer .exe here
./setup.sh
```

`./setup.sh` (or `make setup`) does the whole bootstrap: checks dependencies, checks out the pinned
fheroes2 submodule, builds `battle/fheroes2-battle` out of tree, compiles the translations, unpacks
the installer, converts GOG's raw-sector `homm2.gog` CD image to a plain ISO, and copies every asset
into `battle/` next to the binary. Expect a few minutes, mostly compiling.

If you cloned without `--recurse-submodules`, `setup.sh` initialises the submodule itself.

Useful flags when re-running: `--no-clone`, `--no-build`, `--no-assets`.

**Check it worked** — a full battle, headless, no display and no model:

```bash
make test-e2e
```

That plays a scripted battle with one side driven over the wire by a harness making random legal
moves. It exercises scenario loading, army setup, the protocol, the external-AI hook and the result
report. If it prints a `battle_result` JSON line, everything below will work.

---

## Play it yourself

```bash
make run
```

Boots to a setup screen where you pick heroes, troops, artifacts and spells for both sides, then
fight. Auto-combat is deliberately disabled — a battle here is always played out.

---

## Hand a side to a model

The harness is the **server**; the game dials out to it. That way the game needs no credentials and
the harness needs no inbound access to the machine running the game.

### The Python environment

The harness's `--random` and `--relay` paths need only the standard library, but the LangGraph agent
needs three packages. Either a venv:

```bash
python3 -m venv .venv
. .venv/bin/activate
make harness-deps
```

or pyenv (this repo's `.python-version` names an environment called `llm-heroes2`):

```bash
pyenv virtualenv 3.12 llm-heroes2   # once
make harness-deps
```

If you keep the environment somewhere else, point the Makefile at it rather than activating it:

```bash
make demo HARNESS_PYTHON=/path/to/venv/bin/python3
```

Check the agent's graph without spending a token:

```bash
make harness-test
```

### Any OpenAI-compatible endpoint

`--base-url` takes any OpenAI-compatible `/v1`. The one command that starts both the harness and the
game, with the model playing red against the game's own AI:

```bash
# A local llama.cpp / Ollama / vLLM server
make demo LLM_BASE_URL=http://127.0.0.1:11435/v1 LLM_MODEL=my-local-model

# OpenAI
export OPENAI_API_KEY=sk-...
make demo LLM_BASE_URL=https://api.openai.com/v1 LLM_MODEL=gpt-4o-mini
```

`OPENAI_API_KEY` is read from the environment and is required unless the base URL is localhost. The
endpoint is verified before the battle starts, so a bad URL or model name fails immediately rather
than on turn 14.

Knobs worth knowing (all `VAR=value` on the `make` line):

| variable | default | what it does |
|---|---|---|
| `LLM_BASE_URL` | `http://127.0.0.1:11435/v1` | OpenAI-compatible endpoint |
| `LLM_MODEL` | `bonsai-27b` | model name |
| `LLM_MAX_TOKENS` | `16384` | output budget per call; a reasoning model spends most of it thinking |
| `LLM_EFFORT` | unset | `off`/`minimal`/`low`/`medium`/`high`, if your server honours it |
| `SCENARIO` | `battle/scenarios/archers-vs-goblins.json` | which battle |
| `BATTLE_LOG` | `harness/battle-log.jsonl` | where the engine records every choice |
| `REPLY_TIMEOUT` | `1800` | seconds the game waits for one move |
| `PAUSE` | unset | `PAUSE=1` waits for a click before turn 1, for screen recording |

`make scenarios` lists the predefined battles. Scenario files are plain JSON — terrain, heroes,
troops, artifacts, spells — documented in [`battle/PROTOCOL.md`](battle/PROTOCOL.md).

Other layouts, if you want the game and the harness in separate terminals:

```bash
make harness        # terminal 1: listen, print the commands for terminal 2
make run-vs-llm     # terminal 2: you play blue, the model plays red
```

### Claude Code (or any coding agent) as the player

`make relay` swaps the agent for two files. The harness writes the question and waits for the
answer, so whoever is playing does not need a terminal — which is what makes it usable by a coding
agent.

```bash
make relay
```

Then, each turn:

- **read** `harness/relay/turn.md` — the question
- **write** `harness/relay/move` — one or two numbers

One number is the stack's action. Two numbers are the spell and then the stack's action, in menu
order: `0 5` casts nothing and takes action 5.

```bash
cat harness/relay/turn.md      # decide
echo "1 3" > harness/relay/move # cast spell 1, then take action 3
```

The question is rendered by the same code the model is given — the same frozen briefing, the same
per-turn delta, the same two numbered menus with the same damage and retaliation estimates, and the
same validation before anything reaches the engine. So a hand-played battle and a model-played one
are directly comparable. `RELAY_DIR=<dir>` puts the files elsewhere.

> **Instruction for a coding agent:** run `make relay` in the background, then loop — read
> `harness/relay/turn.md`, choose from the `STACK ACTION MENU`, write the number(s) to
> `harness/relay/move`, and wait for `turn.md` to change. The battle ends when the harness exits.
> Never pick `retreat` or `surrender`: both end the battle in immediate defeat.

---

## Read the battle back

Every choice both sides make is recorded by the engine itself — not by either player — one JSON
object per line:

```bash
make demo                                    # writes harness/battle-log.jsonl
make analyze                                 # who did what, and what it cost
python3 harness/analyze_log.py harness/battle-log.jsonl --follow   # live, as it is played
```

The summary counts each controller's choices (including whether a side ever cast a spell at all);
the turn list shows every move with the losses it caused. Damage is not stored — each turn record
carries the board *before* its own commands, so the effect of a choice is read off the difference
rather than asserted. Details in [`battle/PROTOCOL.md`](battle/PROTOCOL.md).

To record a battle on video, `PAUSE=1` holds the game on the drawn battlefield, naming both heroes
and who is playing them, until you click.

---

## Layout

```
setup.sh              one-shot bootstrap: submodule, build, assets
Makefile              every workflow below is a target; `make help` lists them
idle.sh               off/on: stop the screensaver suspending a long battle
fheroes2/             the engine, a pinned submodule, never modified
battle/
  CMakeLists.txt      builds the engine's sources minus its main(), plus ours
  src/                the battle-only binary: entry point, scenarios, protocol
  vendor/             the four engine files we had to change, marked LOCAL CHANGE
  scenarios/*.json    predefined battles
  PROTOCOL.md         the wire protocol, scenario format, and battle log
harness/
  harness.py          the server; --random, --relay, --demo
  agent.py            the LangGraph agent: assess -> draft -> validate -> revise
  relay.py            plays a side through two files instead of a model
  llm.py              streaming OpenAI-compatible client
  analyze_log.py      reads the engine's battle log back
  test_agent.py       exercises the graph against a scripted fake model
docs/                 notes on the model's reasoning and where it goes wrong
```

---

## Troubleshooting

**"Data files are not found."** The binary resolves assets relative to its own location, so it must
sit in `battle/` next to `data/`. Run it as `cd battle && ./fheroes2-battle`, or via `make run`.
Never run the copy in `battle/build/`.

**`cp: Text file busy` during a build.** A battle is still running and holding the binary. Close it,
then `make build` again. The build itself succeeded; only the copy into `battle/` failed.

**The battle plays itself.** Auto-combat is disabled in this build; if you see it, you are running a
stock fheroes2 binary rather than `battle/fheroes2-battle`.

**The harness waits forever.** The game connects to the harness, not the other way round. Start the
harness first, and check both are using the same `HARNESS_PORT` (default 9000).

**A long battle is interrupted by the screensaver.** `./idle.sh off` disables the screensaver and
idle suspend, saving your current settings; `./idle.sh on` puts them back exactly as they were.

**Port already in use.** A previous harness is still running. Find it with
`ps -o pid,cmd -C python3 | grep harness.py` and `kill` that PID — do not `pkill -f harness`, which
will also match the shell you type it in.
