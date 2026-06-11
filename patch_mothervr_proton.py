#!/usr/bin/env python3
"""
patch_mothervr_proton.py — make MotherVR's dxgi.dll survive Wine/Proton's
imagehlp!MapAndLoad behaviour so the game actually launches.

The problem
-----------
MotherVR's DllMain reads two PE TimeDateStamps via imagehlp!MapAndLoad:

  Site 1: MapAndLoad("AI.exe")  -> read AI.exe   TimeDateStamp, compare to the two
          supported builds (0x5FD25676 Steam-2020, 0x54E3199D 2015). Pass => keep VR.
  Site 2: MapAndLoad("dxgi.dll")-> read MotherVR's own TimeDateStamp, convert to a
          FILETIME (ts*1e7 + 116444736e9) for its versioning/logging.

Both sites do, with NO null check:

    8b 44 24 18    mov eax,[esp+0x18]   ; eax = MapAndLoad LOADED_IMAGE result
    8b 70 08       mov esi,[eax+8]      ; esi = FileHeader->TimeDateStamp
    8d 44 24 0c    lea eax,[esp+0xc]
    50             push eax
    ff 15 ........ call UnMapAndLoad

Under Wine/Proton, MapAndLoad does not hand back a usable LOADED_IMAGE pointer the
way native imagehlp does, so eax = NULL and `mov esi,[eax+8]` faults
(EXCEPTION_ACCESS_VIOLATION reading [NULL+8]). The fault is in DllMain, so the
loader aborts the whole process: "dxgi.dll failed to initialize, aborting", and the
game never starts. This — not the dxgi/DXVK name "collision" — is what blocks
MotherVR under Proton.

The fix
-------
At each site overwrite the 7-byte NULL deref with:

    be <timestamp>   mov esi,<correct TimeDateStamp>   (5 bytes)
    eb 0b            jmp +0x0b                          (2 bytes; skips UnMapAndLoad)

i.e. supply the value the check would have read on a genuine Windows install and
jump past the now-pointless UnMapAndLoad (the map failed, there is nothing to
unmap). Site 1 then runs its original comparison and passes; site 2 runs its
original FILETIME conversion with the real value. Behaviour is identical to a
native run; only the fragile, redundant disk re-read of already-loaded modules is
neutralised.

Site 1 timestamp must equal the installed AI.exe's TimeDateStamp (auto-detected
from the game if --game-exe is given, else AI_EXE_TIMESTAMP below).
Site 2 timestamp is MotherVR's own — auto-read from the dll being patched.

Third fix — the "SteamVR not running!" gate
-------------------------------------------
Before VR init, MotherVR snapshots the process list (CreateToolhelp32Snapshot)
and looks for a running `vrmonitor.exe` (SteamVR's monitor process on Windows). If
not found it shows "SteamVR not running! [vrmonitor.exe] ..." and starts with VR
disabled. Under Proton, SteamVR/WiVRn/xrizer run as *native Linux* processes, so
there is never a `vrmonitor.exe` in the wine process list and the check always
fails — even when a runtime is genuinely up. The relevant branch:

    1b c0           sbb eax,eax           ; name-compare result: 0 = vrmonitor found
    83 c8 01        or  eax,1
    85 c0           test eax,eax
    74 36           je  <vr-init success> ; taken only if vrmonitor.exe was found
    ... fall through -> "SteamVR not running!" dialog, VR disabled

We flip that `je` (0x74) to an unconditional `jmp` (0xeb), so it always proceeds to
the real VR init. That init (openvr_api.dll / VR_Init) is the meaningful check: if a
runtime is actually present it succeeds, otherwise it fails honestly — instead of
being gated on a Windows-only process name that can't exist under Proton.
"""
import sys, shutil, struct, argparse

# Accepted AI.exe builds (MotherVR's own whitelist). The installed game must be one.
KNOWN_AI_BUILDS = {0x5FD25676, 0x54E3199D}
AI_EXE_TIMESTAMP = 0x54E3199D  # fallback if the game exe isn't provided

DEREF = bytes.fromhex("8b442418 8b7008".replace(" ", ""))        # mov eax,[esp+0x18]; mov esi,[eax+8]
UNMAP = bytes.fromhex("8d44240c 50 ff15".replace(" ", ""))        # lea eax,[esp+0xc]; push eax; call ...
# Full anchor = DEREF + UNMAP + 4-byte call target (wildcard)
ANCHOR_LEN = len(DEREF) + len(UNMAP) + 4                          # 7 + 6 + 4 = 17

CMP_ESI = b"\x81\xfe"   # cmp esi,imm32  -> site 1 (exe version check)
MOV_ECX = b"\xb9"       # mov ecx,imm32  -> site 2 (own-timestamp -> FILETIME)

# vrmonitor.exe gate: sbb eax,eax ; or eax,1 ; test eax,eax ; je <success>
# Anchor on the fixed prefix; the je's rel8 operand may differ across versions.
VRMON_ANCHOR = bytes.fromhex("1bc0 83c801 85c0".replace(" ", ""))  # then 74 <rel8>


def pe_timestamp(path):
    """Read IMAGE_FILE_HEADER.TimeDateStamp without external deps."""
    with open(path, "rb") as f:
        d = f.read(0x400)
    if d[:2] != b"MZ":
        raise ValueError("not a PE file: " + path)
    e_lfanew = struct.unpack_from("<I", d, 0x3C)[0]
    if d[e_lfanew:e_lfanew+4] != b"PE\0\0":
        raise ValueError("bad PE signature: " + path)
    return struct.unpack_from("<I", d, e_lfanew + 8)[0]


def _e_lfanew(data):
    return struct.unpack_from("<I", data, 0x3C)[0]

def pe_imagebase(data):
    # PE32 ImageBase at OptionalHeader+0x1C (OptionalHeader starts at e_lfanew+0x18)
    return struct.unpack_from("<I", data, _e_lfanew(data) + 0x18 + 0x1C)[0]

def _sections(data):
    e = _e_lfanew(data)
    nsec = struct.unpack_from("<H", data, e + 6)[0]
    opt = struct.unpack_from("<H", data, e + 0x14)[0]   # SizeOfOptionalHeader
    base = e + 0x18 + opt
    out = []
    for i in range(nsec):
        h = base + i * 0x28
        vsz = struct.unpack_from("<I", data, h + 0x08)[0]
        va  = struct.unpack_from("<I", data, h + 0x0C)[0]
        rsz = struct.unpack_from("<I", data, h + 0x10)[0]
        ptr = struct.unpack_from("<I", data, h + 0x14)[0]
        out.append((va, vsz, ptr, rsz))
    return out

def pe_rva_of_offset(data, off):
    for va, vsz, ptr, rsz in _sections(data):
        if ptr <= off < ptr + rsz:
            return off - ptr + va
    raise ValueError("file offset 0x%x not in any section" % off)

def pe_find_widestr_offset(data, s):
    enc = s.encode("utf-16-le")
    i = data.find(enc)
    return i if i >= 0 else None


def make_patch(ts):
    # mov esi,ts ; jmp +0x0b  (the jmp skips the 11-byte lea+push+call UnMapAndLoad)
    return b"\xbe" + struct.pack("<I", ts) + b"\xeb\x0b"


def find_sites(data):
    sites, start = [], 0
    while True:
        i = data.find(DEREF, start)
        if i < 0:
            break
        start = i + 1
        # require the UnMapAndLoad shape right after the deref
        if data[i+len(DEREF):i+len(DEREF)+len(UNMAP)] != UNMAP:
            continue
        cont = data[i+ANCHOR_LEN:i+ANCHOR_LEN+2]
        if cont[:2] == CMP_ESI:
            sites.append((i, "exe-check"))
        elif cont[:1] == MOV_ECX:
            sites.append((i, "own-ts"))
        else:
            sites.append((i, "unknown"))
    return sites


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dll", help="path to MotherVR dxgi.dll to patch (in the game folder)")
    ap.add_argument("--game-exe", help="path to AI.exe, to auto-detect the accepted build timestamp")
    args = ap.parse_args()

    data = bytearray(open(args.dll, "rb").read())
    if data[:2] != b"MZ":
        print("error: not a PE file"); sys.exit(1)

    own_ts = pe_timestamp(args.dll)
    ai_ts = AI_EXE_TIMESTAMP
    if args.game_exe:
        ai_ts = pe_timestamp(args.game_exe)
        if ai_ts not in KNOWN_AI_BUILDS:
            print(f"warning: AI.exe timestamp 0x{ai_ts:08x} is not in MotherVR's known build "
                  f"whitelist {{0x5fd25676,0x54e3199d}} — forcing the check to accept it anyway.")

    sites = find_sites(data)
    exe_sites = [s for s in sites if s[1] == "exe-check"]
    own_sites = [s for s in sites if s[1] == "own-ts"]

    already = (data.find(b"\xbe" + struct.pack("<I", ai_ts) + b"\xeb\x0b") >= 0 and
               data.find(b"\xbe" + struct.pack("<I", own_ts) + b"\xeb\x0b") >= 0)
    if not sites and already:
        print("Looks already patched (both 'mov esi,imm32; jmp +0x0b' present). Nothing to do.")
        sys.exit(0)
    if len(exe_sites) != 1 or len(own_sites) != 1:
        print(f"error: expected exactly 1 exe-check site and 1 own-ts site, found "
              f"{len(exe_sites)} and {len(own_sites)} (all: {[(hex(o),k) for o,k in sites]}). "
              "Refusing to patch — MotherVR layout may have changed.")
        sys.exit(1)

    # vrmonitor.exe gate. The sbb/or/test idiom is not unique, so anchor on the
    # unique `mov eax, <VA of the "vrmonitor.exe" wide string>` and find the je that
    # follows the name-compare loop within a short window.
    vmon_str_off = pe_find_widestr_offset(data, "vrmonitor.exe")
    real_vmon, done_vmon = [], []
    if vmon_str_off is not None:
        va = pe_imagebase(data) + pe_rva_of_offset(data, vmon_str_off)
        mov_eax = b"\xb8" + struct.pack("<I", va)       # mov eax, <strVA>
        m = data.find(mov_eax)
        if m >= 0:
            win = data[m:m+0x60]
            k = win.find(VRMON_ANCHOR)                  # sbb/or/test just before the je
            if k >= 0:
                je_off = m + k + len(VRMON_ANCHOR)
                if data[je_off] == 0x74:
                    real_vmon.append(je_off)
                elif data[je_off] == 0xeb:
                    done_vmon.append(je_off)
    if len(real_vmon) != 1 and not (len(real_vmon) == 0 and done_vmon):
        print(f"error: could not uniquely locate the vrmonitor je gate "
              f"(found {len(real_vmon)} unpatched, {len(done_vmon)} already-patched). "
              "Refusing to patch.")
        sys.exit(1)

    shutil.copy2(args.dll, args.dll + ".bak")
    plan = [(exe_sites[0][0], ai_ts, "exe-check"), (own_sites[0][0], own_ts, "own-ts")]
    for off, ts, kind in plan:
        patch = make_patch(ts)
        before = bytes(data[off:off+7])
        data[off:off+7] = patch
        print(f"[{kind:9}] file 0x{off:06x}: {before.hex(' ')} -> {patch.hex(' ')}  "
              f"(mov esi,0x{ts:08x}; jmp +0x0b)")
    if real_vmon:
        off = real_vmon[0]
        before = bytes(data[off:off+2])
        data[off] = 0xeb                               # je -> jmp (always proceed to VR init)
        print(f"[vrmonitor] file 0x{off:06x}: {before.hex(' ')} -> {bytes(data[off:off+2]).hex(' ')}  "
              f"(je -> jmp: skip the 'SteamVR not running' gate)")
    open(args.dll, "wb").write(data)
    print(f"Done. Backup at {args.dll}.bak")


if __name__ == "__main__":
    main()
