#!/usr/bin/env bash
#
# Keeps the machine awake for the length of a battle, and puts it back exactly as it was.
#
# A model-driven battle is minutes of the machine sitting idle from the desktop's point of view -
# nobody types, nobody moves the mouse - so the screensaver blanks the screen and, eventually, the
# machine suspends mid-battle. Neither is wanted while a battle is being watched or recorded.
#
#     ./idle.sh off      keep the screen and the machine awake, saving the current settings
#     ./idle.sh on       restore whatever was saved
#     ./idle.sh status   show what is in force now
#
# The previous values are written to a file rather than assumed, so restoring puts back what was
# actually there instead of someone's idea of a sensible default.

set -euo pipefail

STATE_FILE="${XDG_RUNTIME_DIR:-/tmp}/llm-heroes2-idle-settings"

# Cinnamon's keys. Each line is "schema key", and the value is read and written with gsettings.
KEYS=(
    "org.cinnamon.desktop.screensaver idle-activation-enabled"
    "org.cinnamon.desktop.session idle-delay"
    "org.cinnamon.settings-daemon.plugins.power sleep-inactive-ac-timeout"
    "org.cinnamon.settings-daemon.plugins.power sleep-inactive-battery-timeout"
)

# What each key becomes while a battle runs: no idle detection, no idle suspend.
declare -A AWAKE=(
    ["org.cinnamon.desktop.screensaver idle-activation-enabled"]="false"
    ["org.cinnamon.desktop.session idle-delay"]="0"
    ["org.cinnamon.settings-daemon.plugins.power sleep-inactive-ac-timeout"]="0"
    ["org.cinnamon.settings-daemon.plugins.power sleep-inactive-battery-timeout"]="0"
)

# The schema list is captured before being searched rather than piped into grep: 'grep -q' stops at
# the first match and kills the writer with SIGPIPE, which 'pipefail' then reports as a failure -
# so a piped version answers "no such schema" for whichever schemas happen to sort early.
have_schema() {
    local schemas
    schemas=$(gsettings list-schemas 2>/dev/null)

    grep -qx "$1" <<<"$schemas"
}

case "${1:-}" in
off)
    if [[ -e "$STATE_FILE" ]]; then
        echo "Already off; '$STATE_FILE' holds the settings to restore. Run './idle.sh on' first." >&2
        exit 1
    fi

    : >"$STATE_FILE"

    for entry in "${KEYS[@]}"; do
        read -r schema key <<<"$entry"
        have_schema "$schema" || continue

        printf '%s\t%s\t%s\n' "$schema" "$key" "$(gsettings get "$schema" "$key")" >>"$STATE_FILE"
        gsettings set "$schema" "$key" "${AWAKE[$entry]}"
    done

    # X11's own screen blanking is separate from the desktop's, and is not a stored setting - it is
    # reset by anything that restarts the X session, so there is nothing to save here.
    if [[ -n "${DISPLAY:-}" ]] && command -v xset >/dev/null; then
        xset s off -dpms
    fi

    echo "Screensaver and idle suspend are off. Previous settings saved to $STATE_FILE."
    ;;

on)
    if [[ ! -e "$STATE_FILE" ]]; then
        echo "Nothing saved in '$STATE_FILE'; leaving the settings alone." >&2
        exit 0
    fi

    while IFS=$'\t' read -r schema key value; do
        [[ -n "$schema" ]] || continue
        gsettings set "$schema" "$key" "$value"
    done <"$STATE_FILE"

    rm -f "$STATE_FILE"

    if [[ -n "${DISPLAY:-}" ]] && command -v xset >/dev/null; then
        xset s on +dpms
    fi

    echo "Screensaver and idle suspend restored."
    ;;

status)
    for entry in "${KEYS[@]}"; do
        read -r schema key <<<"$entry"
        have_schema "$schema" || continue
        printf '  %-60s %s\n' "$schema $key" "$(gsettings get "$schema" "$key")"
    done

    if [[ -e "$STATE_FILE" ]]; then
        echo "Saved settings waiting to be restored in $STATE_FILE."
    fi
    ;;

*)
    echo "Usage: $0 {off|on|status}" >&2
    exit 1
    ;;
esac
