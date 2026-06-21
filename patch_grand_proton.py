#!/usr/bin/env python3
"""
patch_grand_proton.py — let GRAND-MotherVR (XINPUT1_3.dll) accept a Proton-patched
MotherVR dxgi.dll.

Background
----------
GRAND-MotherVR is an XINPUT1_3.dll proxy that builds on MotherVR. On startup it
CRC32-validates three files and only injects its enhancements if MotherVR matches:

    cmp edi, 0x17C2184B   ; AI.exe          -> "Steam version detected"
    cmp edi, 0xAF2815B9   ; dxgi.dll        -> "MotherVR v0.8.1 detected" -> inject
    cmp edi, 0xFF704276   ; DATA/MOTHER.PAK -> ok

0xAF2815B9 is the CRC of the *stock* MotherVR dxgi.dll. But to run under Proton the
dxgi.dll must be patched (see patch_mothervr_proton.py) — which changes its CRC. GRAND
then reports "Unsupported MotherVR version detected ... Continuing without injecting",
so GRAND loads but does nothing.

Two separate things are needed to run GRAND under Proton:
  1. Launch option  WINEDLLOVERRIDES="xinput1_3=n,b"  — Proton defaults xinput*.dll to
     'builtin', so without this the game-local GRAND XINPUT1_3.dll is never loaded.
     (dxgi already loads native via DXVK, so only xinput needs forcing.)
  2. This patch: replace GRAND's expected MotherVR CRC (0xAF2815B9) with the CRC of the
     *patched* dxgi.dll actually in the game folder, so the integrity check passes and
     GRAND injects. The edi==0 ("MotherVR not found") safety path is preserved.

GRAND's injection targets MotherVR's render code; the dxgi patches are tiny in-place
edits to unrelated init/VR-gate code, so they don't move anything GRAND relies on.

Usage
-----
  python3 patch_grand_proton.py <game>/XINPUT1_3.dll --dxgi <game>/dxgi.dll
(point --dxgi at the already-patched dxgi.dll; its CRC is computed and written in.)
"""
import sys, struct, shutil, zlib, argparse, re

STOCK_MOTHERVR_CRC = 0xAF2815B9
CMP_EDI = b"\x81\xff"                       # cmp edi, imm32

def crc32(path):
    return zlib.crc32(open(path, "rb").read()) & 0xffffffff

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xinput", help="path to GRAND's XINPUT1_3.dll (in the game folder)")
    ap.add_argument("--dxgi", required=True,
                    help="path to the patched MotherVR dxgi.dll in the game folder")
    args = ap.parse_args()

    data = bytearray(open(args.xinput, "rb").read())
    if data[:2] != b"MZ":
        print("error: not a PE file"); sys.exit(1)

    target = crc32(args.dxgi)
    if target == STOCK_MOTHERVR_CRC:
        print("note: the dxgi.dll given is stock (unpatched). It must be patched first "
              "with patch_mothervr_proton.py, or the game will crash under Proton.")

    stock_anchor = CMP_EDI + struct.pack("<I", STOCK_MOTHERVR_CRC)
    done_anchor  = CMP_EDI + struct.pack("<I", target)

    hits = [m.start() for m in re.finditer(re.escape(stock_anchor), data)]
    if not hits:
        if re.search(re.escape(done_anchor), data):
            print(f"Already patched: GRAND's expected MotherVR CRC is 0x{target:08x} "
                  f"(matches the dxgi.dll given). Nothing to do.")
            sys.exit(0)
        print("error: GRAND's MotherVR CRC check (cmp edi,0xAF2815B9) not found. "
              "Layout may have changed."); sys.exit(1)
    if len(hits) != 1:
        print(f"error: expected exactly 1 CRC check, found {len(hits)} "
              f"at {[hex(h) for h in hits]}. Refusing to patch."); sys.exit(1)

    off = hits[0] + len(CMP_EDI)            # offset of the imm32
    shutil.copy2(args.xinput, args.xinput + ".bak")
    before = bytes(data[off:off+4])
    data[off:off+4] = struct.pack("<I", target)
    open(args.xinput, "wb").write(data)
    print(f"Patched GRAND MotherVR-CRC check at file 0x{off:x}: "
          f"{before.hex(' ')} -> {bytes(data[off:off+4]).hex(' ')}  "
          f"(0x{STOCK_MOTHERVR_CRC:08x} -> 0x{target:08x}, the patched dxgi.dll's CRC)")
    print(f"Backup at {args.xinput}.bak")
    print('Remember the launch option:  WINEDLLOVERRIDES="xinput1_3=n,b" %command%')

if __name__ == "__main__":
    main()
