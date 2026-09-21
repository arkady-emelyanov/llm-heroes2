#!/usr/bin/env bash

###########################################################################
#   Bootstrap for the standalone fheroes2 "battle only" binary.           #
#                                                                         #
#   Checks out the pinned fheroes2 submodule, builds the battle-only       #
#   binary out of tree, and extracts the game assets from an original      #
#   Heroes of Might and Magic II installer into battle/, next to the       #
#   binary.                                                                #
#                                                                         #
#   The fheroes2 submodule is used as-is and is never modified: the few    #
#   files we needed to change are vendored under battle/vendor.            #
###########################################################################

set -e -o pipefail

readonly ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly FHEROES2_DIR="${ROOT_DIR}/fheroes2"
readonly BATTLE_DIR="${ROOT_DIR}/battle"
readonly BUILD_DIR="${BATTLE_DIR}/build"

do_clone=1
do_build=1
do_assets=1
do_ost=1
installer=""
ost_archive=""

function echo_stage {
    echo
    echo -e "\033[0;32m==> $*\033[0m"
}

function echo_warn {
    echo -e "\033[0;33m$*\033[0m"
}

function die {
    echo -e "\033[0;31mError: $*\033[0m" >&2
    exit 1
}

function usage {
    cat <<EOF
Usage: $(basename "$0") [options] [installer.exe]

Options:
  --no-clone    Do not check out the fheroes2 submodule
  --no-build    Do not build the battle-only binary
  --no-assets   Do not extract the game assets
  --no-ost      Do not install the bonus soundtrack, even if one is found
  --ost FILE    Use this soundtrack archive instead of looking for one
  -h, --help    Show this help

The installer is the original HoMM2 setup executable (GOG or similar). If it is
not given, the script looks for setup_heroes_of_might_and_magic_2*.exe in
${ROOT_DIR}.

The game's own music comes out of that installer. GOG also offers the remastered
soundtrack as a separate bonus download; if an archive matching *ost*.zip is
found alongside it, those tracks are installed instead, being the same music at
a much higher bitrate.
EOF
}

while [[ "$#" -gt 0 ]]; do
    case "$1" in
    --no-clone) do_clone=0 ;;
    --no-build) do_build=0 ;;
    --no-assets) do_assets=0 ;;
    --no-ost) do_ost=0 ;;
    --ost)
        shift
        [[ "$#" -gt 0 ]] || die "'--ost' needs a path to the soundtrack archive."
        ost_archive="$1"
        ;;
    -h | --help)
        usage
        exit 0
        ;;
    -*) die "unknown option '$1'. Use --help for usage." ;;
    *) installer="$1" ;;
    esac
    shift
done

#
# Dependencies
#
echo_stage "Checking dependencies"

declare -a missing=()

function require {
    # $1 - command, $2 - Debian/Ubuntu package providing it
    if [[ -z "$(command -v "$1")" ]]; then
        missing+=("$2")
    fi
}

[[ "${do_clone}" == "1" ]] && require git git
if [[ "${do_build}" == "1" ]]; then
    require cmake cmake
    require make make
    require g++ g++
    require msgfmt gettext
fi
if [[ "${do_assets}" == "1" ]]; then
    require innoextract innoextract
    require bsdtar libarchive-tools
    require python3 python3
fi

if [[ "${#missing[@]}" -gt 0 ]]; then
    # Deduplicate while preserving order.
    readarray -t missing < <(printf '%s\n' "${missing[@]}" | awk '!seen[$0]++')

    die "missing tools. Install them with:
    sudo apt-get install -y ${missing[*]} libsdl2-dev libsdl2-mixer-dev"
fi

if [[ "${do_build}" == "1" ]] && ! pkg-config --exists sdl2 SDL2_mixer 2> /dev/null; then
    die "SDL2 development headers not found. Install them with:
    sudo apt-get install -y libsdl2-dev libsdl2-mixer-dev"
fi

echo "All required tools are present."

#
# fheroes2 source tree
#
if [[ "${do_clone}" == "1" ]]; then
    echo_stage "Preparing the fheroes2 source tree"

    # fheroes2 is a git submodule pinned to the revision this project is built and vendored against
    # (see battle/vendor/UPSTREAM_REV). Checking it out is therefore a submodule update, not a
    # clone, and the pin lives in the parent repository rather than in this script.
    if [[ ! -d "${ROOT_DIR}/.git" ]]; then
        die "'${ROOT_DIR}' is not a git repository, so the fheroes2 submodule cannot be checked out. Pass --no-clone if you manage the sources yourself."
    fi

    git -C "${ROOT_DIR}" submodule update --init --recursive fheroes2

    if [[ -n "$(git -C "${FHEROES2_DIR}" status --porcelain)" ]]; then
        echo_warn "The fheroes2 submodule has local changes. This project never needs any: everything we change is vendored under battle/vendor."
    fi

    echo "fheroes2 at $(git -C "${FHEROES2_DIR}" rev-parse --short HEAD)"
fi

[[ -f "${FHEROES2_DIR}/src/fheroes2/game/fheroes2.cpp" ]] || die "fheroes2 sources not found under '${FHEROES2_DIR}'."

#
# Build
#
if [[ "${do_build}" == "1" ]]; then
    echo_stage "Building the battle-only binary"

    cmake -B "${BUILD_DIR}" -S "${BATTLE_DIR}" -DCMAKE_BUILD_TYPE=Release
    cmake --build "${BUILD_DIR}" -j "$(nproc)"

    # The translations are built by their own makefile inside the fheroes2 tree; it only writes
    # .mo files next to the .po sources and leaves the tree otherwise untouched.
    make -C "${FHEROES2_DIR}/files/lang" -j "$(nproc)"

    cp "${BUILD_DIR}/fheroes2-battle" "${BATTLE_DIR}/fheroes2-battle"

    echo "Built ${BATTLE_DIR}/fheroes2-battle"
fi

#
# Assets
#
if [[ "${do_assets}" == "0" ]]; then
    echo
    echo "Done. Run the game with:  cd ${BATTLE_DIR} && ./fheroes2-battle"
    exit 0
fi

echo_stage "Locating the HoMM2 installer"

if [[ -z "${installer}" ]]; then
    # Nullglob so that a missing match leaves an empty array rather than the pattern itself.
    shopt -s nullglob
    candidates=("${ROOT_DIR}"/setup_heroes_of_might_and_magic_2*.exe)
    shopt -u nullglob

    if [[ "${#candidates[@]}" -eq 0 ]]; then
        die "no HoMM2 installer found in '${ROOT_DIR}'. Pass the path to it as an argument."
    fi
    if [[ "${#candidates[@]}" -gt 1 ]]; then
        die "several HoMM2 installers found in '${ROOT_DIR}'. Pass the one to use as an argument."
    fi

    installer="${candidates[0]}"
fi

[[ -f "${installer}" ]] || die "installer '${installer}' does not exist."

echo "Installer: ${installer}"

echo_stage "Extracting the installer"

extract_dir="$(mktemp -d)"
# shellcheck disable=SC2064 # expand extract_dir now, it must survive until the script exits
trap "rm -rf -- '${extract_dir}'" EXIT

innoextract -e -s -d "${extract_dir}" -- "${installer}"

# The game files may sit either at the top level or one directory down, depending on the installer.
homm2_dir=""
for candidate in "${extract_dir}" "${extract_dir}"/*; do
    if [[ -f "${candidate}/DATA/HEROES2.AGG" && -d "${candidate}/MAPS" ]]; then
        homm2_dir="${candidate}"
        break
    fi
done

[[ -n "${homm2_dir}" ]] || die "no HoMM2 game files found inside '${installer}'."

echo_stage "Copying game resources into ${BATTLE_DIR}"

mkdir -p "${BATTLE_DIR}"/{anim,data,maps,music,files/data,files/lang}

cp -r "${homm2_dir}/DATA"/*  "${BATTLE_DIR}/data"
cp -r "${homm2_dir}/MAPS"/*  "${BATTLE_DIR}/maps"
[[ -d "${homm2_dir}/ANIM" ]]  && cp -r "${homm2_dir}/ANIM"/*  "${BATTLE_DIR}/anim"
[[ -d "${homm2_dir}/MUSIC" ]] && cp -r "${homm2_dir}/MUSIC"/* "${BATTLE_DIR}/music"

# GOG ships the animations inside a raw CD image rather than as loose files.
if [[ -f "${homm2_dir}/homm2.gog" ]]; then
    echo "Extracting animations from the CD image, please wait..."

    iso_file="${extract_dir}/homm2.iso"

    # The image uses raw 2352-byte sectors, which no archiver reads directly. Convert it to a plain
    # ISO by keeping only the user data of each sector: Mode 1 stores it at offset 16, Mode 2 at 24.
    python3 - "${homm2_dir}/homm2.gog" "${iso_file}" <<'EOF'
import sys

raw_path, iso_path = sys.argv[1], sys.argv[2]

with open(raw_path, "rb") as raw_file, open(iso_path, "wb") as iso_file:
    while True:
        sector = raw_file.read(2352)
        if len(sector) < 2352:
            break
        iso_file.write(sector[24:2072] if sector[15] == 2 else sector[16:2064])
EOF

    bsdtar -x -f "${iso_file}" -C "${BATTLE_DIR}/anim" --include "HEROES2/ANIM/*" --strip-components=2
fi

#
# Bonus soundtrack
#
# fheroes2 looks for external music as <root>/music/homm2_NN.<ext>, where NN is the engine's own
# track id minus one, and it tries .ogg before .mp3 before .flac. GOG's bonus soundtrack is the same
# music at a far higher bitrate but is named "... OST - NN - Title.flac", one higher than the engine
# wants, so it has to be both renamed and renumbered - and the installer's .ogg of the same track
# has to go, or it would win on extension order and the bonus tracks would never be heard.
#
if [[ "${do_ost}" == "1" ]]; then
    if [[ -z "${ost_archive}" ]]; then
        shopt -s nullglob
        ost_candidates=("${ROOT_DIR}"/*[Oo][Ss][Tt]*.zip)
        shopt -u nullglob

        [[ "${#ost_candidates[@]}" -eq 1 ]] && ost_archive="${ost_candidates[0]}"

        if [[ "${#ost_candidates[@]}" -gt 1 ]]; then
            echo_warn "Several soundtrack archives found; pass the one to use with --ost. Using the game's own music."
        fi
    fi

    if [[ -n "${ost_archive}" ]]; then
        if [[ ! -f "${ost_archive}" ]]; then
            die "soundtrack archive '${ost_archive}' does not exist."
        fi

        echo_stage "Installing the bonus soundtrack"
        echo "Archive: ${ost_archive}"

        python3 - "${ost_archive}" "${BATTLE_DIR}/music" <<'EOF'
import os
import re
import sys
import zipfile

archive_path, music_dir = sys.argv[1], sys.argv[2]

# "Heroes of Might and Magic II OST - 07 - Town - Necromancer.flac" -> track 7. The number wanted is
# the one right after "OST -"; later digits belong to the title, so the pattern is anchored on it.
track_pattern = re.compile(r"OST\s*-\s*(\d+)\s*-\s*.*?\.(flac|mp3|ogg)$", re.IGNORECASE)

installed = 0
superseded = 0

with zipfile.ZipFile(archive_path) as archive:
    for entry in archive.infolist():
        if entry.is_dir():
            continue

        match = track_pattern.search(os.path.basename(entry.filename))
        if match is None:
            continue

        # The engine numbers from the CD's data track, which the soundtrack does not include, so
        # every bonus track sits exactly one ahead of the name the engine will ask for.
        number = int(match.group(1)) - 1
        extension = match.group(2).lower()

        target = os.path.join(music_dir, f"homm2_{number:02d}.{extension}")

        with archive.open(entry) as source, open(target, "wb") as destination:
            while chunk := source.read(1 << 20):
                destination.write(chunk)

        installed += 1

        # Extension order means a leftover .ogg of the same track would be chosen over this one.
        for beaten in ("ogg", "mp3"):
            if beaten == extension:
                continue

            stale = os.path.join(music_dir, f"homm2_{number:02d}.{beaten}")
            if os.path.exists(stale):
                os.remove(stale)
                superseded += 1

print(f"Installed {installed} soundtrack tracks, replacing {superseded} of the game's own.")
EOF
    else
        echo
        echo "No bonus soundtrack archive found; using the game's own music."
    fi
fi

echo_stage "Copying fheroes2 resources"

cp "${FHEROES2_DIR}/files/data"/*.h2d "${BATTLE_DIR}/files/data"
cp "${FHEROES2_DIR}/maps"/*.fh2m "${BATTLE_DIR}/maps"

# Translations are only present once they have been compiled; a build does that for us.
shopt -s nullglob
translations=("${FHEROES2_DIR}/files/lang"/*.mo)
shopt -u nullglob

if [[ "${#translations[@]}" -gt 0 ]]; then
    cp "${translations[@]}" "${BATTLE_DIR}/files/lang"
else
    echo_warn "No compiled translations found; the game will run in English."
fi

echo
echo "Done. Run the game with:  cd ${BATTLE_DIR} && ./fheroes2-battle"
