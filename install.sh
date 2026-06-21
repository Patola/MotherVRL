#!/usr/bin/env bash
#
# install.sh — download, install and Proton-patch MotherVR + GRAND-MotherVR for
# Alien: Isolation in one step.
#
# Usage:
#   ./install.sh [GAME_DIR] [--with-d3dcompiler]
#
#   GAME_DIR            Alien: Isolation install dir.
#                       Default: /games/SteamLibrary/steamapps/common/Alien Isolation
#   --with-d3dcompiler  Also install Microsoft's d3dcompiler_47 into the game's Proton
#                       prefix via protontricks. Without this flag it's only printed as a
#                       manual step (it downloads from the web and modifies the prefix).
#
# What it does: downloads the two mods, drops dxgi.dll + XINPUT1_3.dll (+ grand.ini) into
# the game folder, and runs both patchers (patch_mothervr_proton.py then
# patch_grand_proton.py). The DLLs are installed fresh from the zips before patching, so
# the .bak each patcher writes is always the unmodified original.
#
# Two steps still have to be done by hand (they can't live in a file): the Steam launch
# option, and (once) installing d3dcompiler_47. Both are printed at the end.
set -euo pipefail

# ---- versions / URLs (bump these to update) ---------------------------------
MOTHERVR_URL="https://github.com/Nibre/MotherVR/releases/download/0.8.1/MotherVR.0.8.1.zip"
GRAND_URL="https://alienisolationvr.com/downloads/files/GRAND-Releasev0.6.0-h1.zip"
APPID=214490

# ---- args -------------------------------------------------------------------
GAME=""
RUN_D3DC=0
for a in "$@"; do
  case "$a" in
    --with-d3dcompiler) RUN_D3DC=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) GAME="$a" ;;
  esac
done
GAME="${GAME:-/games/SteamLibrary/steamapps/common/Alien Isolation}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- sanity -----------------------------------------------------------------
command -v unzip   >/dev/null || { echo "error: 'unzip' not found";   exit 1; }
command -v python3 >/dev/null || { echo "error: 'python3' not found"; exit 1; }
if   command -v curl >/dev/null; then DL() { curl -fL -o "$1" "$2"; }
elif command -v wget >/dev/null; then DL() { wget -O "$1" "$2"; }
else echo "error: need curl or wget"; exit 1; fi
[ -f "$GAME/AI.exe" ] || { echo "error: AI.exe not found in '$GAME'.
Pass your Alien: Isolation directory as the first argument."; exit 1; }

# ---- download + extract -----------------------------------------------------
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
echo ">> downloading MotherVR ...";  DL "$WORK/mvr.zip"   "$MOTHERVR_URL"
echo ">> downloading GRAND ...";     DL "$WORK/grand.zip" "$GRAND_URL"
unzip -oq "$WORK/mvr.zip"   -d "$WORK/mvr"
unzip -oq "$WORK/grand.zip" -d "$WORK/grand"

DXGI="$(find "$WORK/mvr"     -iname 'dxgi.dll'      -print -quit)"
XINPUT="$(find "$WORK/grand" -iname 'xinput1_3.dll' -print -quit)"
GRANDINI="$(find "$WORK/grand" -iname 'grand.ini'   -print -quit)"
[ -n "$DXGI"   ] || { echo "error: dxgi.dll not found in MotherVR zip";      exit 1; }
[ -n "$XINPUT" ] || { echo "error: XINPUT1_3.dll not found in GRAND zip";    exit 1; }

# ---- install (fresh, so the patchers' .bak is the stock DLL) -----------------
echo ">> installing into: $GAME"
cp -f "$DXGI"   "$GAME/dxgi.dll"
cp -f "$XINPUT" "$GAME/XINPUT1_3.dll"
if   [ -n "$GRANDINI" ] && [ ! -f "$GAME/grand.ini" ]; then
  cp "$GRANDINI" "$GAME/grand.ini"; echo "   installed grand.ini"
elif [ -f "$GAME/grand.ini" ]; then
  echo "   kept your existing grand.ini (new default is in the GRAND zip if you want to diff it)"
fi
# Note: AIWin11Fix.reg from the GRAND zip is intentionally skipped — not needed on Linux.

# ---- patch ------------------------------------------------------------------
echo ">> patching MotherVR dxgi.dll ..."
python3 "$SCRIPT_DIR/patch_mothervr_proton.py" "$GAME/dxgi.dll" --game-exe "$GAME/AI.exe"
echo ">> patching GRAND XINPUT1_3.dll ..."
python3 "$SCRIPT_DIR/patch_grand_proton.py" "$GAME/XINPUT1_3.dll" --dxgi "$GAME/dxgi.dll"

# ---- d3dcompiler_47 (optional) ----------------------------------------------
if [ "$RUN_D3DC" = 1 ]; then
  if command -v protontricks >/dev/null; then
    echo ">> installing Microsoft d3dcompiler_47 via protontricks ..."
    protontricks "$APPID" d3dcompiler_47
  else
    echo "!! protontricks not found — install it, then run: protontricks $APPID d3dcompiler_47"
  fi
fi

# ---- done -------------------------------------------------------------------
echo
echo "Done. Remaining one-time manual steps:"
echo
echo "  1) Set Alien: Isolation's Steam launch options to:"
echo '         WINEDLLOVERRIDES="dxgi=n,b;XINPUT1_3=n,b" %command%'
echo
if [ "$RUN_D3DC" = 1 ]; then
  echo "  2) Microsoft d3dcompiler_47: already installed above."
else
  echo "  2) Install Microsoft d3dcompiler_47 into the prefix (GRAND needs it):"
  echo "         protontricks $APPID d3dcompiler_47"
  echo "     (or re-run this script with --with-d3dcompiler)"
fi
echo
echo "Then launch under your VR runtime (SteamVR, or WiVRn/xrizer)."
