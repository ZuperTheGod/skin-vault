"""
fixer - auto-fix outdated League skin mods using LtMAO-hai's tools.

What "fix" does for one mod:
  1. unpack every WAD inside the mod with LtMAO (real file names via its hash tables)
  2. look up what the CURRENT game loads for the target skin (mesh, skeleton, textures per part)
  3. put the mod's mesh / skeleton / textures at exactly those paths, so the game actually uses them
  4. convert textures to Riot's TEX format with LtMAO (dds -> tex)
  5. if the mod ships its own .bin files that still point at .dds textures, convert those textures
     and rewrite the bins to point at the .tex files (LtMAO's ritobin)
  6. repack with LtMAO and save "<name> (fixed).fantome"; the original is kept in _Originals
"""
import os, re, io, json, time, shutil, zipfile, subprocess, struct, uuid
import lol3d

DEFAULT_LTMAO = [r"D:\LeaguePrograms\LtMAO-hai\LtMAO-hai", r"C:\LeaguePrograms\LtMAO-hai\LtMAO-hai"]
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

def _is_ltmao(d):
    return bool(d) and os.path.exists(os.path.join(d, "src", "cli.py")) and os.path.exists(os.path.join(d, "cpy", "python.exe"))

_LTMAO_CACHE = {}

def find_ltmao(cfg_dir=None):
    """Find an LtMAO-hai install: the configured folder, common locations, or a quick search of a few
    usual parent folders (Downloads, Desktop, Documents, drive roots) two levels deep."""
    if _is_ltmao(cfg_dir):
        return cfg_dir
    if "found" in _LTMAO_CACHE:
        return _LTMAO_CACHE["found"]
    found = next((d for d in DEFAULT_LTMAO if _is_ltmao(d)), None)
    if not found:
        home = os.path.expanduser("~")
        bases = [os.path.join(home, x) for x in ("Downloads", "Desktop", "Documents", "")] + \
                [f"{d}:\\" for d in "CDEFG"] + [f"{d}:\\LeaguePrograms" for d in "CDEFG"]
        for b in bases:
            try:
                for n in os.listdir(b):
                    if "ltmao" not in n.lower():
                        continue
                    p = os.path.join(b, n)
                    for cand in (p, os.path.join(p, n)) + tuple(os.path.join(p, x) for x in (os.listdir(p) if os.path.isdir(p) else [])):
                        if _is_ltmao(cand):
                            found = cand; break
                    if found:
                        break
            except Exception:
                continue
            if found:
                break
    _LTMAO_CACHE["found"] = found
    return found

class LtMAO:
    def __init__(self, root):
        self.root = root
        self.py = os.path.join(root, "cpy", "python.exe")
        self.cli = os.path.join(root, "src", "cli.py")
        self.ritobin = os.path.join(root, "res", "tools", "ritobin_cli.exe")
        self.hashes = os.path.join(root, "pref", "hashes", "custom_hashes")

    def run(self, tool, src, dst=None, timeout=900):
        cmd = [self.py, self.cli, "-t", tool, "-src", src] + (["-dst", dst] if dst else [])
        r = subprocess.run(cmd, cwd=self.root, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=timeout, creationflags=NO_WINDOW, errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        if r.returncode != 0 or "Traceback" in out:
            raise RuntimeError(f"LtMAO {tool} failed: " + out.strip()[-600:])
        return out

    def dds_to_tex(self, dds_src, workdir):
        """Convert one DDS to TEX. Formats TEX can't hold (DXT3, BC7, ...) are re-encoded to DXT5 with
        LtMAO's ImageMagick first. Returns path of the .tex or raises."""
        os.makedirs(workdir, exist_ok=True)
        tmp = os.path.join(workdir, "in.dds")
        shutil.copy2(dds_src, tmp)
        try:
            self.run("dds2tex", tmp)
            return os.path.join(workdir, "in.tex")
        except Exception as first:
            magick = os.path.join(self.root, "res", "tools", "magick.exe")
            if not os.path.exists(magick):
                raise
            tmp2 = os.path.join(workdir, "re.dds")
            r = subprocess.run([magick, tmp, "-define", "dds:compression=dxt5", tmp2], cwd=self.root,
                               capture_output=True, text=True, timeout=300, creationflags=NO_WINDOW, errors="replace")
            if not os.path.exists(tmp2):
                raise RuntimeError(f"{first} / ImageMagick: {(r.stdout or '') + (r.stderr or '')}"[-500:])
            self.run("dds2tex", tmp2)
            return os.path.join(workdir, "re.tex")

    def bin_to_text(self, src, dst):
        self._ritobin(src, dst)

    def text_to_bin(self, src, dst):
        self._ritobin(src, dst)

    def _ritobin(self, src, dst):
        cmd = [self.ritobin, src, dst]
        if os.path.isdir(self.hashes):
            cmd += ["--dir-hashes", self.hashes]
        r = subprocess.run(cmd, cwd=self.root, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=300, creationflags=NO_WINDOW, errors="replace")
        if r.returncode != 0 or not os.path.exists(dst):
            raise RuntimeError("ritobin failed: " + ((r.stdout or "") + (r.stderr or "")).strip()[-400:])

# ------------------------------------------------------------------ helpers
def kind_of(path):
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except Exception:
        return None
    if head[:4] == b"\x33\x22\x11\x00":
        return "skn"
    if head[:8] == b"r3d2sklt" or head[4:8] == b"\xc3\x4f\xfd\x22":
        return "skl"
    if head[:4] == b"DDS ":
        return "dds"
    if head[:4] == b"TEX\0":
        return "tex"
    if head[:4] in (b"PROP", b"PTCH"):
        return "bin"
    return None

def stem_key(path):
    """'assets/.../2x_Ahri_Base_TX_CM.skins_ahri_asu_prepro.dds' -> 'ahri_base_tx_cm'"""
    b = os.path.basename(path).lower()
    b = re.sub(r"\.(dds|tex|skn|skl|png)$", "", b)
    b = re.sub(r"\.skins_[a-z0-9_]+$", "", b)
    b = re.sub(r"^(2x_|4x_)", "", b)
    return b

def res_rank(path):
    b = os.path.basename(path).lower()
    return 0 if b.startswith(("2x_", "4x_")) else 1

BAD_TEX = re.compile(r"(_n|_normal|_mask|_glow|_emissive|_ao|_spec|_gloss|_rough|_metal|_fresnel|_ramp|_noise|_dist|_erode|_flipbook|_sq|_circle)$|recall|particles|loadscreen|/hud/|/icons?/", re.I)

def tokens(s):
    return [t for t in re.split(r"[^a-z0-9]+", s.lower()) if len(t) > 2 and t not in ("mat", "skin", "base", "material")]

def name_str(v):
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return lol3d.name_of(v) or f"{v:016x}"

# ------------------------------------------------------------------ main
def fix_mod(mod_path, champ_id, slot, game_dir, ltmao_root, work_root, out_path, info_suffix=" (fixed)", log=print):
    """Returns report dict. Raises on fatal problems."""
    lt = LtMAO(ltmao_root)
    report = {"steps": [], "mapped": [], "converted": 0, "bins_patched": 0, "warnings": []}
    step = lambda s: (report["steps"].append(s), log(s))
    W = os.path.join(work_root, f"fix-{uuid.uuid4().hex[:8]}")
    src = os.path.join(W, "mod")
    os.makedirs(src, exist_ok=True)
    try:
        # ---- 1. extract
        low = mod_path.lower()
        if os.path.isdir(mod_path):
            shutil.copytree(mod_path, src, dirs_exist_ok=True)
        elif low.endswith((".fantome", ".zip")):
            with zipfile.ZipFile(mod_path) as zf:
                names = [n.replace("\\", "/") for n in zf.namelist()]
                inner = [n for n in names if n.lower().endswith((".fantome", ".zip"))]
                structured = any(re.search(r"(^|/)(wad|raw|meta)/", n.lower()) for n in names)
                if len(inner) == 1 and not structured:
                    with zipfile.ZipFile(io.BytesIO(zf.read(zf.namelist()[names.index(inner[0])]))) as z2:
                        _extract(z2, src)
                else:
                    _extract(zf, src)
        elif low.endswith((".wad.client", ".wad")):
            os.makedirs(os.path.join(src, "WAD"), exist_ok=True)
            shutil.copy2(mod_path, os.path.join(src, "WAD", os.path.basename(mod_path)))
        else:
            raise RuntimeError("Unsupported mod format")
        # find mod root (folder with META or WAD)
        root = src
        for dp, dn, fn in os.walk(src):
            dl = [d.lower() for d in dn]
            if "meta" in dl or "wad" in dl or "raw" in dl:
                root = dp; break
        step("Extracted mod")

        # ---- 2. unpack WADs with LtMAO
        wad_dir = os.path.join(root, "WAD")
        os.makedirs(wad_dir, exist_ok=True)
        game_wad = lol3d.game_wad(game_dir, champ_id)
        if not game_wad:
            raise RuntimeError("Couldn't find the champion's game files - check the League game folder setting")
        champ_wad_name = os.path.basename(game_wad.name)[: -len(".client")]  # e.g. Ahri.wad
        for f in list(os.listdir(wad_dir)):
            p = os.path.join(wad_dir, f)
            if os.path.isfile(p) and f.lower().endswith(".wad.client"):
                dst = os.path.join(wad_dir, f[: -len(".client")])
                lt.run("wadunpack", p, dst)
                os.remove(p)
                step(f"Unpacked {f} with LtMAO")
            elif os.path.isdir(p) and f.lower().endswith(".wad.client"):
                os.rename(p, os.path.join(wad_dir, f[: -len(".client")]))
        # RAW files go into the champion WAD
        raw = os.path.join(root, "RAW")
        cw = None
        for f in os.listdir(wad_dir):
            if f.lower() == champ_wad_name.lower():
                cw = os.path.join(wad_dir, f)
        if not cw:
            cw = os.path.join(wad_dir, champ_wad_name); os.makedirs(cw, exist_ok=True)
        if os.path.isdir(raw):
            for dp, dn, fn in os.walk(raw):
                for f in fn:
                    s = os.path.join(dp, f)
                    d = os.path.join(cw, os.path.relpath(s, raw))
                    os.makedirs(os.path.dirname(d), exist_ok=True); shutil.move(s, d)
            shutil.rmtree(raw, ignore_errors=True)
            step("Moved RAW files into the champion WAD")

        # ---- 3. inventory (resolve hash-named files to real names when we know them)
        files = []
        for dp, dn, fn in os.walk(cw):
            for f in fn:
                full = os.path.join(dp, f)
                rel = os.path.relpath(full, cw).replace("\\", "/")
                m = re.match(r"^([0-9a-fA-F]{16})(\.\w+)?$", rel)
                if m:
                    nm = lol3d.name_of(int(m.group(1), 16))
                    if nm:
                        dst = os.path.join(cw, nm.lower())
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        if not os.path.exists(dst):
                            shutil.move(full, dst); full = dst; rel = nm.lower()
                files.append((rel, full, kind_of(full)))
        skns = [x for x in files if x[2] == "skn"]
        skls = [x for x in files if x[2] == "skl"]
        texs = [x for x in files if x[2] in ("dds", "tex") and not BAD_TEX.search(stem_key(x[0]))]
        bins = [x for x in files if x[2] == "bin"]
        step(f"Found {len(skns)} model(s), {len(texs)} texture(s), {len(bins)} bin file(s)")

        # ---- 4. what the current game loads for this skin
        c = champ_id.lower()
        gb = lol3d.BinReader(game_wad.read(lol3d.path_hash(f"data/characters/{c}/skins/skin{slot}.bin")))
        bins_all = [gb]
        for lk in gb.linked[:40]:
            h = lol3d.path_hash(lk)
            if h in game_wad.entries:
                try:
                    bins_all.append(lol3d.BinReader(game_wad.read(h)))
                except Exception:
                    pass
        smp, _ = lol3d.find_skin_mesh(bins_all)
        if not smp:
            raise RuntimeError(f"Skin {slot} has no model data in the current game")
        H = lol3d.H
        g_mesh = name_str(smp.get(H["simpleSkin"]))
        g_skl = name_str(smp.get(H["skeleton"]))
        g_tex = smp.get(H["texture"]) or (lol3d.material_texture(bins_all, smp[H["material"]]) if smp.get(H["material"]) else None)
        g_tex = name_str(g_tex)
        g_over = []
        for o in smp.get(H["materialOverride"]) or []:
            if not isinstance(o, dict):
                continue
            t = o.get(H["texture"]) or (lol3d.material_texture(bins_all, o[H["material"]]) if o.get(H["material"]) else None)
            if t and o.get(H["submesh"]):
                g_over.append((o[H["submesh"]], name_str(t)))

        def place(src_full, game_path, what):
            if not game_path:
                return False
            if re.fullmatch(r"[0-9a-f]{16}", game_path):
                dst = os.path.join(cw, game_path + os.path.splitext(src_full)[1])
            else:
                dst = os.path.join(cw, game_path.lower().replace("/", os.sep))
            if os.path.normcase(os.path.abspath(dst)) == os.path.normcase(os.path.abspath(src_full)):
                return False
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src_full, dst)
            report["mapped"].append({"what": what, "from": os.path.relpath(src_full, cw).replace("\\", "/"), "to": game_path})
            return True

        own_bin = any(r.lower() == f"data/characters/{c}/skins/skin{slot}.bin" for r, f, k in files)
        if own_bin:
            step(f"The mod has its own skin{slot}.bin (a full custom skin) - keeping its file layout")
            skns_map, texs_map = [], []
        else:
            skns_map, texs_map = skns, texs
        # ---- 5. mesh + skeleton
        mesh_pick = None
        if skns_map:
            slot_dir = f"/skin{slot:02d}/" if slot else "/base/"
            mesh_pick = max(skns, key=lambda x: (slot_dir in "/" + x[0], os.path.getsize(x[1])))
            place(mesh_pick[1], g_mesh, "model")
            if skls:
                d = os.path.dirname(mesh_pick[0])
                skl_pick = max(skls, key=lambda x: (os.path.dirname(x[0]) == d, os.path.getsize(x[1])))
                if place(skl_pick[1], g_skl, "skeleton"):
                    report["warnings"].append("Replaced the skeleton with the mod's - animations come from the current game, "
                                              "so if the model moves oddly in game, the mod's skeleton is too old for them.")
            else:
                report["warnings"].append("The mod has a model but no skeleton - using the game's skeleton")

        # ---- 6. textures: pick a mod texture for every texture the game skin loads
        targets = []
        if own_bin:
            pass
        elif g_tex:
            targets.append(("main", g_tex))
        for sub, t in ([] if own_bin else g_over):
            targets.append((sub, t))
        conv_dir = os.path.join(W, "conv"); os.makedirs(conv_dir, exist_ok=True)
        to_convert = []   # (dds_src, game_path, label)
        used = set()
        mesh_folder = os.path.dirname(mesh_pick[0]) if mesh_pick else None
        for label, gpath in targets:
            want = stem_key(gpath) if not re.fullmatch(r"[0-9a-f]{16}", gpath or "") else None
            cands = []
            for rel, full, k in texs:
                score = 0
                sk = stem_key(rel)
                if want and sk == want:
                    score = 100
                elif want and (sk.endswith(want) or want.endswith(sk)) and len(sk) > 6:
                    score = 60
                elif label != "main" and any(t in sk for t in tokens(label)):
                    score = 40
                elif label == "main" and ("tx_cm" in sk or "diffuse" in sk or "_cm" in sk):
                    score = 20 + (10 if mesh_folder and os.path.dirname(rel) == mesh_folder else 0)
                if score:
                    cands.append((score, res_rank(rel), os.path.getsize(full), rel, full, k))
            if not cands and label == "main" and mesh_pick and texs:
                # model replaced but nothing named like a diffuse texture -> biggest texture next to the model
                near = [(0, res_rank(r), os.path.getsize(f), r, f, k) for r, f, k in texs
                        if os.path.dirname(r) == mesh_folder] or \
                       [(0, res_rank(r), os.path.getsize(f), r, f, k) for r, f, k in texs]
                cands = near
            if not cands:
                continue
            best = max(cands)
            _, _, _, rel, full, k = best
            if gpath.lower().endswith(".dds") or k == "tex":
                place(full, gpath, f"texture ({label})")
            else:
                to_convert.append((full, gpath, label))
            used.add(rel)
        if to_convert:
            for i, (full, gpath, label) in enumerate(to_convert):
                try:
                    tex = lt.dds_to_tex(full, os.path.join(conv_dir, f"t{i}"))
                except Exception as e:
                    tex = None
                    report["warnings"].append(f"Couldn't convert {os.path.basename(full)} to TEX: {str(e)[-160:]}")
                if tex and os.path.exists(tex):
                    if place(tex, gpath, f"texture ({label}) converted to TEX"):
                        report["mapped"][-1]["from"] = os.path.relpath(full, cw).replace("\\", "/") + "  (DDS -> TEX)"
                    report["converted"] += 1
            step(f"Converted {report['converted']} texture(s) to TEX with LtMAO")

        # ---- 7. mod's own bins that still point at .dds files
        if bins:
            dds_files = [f for r, f, k in files if k == "dds"]
            patched = 0
            for rel, full, k in bins:
                txt = full + ".py"
                try:
                    lt.bin_to_text(full, txt)
                    t = open(txt, encoding="utf-8", errors="replace").read()
                except Exception as e:
                    report["warnings"].append(f"Couldn't read {os.path.basename(rel)}: {e}")
                    continue
                refs = set(re.findall(r'"([^"]+?)\.dds"', t, re.I))
                if not refs:
                    os.remove(txt); continue
                changed = 0
                for r in refs:
                    tex_path = r + ".tex"
                    local_dds = os.path.join(cw, (r + ".dds").lower().replace("/", os.sep))
                    local_tex = os.path.join(cw, tex_path.lower().replace("/", os.sep))
                    if os.path.exists(local_dds) and not os.path.exists(local_tex):
                        try:
                            tx = lt.dds_to_tex(local_dds, os.path.join(W, f"bconv{len(report['steps'])}_{changed}_{abs(hash(r))}"))
                            os.makedirs(os.path.dirname(local_tex), exist_ok=True)
                            shutil.copy2(tx, local_tex); report["converted"] += 1
                        except Exception as e:
                            report["warnings"].append(f"Couldn't convert {os.path.basename(local_dds)}: {str(e)[-120:]}")
                    in_game = lol3d.path_hash(tex_path) in game_wad.entries
                    if os.path.exists(local_tex) or in_game:
                        t = re.sub(r'"' + re.escape(r) + r'\.dds"', '"' + r + '.tex"', t, flags=re.I)
                        changed += 1
                if changed:
                    open(txt, "w", encoding="utf-8").write(t)
                    lt.text_to_bin(txt, full)
                    patched += 1
                os.remove(txt)
            report["bins_patched"] = patched
            if patched:
                step(f"Pointed {patched} bin file(s) at the TEX textures (ritobin)")

        if not report["mapped"] and not report["bins_patched"]:
            report["warnings"].append("Nothing needed changing - the mod's files already match what the game loads, "
                                      "or it doesn't contain a model/texture for this skin.")

        # ---- 8. repack with LtMAO
        for f in list(os.listdir(wad_dir)):
            p = os.path.join(wad_dir, f)
            if os.path.isdir(p) and f.lower().endswith(".wad"):
                lt.run("wadpack", p, p + ".client")
                shutil.rmtree(p)
        step("Repacked with LtMAO")
        meta_dir = os.path.join(root, "META"); os.makedirs(meta_dir, exist_ok=True)
        ip = os.path.join(meta_dir, "info.json")
        try:
            info = json.load(open(ip, encoding="utf-8-sig"))
        except Exception:
            info = {"Author": "", "Description": "", "Name": os.path.splitext(os.path.basename(mod_path))[0], "Version": "1.0"}
        if not str(info.get("Name", "")).endswith(info_suffix):
            info["Name"] = f'{info.get("Name") or "Mod"}{info_suffix}'
        info["Description"] = (info.get("Description") or "") + f" [auto-fixed for skin {slot} by Skin Vault + LtMAO]"
        json.dump(info, open(ip, "w", encoding="utf-8"), indent=4)
        tmp = out_path + ".tmp"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for dp, dn, fn in os.walk(root):
                for f in fn:
                    full = os.path.join(dp, f)
                    z.write(full, os.path.relpath(full, root).replace("\\", "/"))
        os.replace(tmp, out_path)
        step("Saved " + os.path.basename(out_path))
        return report
    finally:
        shutil.rmtree(W, ignore_errors=True)

def _extract(zf, dst):
    for i in zf.infolist():
        n = i.filename.replace("\\", "/")
        if i.is_dir() or n.startswith("/") or ".." in n.split("/"):
            continue
        p = os.path.join(dst, *n.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with zf.open(i) as s, open(p, "wb") as d:
            shutil.copyfileobj(s, d)

def unpack_mod(mod_path, ltmao_root, out_dir):
    """Unpack a mod into a readable folder (real file names) with LtMAO."""
    lt = LtMAO(ltmao_root)
    os.makedirs(out_dir, exist_ok=True)
    low = mod_path.lower()
    if low.endswith((".fantome", ".zip")):
        with zipfile.ZipFile(mod_path) as zf:
            _extract(zf, out_dir)
    elif low.endswith(".wad.client"):
        os.makedirs(os.path.join(out_dir, "WAD"), exist_ok=True)
        shutil.copy2(mod_path, os.path.join(out_dir, "WAD", os.path.basename(mod_path)))
    for dp, dn, fn in os.walk(out_dir):
        for f in fn:
            if f.lower().endswith(".wad.client"):
                p = os.path.join(dp, f)
                lt.run("wadunpack", p, p[: -len(".client")])
                os.remove(p)
    return out_dir


# ------------------------------------------------------------------ .zip -> .fantome
def zip_to_fantome(zip_path, champ_wad_name, ltmao_root, work_root, out_path, log=print):
    """Turn a mod .zip into a proper .fantome (META/info.json + packed WAD/<Champ>.wad.client), the format
    LTK Manager installs directly. Loose RAW / assets files are packed into the champion's WAD with LtMAO.
    Returns a short description of what was done."""
    W = os.path.join(work_root, f"conv-{uuid.uuid4().hex[:8]}")
    src = os.path.join(W, "mod")
    os.makedirs(src, exist_ok=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = [n.replace("\\", "/") for n in zf.namelist()]
            inner = [n for n in names if n.lower().endswith((".fantome", ".zip"))]
            structured = any(re.search(r"(^|/)(wad|raw|meta)/", n.lower()) for n in names)
            if len(inner) == 1 and not structured:
                with zipfile.ZipFile(io.BytesIO(zf.read(zf.namelist()[names.index(inner[0])]))) as z2:
                    _extract(z2, src)
            else:
                # fast path: already META + packed WAD files -> it's a fantome with the wrong extension
                lows = [n.lower() for n in names if not n.endswith("/")]
                root_pref = ""
                for n in lows:
                    if n.endswith("meta/info.json"):
                        root_pref = n[: -len("meta/info.json")]; break
                rel = [n[len(root_pref):] for n in lows if n.startswith(root_pref)]
                if root_pref == "" and rel and all(r.startswith("meta/") or (r.startswith("wad/") and r.count("/") == 1 and r.endswith(".wad.client")) for r in rel) \
                        and any(r.startswith("wad/") for r in rel):
                    shutil.copy2(zip_path, out_path)
                    return "renamed (already packed)"
                _extract(zf, src)
        root = src
        for dp, dn, fn in os.walk(src):
            dl = [d.lower() for d in dn]
            if "meta" in dl or "wad" in dl or "raw" in dl:
                root = dp; break
        wad_dir = os.path.join(root, "WAD")
        os.makedirs(wad_dir, exist_ok=True)
        cw = os.path.join(wad_dir, champ_wad_name)          # e.g. WAD/Ahri.wad
        moved = 0
        # RAW/ -> champion WAD
        for entry in list(os.listdir(root)):
            p = os.path.join(root, entry)
            if entry.lower() == "raw" and os.path.isdir(p):
                for dp, dn, fn in os.walk(p):
                    for f in fn:
                        s_ = os.path.join(dp, f); d_ = os.path.join(cw, os.path.relpath(s_, p))
                        os.makedirs(os.path.dirname(d_), exist_ok=True); shutil.move(s_, d_); moved += 1
                shutil.rmtree(p, ignore_errors=True)
            elif entry.lower() in ("assets", "data") and os.path.isdir(p):   # loose game folders at the root
                d_ = os.path.join(cw, entry.lower())
                os.makedirs(cw, exist_ok=True); shutil.move(p, d_); moved += 1
        # unpacked WAD folders: WAD/Ahri.wad.client/ -> pack
        for f in list(os.listdir(wad_dir)):
            p = os.path.join(wad_dir, f)
            if os.path.isdir(p) and f.lower().endswith(".wad.client"):
                os.rename(p, p[: -len(".client")])
        packed = 0
        lt = LtMAO(ltmao_root) if ltmao_root else None
        for f in list(os.listdir(wad_dir)):
            p = os.path.join(wad_dir, f)
            if os.path.isdir(p) and f.lower().endswith(".wad"):
                if not any(fn for _, _, fn in os.walk(p)):
                    shutil.rmtree(p); continue
                if not lt:
                    raise RuntimeError("LtMAO-hai is needed to pack this mod")
                log(f"Packing {f} with LtMAO")
                lt.run("wadpack", p, p + ".client")
                shutil.rmtree(p); packed += 1
        if not any(f.lower().endswith(".wad.client") for f in os.listdir(wad_dir)):
            raise RuntimeError("No game files found to pack")
        meta = os.path.join(root, "META"); os.makedirs(meta, exist_ok=True)
        ip = os.path.join(meta, "info.json")
        if not os.path.exists(ip):
            stem = re.sub(r"\.(zip|fantome)$", "", os.path.basename(zip_path), flags=re.I)
            json.dump({"Author": "Unknown", "Description": "", "Name": stem, "Version": "1.0"}, open(ip, "w", encoding="utf-8"), indent=4)
        tmp = out_path + ".tmp"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for dp, dn, fn in os.walk(root):
                for f in fn:
                    full = os.path.join(dp, f)
                    z.write(full, os.path.relpath(full, root).replace("\\", "/"))
        os.replace(tmp, out_path)
        return f"packed {packed} WAD(s) with LtMAO" if packed else "repackaged"
    finally:
        shutil.rmtree(W, ignore_errors=True)
