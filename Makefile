###########################################################################
#   Standalone fheroes2 "battle only" binary.                             #
#                                                                         #
#   The fheroes2 source tree is used as-is and is never modified; all of   #
#   our own code lives in battle/.                                        #
###########################################################################

ROOT_DIR := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))

FHEROES2_DIR := $(ROOT_DIR)/fheroes2
BATTLE_DIR   := $(ROOT_DIR)/battle
BUILD_DIR    := $(BATTLE_DIR)/build

BINARY := $(BATTLE_DIR)/fheroes2-battle

# The game locates its resources relative to the binary, so the assets have to sit next to it.
ASSETS_STAMP := $(BATTLE_DIR)/data/HEROES2.AGG

JOBS := $(shell nproc 2>/dev/null || echo 4)

PYTHON  ?= python3

# Python for the LangGraph agent. Point it at a virtualenv that has harness/requirements.txt.
HARNESS_PYTHON ?= $(PYTHON)
HARNESS := $(ROOT_DIR)/harness/harness.py

# Where the harness listens and the game dials out to.
HARNESS_HOST ?= 127.0.0.1
HARNESS_PORT ?= 9000

# The model endpoint. Override on the command line, e.g. LLM_MODEL=gpt-4o-mini.
LLM_BASE_URL ?= http://127.0.0.1:11435/v1
LLM_MODEL    ?= bonsai-27b

# Output budget per call. A reasoning model spends most of it thinking, so this is generous:
# too small and it thinks until the budget runs out and never answers.
LLM_MAX_TOKENS ?= 16384

# How hard the model thinks per move: off, minimal, low, medium, high. Empty leaves it to the
# model's own default. Not every server understands this.
LLM_EFFORT ?=

# How long the game waits for one move before giving up on the harness.
REPLY_TIMEOUT ?= 1800

# Default battle for the scenario targets.
SCENARIO ?= $(BATTLE_DIR)/scenarios/archers-vs-goblins.json

# Where the engine records every choice both sides made. Written by the game itself rather than by
# either player, so it is the one account of a battle that does not depend on who is telling it.
BATTLE_LOG ?= $(ROOT_DIR)/harness/battle-log.jsonl

# Where 'make relay' puts the two files a person (or a coding agent) plays through.
RELAY_DIR ?= $(ROOT_DIR)/harness/relay

.PHONY: all setup build run run-vs-llm harness demo scenarios harness-random harness-deps harness-test test-e2e scenario scenario-vs-llm relay analyze assets clean distclean help

all: build

help:
	@echo "Targets:"
	@echo "  setup      Check out the fheroes2 submodule, build, and extract the game assets"
	@echo "  build      Build the battle-only binary"
	@echo "  run        Build if needed, then run the battle-only binary"
	@echo "  assets     Extract the game assets from the HoMM2 installer"
	@echo "  clean      Remove the build directory and the built binary"
	@echo "  distclean  Also remove the extracted assets"
	@echo ""
	@echo "Playing against an LLM:"
	@echo "  demo            One command: the game's AI versus the model, nothing to click"
	@echo "                  (PAUSE=1 waits for a click before turn 1, for screen recording)"
	@echo "  scenarios       List the predefined battles"
	@echo "  harness         Start the harness and wait for a game to connect"
	@echo "  harness-random  Start the harness playing random legal moves, no LLM needed"
	@echo "  run-vs-llm      Run the game with the red side played by the harness"
	@echo "  harness-deps    Install the LangGraph agent's dependencies"
	@echo "  harness-test    Test the agent's graph against a fake model, no API calls"
	@echo ""
	@echo "Scripted battles (SCENARIO=<file.json> to pick one):"
	@echo "  scenario         Fight it, both sides game AI, print the JSON result"
	@echo "  scenario-vs-llm  Fight it with red played by a listening harness"
	@echo "  test-e2e         Headless end-to-end test, no display and no model needed"
	@echo ""
	@echo "Playing it yourself (or having a coding agent play):"
	@echo "  relay           Start the game and ask YOU for each move, through two files"
	@echo "                  (RELAY_DIR=<dir> to pick where; read turn.md, write move)"
	@echo ""
	@echo "Reading a battle back:"
	@echo "  analyze          Summarise who did what from the engine's battle log"
	@echo "                   (BATTLE_LOG=<file.jsonl> to pick one)"

# Full bootstrap from a bare checkout.
setup:
	$(ROOT_DIR)/setup.sh

build: $(BINARY)

$(BINARY): $(FHEROES2_DIR)/src/fheroes2/game/fheroes2.cpp $(wildcard $(BATTLE_DIR)/src/*.cpp) $(wildcard $(BATTLE_DIR)/vendor/*.cpp) $(BATTLE_DIR)/CMakeLists.txt
	cmake -B $(BUILD_DIR) -S $(BATTLE_DIR) -DCMAKE_BUILD_TYPE=Release
	cmake --build $(BUILD_DIR) -j $(JOBS)
	cp $(BUILD_DIR)/fheroes2-battle $@

assets: $(ASSETS_STAMP)

$(ASSETS_STAMP):
	$(ROOT_DIR)/setup.sh --no-clone --no-build

# The binary resolves its resources relative to its own location, but the working directory has to
# be there too so that fheroes2.cfg and the saved games land next to the assets rather than here.
run: build $(ASSETS_STAMP)
	cd $(BATTLE_DIR) && ./fheroes2-battle

# The red side is played by the harness, which must already be listening. Blue stays human, so
# change red's mode in the setup screen if you want the game AI to play it instead.
run-vs-llm: build $(ASSETS_STAMP)
	cd $(BATTLE_DIR) && ./fheroes2-battle --red external:tcp://$(HARNESS_HOST):$(HARNESS_PORT) --reply-timeout $(REPLY_TIMEOUT)

# Starts the harness and waits for a game to connect. Checks the endpoint first and prints the
# commands for running a battle against it.
harness:
	$(HARNESS_PYTHON) $(HARNESS) --host $(HARNESS_HOST) --port $(HARNESS_PORT) \
		--base-url $(LLM_BASE_URL) --model $(LLM_MODEL) --max-tokens $(LLM_MAX_TOKENS) \
		$(if $(LLM_EFFORT),--reasoning-effort $(LLM_EFFORT),) --verbose

# Lists the predefined battles and who fights in them.
scenarios:
	@echo "Predefined battles (pass one as SCENARIO=<file>):"
	@for f in $(BATTLE_DIR)/scenarios/*.json; do \
		printf "  %-28s " "$$(basename $$f)"; \
		$(PYTHON) -c "import json,sys; d=json.load(open('$$f')); f=lambda k: d[k]['hero']+' ('+', '.join(str(t['count'])+'x '+t['monster'] for t in d[k]['troops'])+')'; print(f('blue'),'vs',f('red'))"; \
	done

# The one-command version: starts the harness AND the game, the game's own AI versus the model.
demo:
	$(HARNESS_PYTHON) $(HARNESS) --host $(HARNESS_HOST) --port $(HARNESS_PORT) \
		--base-url $(LLM_BASE_URL) --model $(LLM_MODEL) --max-tokens $(LLM_MAX_TOKENS) \
		$(if $(LLM_EFFORT),--reasoning-effort $(LLM_EFFORT),) \
		--demo --scenario $(SCENARIO) --reply-timeout $(REPLY_TIMEOUT) $(if $(PAUSE),--pause,) \
		$(if $(BATTLE_LOG),--battle-log $(BATTLE_LOG),) --verbose

# Installs the LangGraph agent's dependencies into whatever Python is active. The --random
# harness needs none of this and keeps working without it.
harness-deps:
	$(HARNESS_PYTHON) -m pip install -r $(ROOT_DIR)/harness/requirements.txt

# Exercises the agent's graph against a scripted fake model, so no API calls are made.
harness-test:
	$(HARNESS_PYTHON) $(ROOT_DIR)/harness/test_agent.py

# Fights a scenario headless with the harness playing one side. Needs no display and no model.
test-e2e: build $(ASSETS_STAMP)
	$(BATTLE_DIR)/test_e2e.sh $(SCENARIO)

# Fights a scenario with both sides played by the game AI, printing the JSON result line.
scenario: build $(ASSETS_STAMP)
	cd $(BATTLE_DIR) && ./fheroes2-battle --blue ai --red ai --headless --scenario $(SCENARIO) \
		$(if $(BATTLE_LOG),--battle-log $(BATTLE_LOG),)

# Fights a scenario with red played by the harness, which must already be listening.
scenario-vs-llm: build $(ASSETS_STAMP)
	cd $(BATTLE_DIR) && ./fheroes2-battle --blue ai --red external:tcp://$(HARNESS_HOST):$(HARNESS_PORT) \
		--reply-timeout $(REPLY_TIMEOUT) --scenario $(SCENARIO) \
		$(if $(BATTLE_LOG),--battle-log $(BATTLE_LOG),)

# Starts the game and plays red by hand: each turn is written to RELAY_DIR/turn.md and the harness
# waits for RELAY_DIR/move. Needs no model and no API key, but does need the agent's dependencies,
# because the questions are rendered by the same code the model is given.
relay: build $(ASSETS_STAMP)
	$(HARNESS_PYTHON) $(HARNESS) --host $(HARNESS_HOST) --port $(HARNESS_PORT) \
		--relay $(RELAY_DIR) --demo --scenario $(SCENARIO) --reply-timeout $(REPLY_TIMEOUT) \
		$(if $(PAUSE),--pause,) $(if $(BATTLE_LOG),--battle-log $(BATTLE_LOG),) --once --verbose

# Reads the engine's battle log back: who acted, what they chose, and what it cost the other side.
analyze:
	$(PYTHON) $(ROOT_DIR)/harness/analyze_log.py $(BATTLE_LOG)

harness-random:
	$(PYTHON) $(HARNESS) --host $(HARNESS_HOST) --port $(HARNESS_PORT) --random --verbose

clean:
	rm -rf $(BUILD_DIR)
	rm -f $(BINARY)

distclean: clean
	rm -rf $(BATTLE_DIR)/anim $(BATTLE_DIR)/data $(BATTLE_DIR)/maps $(BATTLE_DIR)/music $(BATTLE_DIR)/files
