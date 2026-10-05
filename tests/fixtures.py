"""Build a synthetic mod library for the tests - no game files or real mods needed."""
import os, io, json, struct, zipfile, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lol3d  # noqa: E402


def wad_bytes(files):
    """files: {path_or_hash: bytes} -> minimal WAD v3.4 (uncompressed entries)."""
    items = []
    for k, data in files.items():
        h = k if isinstance(k, int) else lol3d.path_hash(k)
        items.append((h, data))
    items.sort()
    count = len(items)
    header = b"RW\x03\x04" + b"\0" * 256 + struct.pack("<Q", 0) + struct.pack("<I", count)
    toc_size = 32 * count
    offset = len(header) + toc_size
    toc, body = b"", b""
    for h, data in items:
        toc += struct.pack("<QIIIBBHQ", h, offset + len(body), len(data), len(data), 0, 0, 0, 0)
        body += data
    return header + toc + body


def skn_bytes(nverts=3):
    """Tiny valid SKN v4.1 mesh (one submesh, one triangle)."""
    sub = b"Body".ljust(64, b"\0") + struct.pack("<4I", 0, nverts, 0, 3)
    out = struct.pack("<IHH", 0x00112233, 4, 1) + struct.pack("<I", 1) + sub + struct.pack("<I", 0)
    out += struct.pack("<II", 3, nverts) + struct.pack("<II", 52, 0) + b"\0" * 40
    out += struct.pack("<3H", 0, 1, 2) + b"\0\0"[:0]
    for i in range(nverts):
        out += struct.pack("<3f", float(i), float(i * 2), 0.0) + b"\0" * 4 + struct.pack("<4f", 1, 0, 0, 0) \
               + struct.pack("<3f", 0, 0, 1) + struct.pack("<2f", 0.5, 0.5)
    return out


def real_path(champ, skin, ext=".skn"):
    """A file path the game really uses for that champion/skin (so the hash table knows it)."""
    folder = "base" if skin == 0 else f"skin{skin:02d}"
    for p in sorted(lol3d.names().values()):
        if p.lower().startswith(f"assets/characters/{champ}/skins/{folder}/") and p.lower().endswith(ext):
            return p.lower()
    return f"assets/characters/{champ}/skins/{folder}/{champ}_{folder}{ext}"


DDS = b"DDS " + struct.pack("<I", 124) + b"\0" * 120 + b"\0" * 64
SKL = b"\0\0\0\0" + b"\xc3\x4f\xfd\x22" + b"\0" * 64


def fantome(path, name, wad_files=None, raw_files=None, wad_name="Ahri.wad.client", meta=True, author="Tester"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        if meta:
            z.writestr("META/info.json", json.dumps({"Name": name, "Author": author, "Version": "1.0", "Description": ""}))
        if wad_files:
            z.writestr(f"WAD/{wad_name}", wad_bytes(wad_files))
        for rel, data in (raw_files or {}).items():
            z.writestr(f"RAW/{rel}", data)


def build(lib):
    """Create the test library. Returns a dict of expectations used by the tests."""
    A = "assets/characters/ahri/skins"
    D = "data/characters/ahri/skins"
    exp = {}
    # 1. full custom skin: ships skin14.bin + skin14 model/texture  -> Star Guardian Ahri (14), via its bin
    fantome(os.path.join(lib, "Ahri", "SG Ahri custom.fantome"), "SG Ahri custom",
            {f"{D}/skin14.bin": b"PROP" + b"\0" * 16, f"{A}/skin14/ahri_skin14.skn": skn_bytes(),
             f"{A}/skin14/ahri_skin14_tx_cm.tex": b"TEX\0" + b"\0" * 32})
    exp["SG Ahri custom"] = ("Ahri", 14)
    # 2. textures + model in the base folder plus lots of base animations, packed as RAW in a zip -> Default (0)
    raw = {f"{A}/base/ahri_base.skn": skn_bytes(), f"{A}/base/ahri_base_tx_cm.dds": DDS}
    for i in range(10):
        raw[f"{A}/base/animations/ahri_idle{i}.anm"] = b"r3d2anmd" + b"\0" * 20
    fantome(os.path.join(lib, "SKIN", "Ahri Base Remodel.zip"), "Ahri Base Remodel", raw_files=raw)
    exp["Ahri Base Remodel"] = ("Ahri", 0)
    # 3. model in skin07 folder, many animations in base -> folder rule picks 7 (model beats animations)
    raw = {f"assets/characters/katarina/skins/skin07/katarina_xmas.skn": skn_bytes(),
           f"assets/characters/katarina/skins/skin07/katarina_xmas_tx_cm.dds": DDS}
    for i in range(20):
        raw[f"assets/characters/katarina/skins/base/animations/kat_{i}.anm"] = b"r3d2anmd" + b"\0" * 20
    fantome(os.path.join(lib, "Katarina", "Kat Skin07 port.zip"), "Kat Skin07 port", raw_files=raw)
    exp["Kat Skin07 port"] = ("Katarina", 7)
    # 4. a zip that just wraps one .fantome
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META/info.json", json.dumps({"Name": "Wrapped Lux", "Author": "X", "Version": "1"}))
        z.writestr("WAD/Lux.wad.client", wad_bytes({"assets/characters/lux/skins/skin07/lux_skin07.skn": skn_bytes()}))
    os.makedirs(os.path.join(lib, "Lux"), exist_ok=True)
    with zipfile.ZipFile(os.path.join(lib, "Lux", "Wrapped Lux download.zip"), "w") as z:
        z.writestr("Wrapped Lux.fantome", buf.getvalue())
    exp["Wrapped Lux"] = ("Lux", 7)
    # 5. pack: zip with two fantomes for different champions
    os.makedirs(os.path.join(lib, "SKIN"), exist_ok=True)
    with zipfile.ZipFile(os.path.join(lib, "SKIN", "Big Pack.zip"), "w") as z:
        z.writestr("Pack/A.fantome", buf.getvalue())
        b2 = io.BytesIO()
        with zipfile.ZipFile(b2, "w") as z2:
            z2.writestr("META/info.json", json.dumps({"Name": "Pack Jinx", "Author": "X", "Version": "1"}))
            z2.writestr("WAD/Jinx.wad.client", wad_bytes({"assets/characters/jinx/skins/base/jinx.skn": skn_bytes()}))
        z.writestr("Pack/B.fantome", b2.getvalue())
    # 6. corrupt archive, unreadable .rar, voice WAD, loading screen
    open(os.path.join(lib, "Ahri", "broken.fantome"), "wb").write(b"PK\x03\x04garbage")
    open(os.path.join(lib, "Ahri", "something.rar"), "wb").write(b"Rar!\x1a\x07\x00")
    os.makedirs(os.path.join(lib, "Ahri"), exist_ok=True)
    open(os.path.join(lib, "Ahri", "Ahri.en_US.wad.client"), "wb").write(wad_bytes({"assets/sounds/wwise2016/vo/en_us/characters/ahri/skins/base/ahri_base_vo_audio.wpk": b"r3d2" + b"\0" * 8}))
    # 7. exact duplicate + extracted folder next to its archive
    import shutil
    shutil.copy2(os.path.join(lib, "Ahri", "SG Ahri custom.fantome"), os.path.join(lib, "Ahri", "SG Ahri custom (1).fantome"))
    ext = os.path.join(lib, "Ahri", "SG Ahri custom")
    with zipfile.ZipFile(os.path.join(lib, "Ahri", "SG Ahri custom.fantome")) as z:
        z.extractall(ext)
    # 8. mod sitting in the wrong champion folder
    fantome(os.path.join(lib, "Ahri", "Jinx in wrong folder.fantome"), "Jinx thing",
            {real_path("jinx", 4): skn_bytes()}, wad_name="Jinx.wad.client")
    exp["Jinx thing"] = ("Jinx", 4)
    # 9. champion folder with a mod extracted straight into it (loose META/WAD) next to real mods
    xa = os.path.join(lib, "Xayah")
    os.makedirs(os.path.join(xa, "META"), exist_ok=True); os.makedirs(os.path.join(xa, "WAD"), exist_ok=True)
    json.dump({"Name": "loose"}, open(os.path.join(xa, "META", "info.json"), "w"))
    open(os.path.join(xa, "WAD", "Xayah.wad.client"), "wb").write(wad_bytes({"assets/characters/xayah/skins/base/xayah.skn": skn_bytes()}))
    fantome(os.path.join(xa, "Xayah BA.fantome"), "Xayah BA", {real_path("xayah", 5, ".dds"): DDS}, wad_name="Xayah.wad.client")
    fantome(os.path.join(xa, "Xayah SG.fantome"), "Xayah SG", {real_path("xayah", 8, ".dds"): DDS}, wad_name="Xayah.wad.client")
    exp["Xayah BA"] = ("Xayah", 5)
    # 10. no champion clues at all, odd META json (BOM + trailing comma), unicode name
    with zipfile.ZipFile(os.path.join(lib, "SKIN", "mystery.zip"), "w") as z:
        z.writestr("META/info.json", "﻿{\"Name\": \"신비\", \"Author\": \"?\",}")
        z.writestr("RAW/readme.txt", "hi")
    # 11. raw game files that were never packaged
    os.makedirs(os.path.join(lib, "Fiora"), exist_ok=True)
    with zipfile.ZipFile(os.path.join(lib, "Fiora", "fiora.zip"), "w") as z:
        z.writestr("Animations/fiora_attack1.anm", b"r3d2anmd")
        z.writestr("fiora_base.skn", skn_bytes())
    # 12. name-only detection: "Miss Fortune Skin04" with base files
    fantome(os.path.join(lib, "SKIN", "Miss Fortune Skin04.zip"), "Miss Fortune Skin04",
            raw_files={"assets/characters/missfortune/skins/skin04/mf_skin04.skn": skn_bytes()})
    exp["Miss Fortune Skin04"] = ("MissFortune", 4)
    return exp
