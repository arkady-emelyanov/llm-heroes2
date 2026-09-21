#!/usr/bin/env bash

###########################################################################
#   End-to-end test: a scenario battle fought to completion with one side  #
#   played by the harness over the wire.                                   #
#                                                                          #
#   Runs headless and unattended, so it needs neither a display nor a      #
#   model: the harness plays random legal moves. What is under test is the #
#   whole path -- scenario loading, army setup, the protocol, the external #
#   AI hook, and the result report -- not the quality of play.             #
###########################################################################

set -e -o pipefail

readonly BATTLE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly ROOT_DIR="$(dirname "${BATTLE_DIR}")"

readonly BINARY="${BATTLE_DIR}/fheroes2-battle"
readonly HARNESS="${ROOT_DIR}/harness/harness.py"
# Resolved to an absolute path: the game is run with its own directory as the working directory, so
# a relative path given on the command line would be looked up in the wrong place.
SCENARIO="${1:-${BATTLE_DIR}/scenarios/archers-vs-goblins.json}"
[[ -f "${SCENARIO}" ]] && SCENARIO="$(cd -- "$(dirname -- "${SCENARIO}")" && pwd)/$(basename -- "${SCENARIO}")"
readonly SCENARIO

# A port unlikely to collide with a harness the user is already running.
readonly PORT="${HARNESS_PORT:-9201}"

readonly WORK_DIR="$(mktemp -d)"
# The trap must not decide the exit status. With --once the harness exits on its own, so by the time
# this runs the kill usually fails - and a failing last command in an EXIT trap replaces the script's
# status, reporting a passing test as a failure.
function cleanup {
    local status=$?

    rm -rf -- "${WORK_DIR}"

    if [[ -n "${harness_pid:-}" ]]; then
        kill "${harness_pid}" 2> /dev/null || true
    fi

    exit "${status}"
}

trap cleanup EXIT

function die {
    echo -e "\033[0;31mFAIL: $*\033[0m" >&2
    exit 1
}

[[ -x "${BINARY}" ]] || die "'${BINARY}' not built. Run: make build"
[[ -f "${SCENARIO}" ]] || die "scenario '${SCENARIO}' does not exist."
[[ -f "${BATTLE_DIR}/data/HEROES2.AGG" ]] || die "game assets missing. Run: make assets"

echo "Starting the harness on port ${PORT} (random legal moves)..."

python3 "${HARNESS}" --port "${PORT}" --random --once --verbose > "${WORK_DIR}/harness.log" 2>&1 &
harness_pid=$!

# Wait for the listening socket rather than sleeping a fixed amount, so the test is not racy on a
# loaded machine.
for _ in $(seq 50); do
    if grep -q "Harness listening" "${WORK_DIR}/harness.log" 2> /dev/null; then
        break
    fi
    sleep 0.1
done

grep -q "Harness listening" "${WORK_DIR}/harness.log" || die "the harness did not start:
$(cat "${WORK_DIR}/harness.log")"

echo "Fighting '${SCENARIO}' with blue = game AI, red = harness..."

# The dummy drivers let the battle render and play with no display and no sound card.
if ! ( cd "${BATTLE_DIR}" && timeout 300 env SDL_VIDEODRIVER=dummy SDL_AUDIODRIVER=dummy \
    "${BINARY}" --blue ai --red "external:tcp://127.0.0.1:${PORT}" --headless --scenario "${SCENARIO}" ) \
    > "${WORK_DIR}/game.log" 2>&1; then
    die "the game exited non-zero:
$(tail -20 "${WORK_DIR}/game.log")"
fi

result="$(grep '"type":"battle_result"' "${WORK_DIR}/game.log" || true)"

[[ -n "${result}" ]] || die "the battle produced no result line:
$(tail -20 "${WORK_DIR}/game.log")"

# A harness that never got asked anything would still let the battle finish, so the test also
# checks that the external side actually played.
turns="$(grep -c "sending:" "${WORK_DIR}/harness.log" || true)"

[[ "${turns}" -gt 0 ]] || die "the harness was never asked for an action:
$(cat "${WORK_DIR}/harness.log")"

if grep -q "rejected:" "${WORK_DIR}/harness.log"; then
    die "the game rejected actions the harness believed were legal:
$(grep 'rejected:' "${WORK_DIR}/harness.log")"
fi

echo
echo "Harness played ${turns} actions."
echo "Result: ${result}"
echo -e "\033[0;32mPASS\033[0m"
