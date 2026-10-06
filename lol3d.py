"""
lol3d - read League of Legends game/mod files well enough to show a skin in 3D.

  * WAD archives (v3.x): table of contents + entry decompression (raw/gzip/zstd/zstd-chunked)
  * PROP .bin files: generic reader, used to find a skin's mesh / skeleton / textures
  * SKN meshes -> flat arrays for three.js
  * TEX textures -> DDS (three.js DDSLoader renders DXT1/DXT5/BGRA)

Mod files are layered on top of the game's own files, exactly like the game does
when the mod is enabled, so the viewer shows what you'd actually see in game.
"""
import os, io, re, struct, zlib, gzip, zipfile, base64, threading

try:
    import zstandard as _zstd
except Exception:          # installed on demand by skin_manager
    _zstd = None
try:
    import xxhash as _xxhash
except Exception:
    _xxhash = None

# ------------------------------------------------------------------ hashing
_P1, _P2, _P3, _P4, _P5 = (11400714785074694791, 14029467366897019727, 1609587929392839161,
                           9650029242287828579, 2870177450012600261)
_M = (1 << 64) - 1

def _rotl(x, r):
    return ((x << r) | (x >> (64 - r))) & _M

def _round(acc, v):
    acc = (acc + v * _P2) & _M
    return (_rotl(acc, 31) * _P1) & _M

def _merge(acc, v):
    acc ^= _round(0, v)
    return (acc * _P1 + _P4) & _M

def xxh64_py(data, seed=0):
    n = len(data); i = 0
    if n >= 32:
        v1 = (seed + _P1 + _P2) & _M; v2 = (seed + _P2) & _M; v3 = seed; v4 = (seed - _P1) & _M
        while i + 32 <= n:
            a, b, c, d = struct.unpack_from("<4Q", data, i)
            v1 = _round(v1, a); v2 = _round(v2, b); v3 = _round(v3, c); v4 = _round(v4, d); i += 32
        h = (_rotl(v1, 1) + _rotl(v2, 7) + _rotl(v3, 12) + _rotl(v4, 18)) & _M
        h = _merge(h, v1); h = _merge(h, v2); h = _merge(h, v3); h = _merge(h, v4)
    else:
        h = (seed + _P5) & _M
    h = (h + n) & _M
    while i + 8 <= n:
        k = _round(0, struct.unpack_from("<Q", data, i)[0])
        h ^= k; h = (_rotl(h, 27) * _P1 + _P4) & _M; i += 8
    if i + 4 <= n:
        h ^= (struct.unpack_from("<I", data, i)[0] * _P1) & _M
        h = (_rotl(h, 23) * _P2 + _P3) & _M; i += 4
    while i < n:
        h ^= (data[i] * _P5) & _M
        h = (_rotl(h, 11) * _P1) & _M; i += 1
    h ^= h >> 33; h = (h * _P2) & _M; h ^= h >> 29; h = (h * _P3) & _M; h ^= h >> 32
    return h

def path_hash(path):
    b = path.lower().replace("\\", "/").encode("utf-8")
    return _xxhash.xxh64_intdigest(b) if _xxhash else xxh64_py(b)

def fnv1a(s):
    h = 0x811C9DC5
    for c in s.lower().encode():
        h = ((h ^ c) * 0x01000193) & 0xFFFFFFFF
    return h

# ------------------------------------------------------------------ WAD
class Wad:
    """Random-access reader for a WAD held on disk or in memory."""
    def __init__(self, src, name=""):
        self.name = name
        if isinstance(src, (bytes, bytearray)):
            self.f = io.BytesIO(src)
        else:
            self.f = open(src, "rb")
        f = self.f
        head = f.read(4)
        if head[:2] != b"RW":
            raise ValueError("not a WAD")
        self.major, self.minor = head[2], head[3]
        self.entries = {}
        if self.major == 3:
            f.seek(268); count = struct.unpack("<I", f.read(4))[0]
            toc = f.read(32 * count)
            for i in range(count):
                h, off, cs, us, t, dup, fsub, chk = struct.unpack_from("<QIIIBBHQ", toc, i * 32)
                self.entries[h] = (off, cs, us, t & 0x0F, t >> 4, fsub)
        elif self.major in (1, 2):
            if self.major == 1:
                toc_off, esize, count = struct.unpack("<HHI", f.read(8))
            else:
                f.seek(4 + 84 + 8); toc_off, esize, count = struct.unpack("<HHI", f.read(8))
            f.seek(toc_off); toc = f.read(esize * count)
            for i in range(count):
                h, off, cs, us, t = struct.unpack_from("<QIIIB", toc, i * esize)
                self.entries[h] = (off, cs, us, t & 0x0F, 0, 0)
        else:
            raise ValueError(f"unsupported WAD {self.major}.{self.minor}")
        self._subchunks = None
        self._lock = threading.Lock()     # one shared file handle: seek+read must not interleave between threads

    def __contains__(self, h):
        return h in self.entries

    def _subchunk_table(self):
        if self._subchunks is None:
            self._subchunks = []
            base = self.name.lower().replace("\\", "/")
            cands = []
            if base:
                stem = os.path.basename(base)
                if stem.endswith(".client"):
                    stem = stem[:-7]
                cands += [f"data/final/champions/{stem}.subchunktoc", f"data/final/maps/shipping/{stem}.subchunktoc",
                          f"data/final/{stem}.subchunktoc"]
            for c in cands:
                h = path_hash(c)
                if h in self.entries:
                    raw = self.read(h, allow_chunked=False)
                    self._subchunks = [struct.unpack_from("<IIQ", raw, i) for i in range(0, len(raw) - 15, 16)]
                    break
        return self._subchunks

    def read(self, h, allow_chunked=True):
        off, cs, us, t, nsub, fsub = self.entries[h]
        with self._lock:
            self.f.seek(off)
            data = self.f.read(cs)
        if t == 0:
            return data
        if t == 1:
            return gzip.decompress(data)
        if t == 2:
            return data  # link (path string)
        if _zstd is None:
            raise RuntimeError("zstandard module missing")
        if t == 3:
            return _zstd.ZstdDecompressor().decompress(data, max_output_size=us)
        if t == 4:
            table = self._subchunk_table() if allow_chunked else []
            if table and nsub and fsub + nsub <= len(table):
                out = bytearray(); pos = 0
                for csz, usz, _ in table[fsub:fsub + nsub]:
                    chunk = data[pos:pos + csz]; pos += csz
                    out += chunk if csz == usz else _zstd.ZstdDecompressor().decompress(chunk, max_output_size=usz)
                return bytes(out)
            # fallback: stream of zstd frames
            out = bytearray(); rest = data
            while rest:
                if rest[:4] != b"\x28\xb5\x2f\xfd":
                    out += rest; break
                d = _zstd.ZstdDecompressor().decompressobj()
                out += d.decompress(rest); rest = d.unused_data
            return bytes(out)
        raise ValueError(f"unknown entry type {t}")

# ------------------------------------------------------------------ file layers
class _WadSource:
    def __init__(self, wad):
        self.w = wad
    def __contains__(self, h):
        return h in self.w.entries
    def get(self, h):
        return (lambda: self.w.read(h)) if h in self.w.entries else None
    def items(self):
        return ((h, (lambda h=h: self.w.read(h))) for h in self.w.entries)

class Layers:
    """Lookup by path hash across: mod files (top) -> game WAD (bottom)."""
    def __init__(self):
        self.sources = []   # list of (label, getter dict: hash -> callable)
        self.origin = {}

    def add_wad(self, wad, label):
        self.sources.append((label, _WadSource(wad)))

    def add_files(self, mapping, label):
        self.sources.append((label, mapping))

    def get(self, key):
        h = key if isinstance(key, int) else path_hash(key)
        for label, src in self.sources:
            g = src.get(h)
            if g is not None:
                self.origin[h] = label
                return g()
        return None

    def source_of(self, key):
        h = key if isinstance(key, int) else path_hash(key)
        for label, src in self.sources:
            if h in src:
                return label
        return None

def mod_layer(path):
    """Collect every file in a mod (file or folder) as hash -> getter."""
    files = {}
    def add_rel(rel, getter):
        rel = rel.replace("\\", "/")
        m = re.match(r"^([0-9a-fA-F]{16})(\.\w+)?$", os.path.basename(rel))
        if m and "/" not in rel.strip("/"):
            files[int(m.group(1), 16)] = getter
        else:
            files[path_hash(rel)] = getter
    def add_wad_bytes(data, name):
        w = Wad(data, name)
        for h in w.entries:
            files[h] = (lambda h=h, w=w: w.read(h))
    low = path.lower()
    if os.path.isdir(path):
        for dp, dn, fn in os.walk(path):
            for f in fn:
                full = os.path.join(dp, f)
                rel = os.path.relpath(full, path).replace("\\", "/")
                rl = rel.lower()
                if rl.startswith("meta/"):
                    continue
                if rl.endswith((".wad.client", ".wad")) and os.path.isfile(full):
                    try:
                        add_wad_bytes(open(full, "rb").read(), f)
                    except Exception:
                        pass
                    continue
                for pre in ("raw/",):
                    if rl.startswith(pre):
                        rel = rel[len(pre):]
                if ".wad.client/" in rel.lower():
                    rel = re.split(r"(?i)\.wad\.client/", rel, 1)[1]
                add_rel(rel, lambda p=full: open(p, "rb").read())
    elif low.endswith((".wad.client", ".wad")):
        add_wad_bytes(open(path, "rb").read(), os.path.basename(path))
    elif low.endswith((".fantome", ".zip")):
        zf = zipfile.ZipFile(path)
        names = [(i, i.filename.replace("\\", "/")) for i in zf.infolist() if not i.is_dir()]
        root = ""
        for i, n in names:
            if n.lower().endswith("meta/info.json"):
                root = n[: -len("meta/info.json")]; break
        nested = [i for i, n in names if n.lower().endswith((".fantome", ".zip"))]
        if len(nested) == 1 and not any(n[len(root):].lower().startswith(("wad/", "raw/")) for i, n in names):
            data = zf.read(nested[0])
            tmp = io.BytesIO(data)
            return mod_layer_zip(zipfile.ZipFile(tmp))
        return mod_layer_zip(zf)
    return files

def mod_layer_zip(zf):
    files = {}
    names = [(i, i.filename.replace("\\", "/")) for i in zf.infolist() if not i.is_dir()]
    root = ""
    for i, n in names:
        if n.lower().endswith("meta/info.json"):
            root = n[: -len("meta/info.json")]; break
    for i, n in names:
        rel = n[len(root):] if n.startswith(root) else n
        rl = rel.lower()
        if rl.startswith("meta/"):
            continue
        if rl.startswith("wad/"):
            inner = rel[4:]
            if "/" in inner:
                sub = inner.split("/", 1)[1]
                m = re.match(r"^([0-9a-fA-F]{16})(\.\w+)?$", sub)
                key = int(m.group(1), 16) if m else path_hash(sub)
                files[key] = (lambda i=i: zf.read(i))
            elif rl.endswith((".wad.client", ".wad")):
                try:
                    w = Wad(zf.read(i), inner)
                    for h in w.entries:
                        files[h] = (lambda h=h, w=w: w.read(h))
                except Exception:
                    pass
            continue
        if rl.startswith("raw/"):
            rel = rel[4:]
        files[path_hash(rel)] = (lambda i=i: zf.read(i))
    return files

# ------------------------------------------------------------------ PROP bin reader
class BinReader:
    PRIM = {0: 0, 1: 1, 2: 1, 3: 1, 4: 2, 5: 2, 6: 4, 7: 4, 8: 8, 9: 8, 10: 4, 11: 8, 12: 12, 13: 16, 14: 64,
            15: 4, 17: 4, 18: 8}
    LIST, LIST2, POINTER, EMBED, LINK, OPTION, MAP, FLAG = 128, 129, 130, 131, 132, 133, 134, 135

    def __init__(self, data, only_classes=None):
        self.d = data; self.p = 0
        self.only = only_classes
        self.entries = {}   # name hash -> (class hash, fields dict)
        self.linked = []
        self._parse()

    def u8(self):
        v = self.d[self.p]; self.p += 1; return v
    def u16(self):
        v = struct.unpack_from("<H", self.d, self.p)[0]; self.p += 2; return v
    def u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]; self.p += 4; return v

    def _parse(self):
        if self.d[:4] == b"PTCH":
            self.p = 16
        if self.d[self.p:self.p + 4] != b"PROP":
            raise ValueError("not a PROP bin")
        self.p += 4
        ver = self.u32()
        if ver >= 2:
            for _ in range(self.u32()):
                n = self.u16(); self.linked.append(self.d[self.p:self.p + n].decode("utf-8", "replace")); self.p += n
        count = self.u32()
        types = struct.unpack_from(f"<{count}I", self.d, self.p); self.p += 4 * count
        for ct in types:
            size = self.u32(); end = self.p + size
            if self.only is not None and ct not in self.only:
                self.p = end; continue
            name = self.u32()
            try:
                fields = self._fields(self.u16())
                self.entries[name] = (ct, fields)
            except Exception:
                pass
            self.p = end

    def _fields(self, n):
        out = {}
        for _ in range(n):
            name = self.u32(); t = self.u8()
            out[name] = self._value(t)
        return out

    def _value(self, t):
        if t in self.PRIM:
            sz = self.PRIM[t]
            if t == 16:
                pass
            raw = self.d[self.p:self.p + sz]; self.p += sz
            if t in (17, 7, 6):
                return struct.unpack("<I", raw)[0]
            if t == 10:
                return struct.unpack("<f", raw)[0]
            if t == 1:
                return raw[0] != 0
            if t == 12:
                return struct.unpack("<3f", raw)
            if t == 18 or t == 9 or t == 8:
                return struct.unpack("<Q", raw)[0]
            return raw
        if t == 16:
            n = self.u16(); s = self.d[self.p:self.p + n].decode("utf-8", "replace"); self.p += n; return s
        if t in (self.LIST, self.LIST2):
            et = self.u8(); size = self.u32(); end = self.p + size; cnt = self.u32()
            vals = [self._value(et) for _ in range(cnt)]
            self.p = end; return vals
        if t in (self.POINTER, self.EMBED):
            cls = self.u32()
            if cls == 0:
                return None
            size = self.u32(); end = self.p + size
            f = self._fields(self.u16()); self.p = end
            f["__class__"] = cls
            return f
        if t == self.LINK:
            return self.u32()
        if t == self.OPTION:
            et = self.u8(); c = self.u8()
            return self._value(et) if c else None
        if t == self.MAP:
            kt = self.u8(); vt = self.u8(); size = self.u32(); end = self.p + size; cnt = self.u32()
            m = {}
            for _ in range(cnt):
                k = self._value(kt); m[k if not isinstance(k, (bytes, list, dict)) else repr(k)] = self._value(vt)
            self.p = end; return m
        if t == self.FLAG:
            return self.u8() != 0
        raise ValueError(f"unknown bin type {t}")

H = {k: fnv1a(k) for k in [
    "skinMeshProperties", "skeleton", "simpleSkin", "texture", "material", "materialOverride", "submesh",
    "initialSubmeshToHide", "initialSubmeshAvatarToHide", "skinScale", "samplerValues", "textureName", "texturePath",
    "samplerName", "SkinCharacterDataProperties", "StaticMaterialDef", "paramValues", "name", "value",
    "championSkinName", "skinClassification", "emissiveTexture", "TextureName"]}

def find_skin_mesh(bins):
    """Given parsed bins (BinReader list), return mesh setup dict for the skin."""
    for b in bins:
        for name, (ct, fields) in b.entries.items():
            smp = fields.get(H["skinMeshProperties"])
            if isinstance(smp, dict):
                return smp, b
    return None, None

def material_texture(bins, link):
    """Diffuse texture path of a StaticMaterialDef referenced by link hash."""
    for b in bins:
        e = b.entries.get(link)
        if not e:
            continue
        for s in e[1].get(H["samplerValues"], []) or []:
            if not isinstance(s, dict):
                continue
            nm = (s.get(H["textureName"]) or s.get(H["samplerName"]) or "")
            tp = s.get(H["texturePath"])
            if tp and (not nm or "diffuse" in str(nm).lower() or "main" in str(nm).lower() or "base" in str(nm).lower()):
                return tp
        for s in e[1].get(H["samplerValues"], []) or []:
            if isinstance(s, dict) and s.get(H["texturePath"]):
                return s[H["texturePath"]]
    return None

# ------------------------------------------------------------------ SKN
def parse_skn(data):
    magic, major, minor = struct.unpack_from("<IHH", data, 0)
    if magic != 0x00112233:
        raise ValueError("not an SKN mesh")
    p = 8
    subs = []
    if major == 0:
        icount, vcount = struct.unpack_from("<II", data, p); p += 8
    else:
        n = struct.unpack_from("<I", data, p)[0]; p += 4
        for _ in range(n):
            name = data[p:p + 64].split(b"\0")[0].decode("latin-1"); p += 64
            sv, vc, si, ic = struct.unpack_from("<4I", data, p); p += 16
            subs.append({"name": name, "start": si, "count": ic})
        if major == 4:
            p += 4  # flags
        icount, vcount = struct.unpack_from("<II", data, p); p += 8
    vsize, vtype = 52, 0
    if major == 4:
        vsize, vtype = struct.unpack_from("<II", data, p); p += 8
        p += 24 + 16  # bbox + sphere
    idx = data[p:p + 2 * icount]; p += 2 * icount
    if icount % 3:
        idx = idx[: 2 * (icount - icount % 3)]
    pos = bytearray(); nrm = bytearray(); uv = bytearray()
    for i in range(vcount):
        o = p + i * vsize
        pos += data[o:o + 12]
        nrm += data[o + 32:o + 44]
        uv += data[o + 44:o + 52]
    if not subs:
        subs = [{"name": "mesh", "start": 0, "count": icount}]
    return {"positions": bytes(pos), "normals": bytes(nrm), "uvs": bytes(uv), "indices": bytes(idx),
            "vcount": vcount, "submeshes": subs}

# ------------------------------------------------------------------ textures
def tex_to_dds(data):
    """Riot .tex -> .dds (largest mip only). Returns (bytes, note) or (None, reason)."""
    if data[:4] != b"TEX\0":
        return None, "not a TEX"
    w, h = struct.unpack_from("<HH", data, 4)
    fmt = data[9]; mips = data[11] & 1
    if fmt == 10:
        bpb, four = 8, b"DXT1"
    elif fmt == 12:
        bpb, four = 16, b"DXT5"
    elif fmt == 20:
        bpb, four = None, None
    else:
        return None, f"unsupported TEX format {fmt}"
    body = data[12:]
    if four:
        size = max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * bpb
    else:
        size = w * h * 4
    top = body[-size:] if mips else body[:size]
    if four:
        hdr = struct.pack("<4sI", b"DDS ", 124) + struct.pack("<6I", 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000, h, w, size, 0, 1)
        hdr += b"\0" * 44 + struct.pack("<II4s5I", 32, 0x4, four, 0, 0, 0, 0, 0)
    else:
        hdr = struct.pack("<4sI", b"DDS ", 124) + struct.pack("<6I", 0x1 | 0x2 | 0x4 | 0x1000 | 0x8, h, w, w * 4, 0, 1)
        hdr += b"\0" * 44 + struct.pack("<II4s5I", 32, 0x41, b"\0\0\0\0", 32, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    hdr += struct.pack("<5I", 0x1000, 0, 0, 0, 0)
    return hdr + top, None

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
BUNDLED_DATA = DATA_DIR
_NAMES = None

def names():
    """hash -> known asset path (textures/meshes of champion skins)."""
    global _NAMES
    if _NAMES is None:
        _NAMES = {}
        try:
            p = os.path.join(DATA_DIR, "texnames.bin")
            if not os.path.exists(p):
                p = os.path.join(BUNDLED_DATA, "texnames.bin")
            txt = zlib.decompress(open(p, "rb").read()).decode()
            for p in txt.split("\n"):
                _NAMES[path_hash(p)] = p
        except Exception:
            pass
    return _NAMES

def name_of(key):
    if isinstance(key, str):
        return key
    return names().get(key)

def decode_texture(d):
    if not d:
        return None
    if d[:4] == b"TEX\0":
        return tex_to_dds(d)[0]
    if d[:4] == b"DDS ":
        return d
    return None

def texture_bytes(layers, key):
    """Load a texture (path or hash); tries .tex/.dds siblings and prefers the mod's copy.
    Returns (dds_bytes, actual_key, source)."""
    if not key:
        return None, None, None
    cands = [key]
    nm = name_of(key)
    if nm:
        base, ext = os.path.splitext(nm)
        cands += [base + ".tex", base + ".dds"]
    found, seen = [], set()
    for c in cands:
        h = c if isinstance(c, int) else path_hash(c)
        if h in seen:
            continue
        seen.add(h)
        src = layers.source_of(h)
        if src:
            found.append((src != "mod", h))
    for _, h in sorted(found):
        dds = decode_texture(layers.get(h))
        if dds:
            return dds, h, layers.source_of(h)
    return None, None, None

BAD_TEX = re.compile(r"(_n|_normal|_mask|_glow|_emissive|_ao|_spec|_gloss|_rough|_metal|_fresnel|_mat|_ramp|_noise|_dist|_erode|_flipbook)\b|recall|particles|loadscreen|_sq|square|circle", re.I)

# ------------------------------------------------------------------ high level
def find_game_dir(cfg_dir=None):
    cands = []
    if cfg_dir:
        cands.append(cfg_dir)
    try:
        import json
        j = json.load(open(r"C:\ProgramData\Riot Games\RiotClientInstalls.json"))
        for k in (j.get("associated_client") or {}):
            if "pbe" not in k.lower():
                cands.append(k)
    except Exception:
        pass
    for d in "CDEFGH":
        cands.append(f"{d}:/Riot Games/League of Legends")
    for c in cands:
        for sub in ("", "Game"):
            p = os.path.join(c, sub, "DATA", "FINAL", "Champions")
            if os.path.isdir(p):
                return os.path.normpath(os.path.join(c, sub))
    return None

_WAD_CACHE = {}

def game_wad(game_dir, champ_id):
    if not game_dir:
        return None
    folder = os.path.join(game_dir, "DATA", "FINAL", "Champions")
    key = champ_id.lower()
    if key in _WAD_CACHE:
        return _WAD_CACHE[key]
    try:
        for f in os.listdir(folder):
            if f.lower() == f"{key}.wad.client":
                w = Wad(os.path.join(folder, f), f)
                _WAD_CACHE[key] = w
                return w
    except Exception:
        pass
    return None

def build_model(champ_id, skin_num, game_dir=None, mod_path=None):
    """Resolve the mesh + textures for champion/skin with an optional mod on top.
    Returns (model_dict, textures{key: dds bytes})."""
    notes = []
    layers = Layers()
    if mod_path:
        layers.add_files(mod_layer(mod_path), "mod")
    gw = game_wad(game_dir, champ_id)
    if gw:
        layers.add_wad(gw, "game")
    elif not mod_path:
        raise RuntimeError("League of Legends game files not found - set the game folder in Settings")
    else:
        notes.append("Game files not found - showing only what's inside the mod")

    c = champ_id.lower()
    bin_path = f"data/characters/{c}/skins/skin{skin_num}.bin"
    bins = []
    raw = layers.get(bin_path)
    bin_src = layers.source_of(bin_path)
    if raw:
        try:
            b = BinReader(raw); bins.append(b)
            for lk in b.linked[:40]:
                d2 = layers.get(lk)
                if d2:
                    try:
                        bins.append(BinReader(d2))
                    except Exception:
                        pass
        except Exception as e:
            notes.append(f"Couldn't read skin data ({e})")
    smp, _ = find_skin_mesh(bins)
    if smp is None and bin_src == "mod" and gw:
        # the mod's own skin file is unreadable/old - fall back to the game's so we still know the layout
        h = path_hash(bin_path)
        if h in gw.entries:
            try:
                gb = BinReader(gw.read(h)); bins = [gb]
                smp, _ = find_skin_mesh(bins)
                notes.append("The mod's skin data file couldn't be read (it may be from an old patch) - using the game's")
            except Exception:
                pass
    skn_path = smp.get(H["simpleSkin"]) if smp else None
    tex_path = smp.get(H["texture"]) if smp else None
    mat = smp.get(H["material"]) if smp else None
    if not tex_path and mat:
        tex_path = material_texture(bins, mat)
    hide = set()
    if smp:
        for k in ("initialSubmeshToHide", "initialSubmeshAvatarToHide"):
            v = smp.get(H[k])
            if isinstance(v, str):
                hide |= {x.lower() for x in re.split(r"[ ,;]+", v) if x}
    overrides = {}
    for o in (smp.get(H["materialOverride"]) or []) if smp else []:
        if not isinstance(o, dict):
            continue
        sm = o.get(H["submesh"]); t = o.get(H["texture"])
        if not t and o.get(H["material"]):
            t = material_texture(bins, o[H["material"]])
        if sm and t:
            overrides[sm.lower()] = t
    scale = smp.get(H["skinScale"]) if smp else None

    skn = layers.get(skn_path) if skn_path else None
    if skn is None:
        conv = [f"assets/characters/{c}/skins/skin{skin_num:02d}/{c}_skin{skin_num:02d}.skn",
                f"assets/characters/{c}/skins/base/{c}.skn", f"assets/characters/{c}/skins/base/{c}_base.skn"]
        for p in conv:
            skn = layers.get(p)
            if skn:
                skn_path = p; break

    textures = {}
    def tex_key(p):
        if not p:
            return None, None
        dds, actual, src = texture_bytes(layers, p)
        if dds is None:
            return None, None
        k = f"{actual:016x}"
        textures[k] = dds
        return k, src

    mesh = parse_skn(skn) if skn else None
    used_mod = bin_src == "mod" or (skn_path and layers.source_of(skn_path) == "mod")
    default_key = default_src = None
    subs = []
    if mesh:
        default_key, default_src = tex_key(tex_path)
        if tex_path and not default_key:
            notes.append("Main texture couldn't be decoded")
        used_mod = used_mod or default_src == "mod"
        for sm in mesh["submeshes"]:
            ov = overrides.get(sm["name"].lower())
            k, src = tex_key(ov) if ov else (default_key, default_src)
            used_mod = used_mod or src == "mod"
            subs.append({"name": sm["name"], "start": sm["start"], "count": sm["count"], "tex": k,
                         "hidden": sm["name"].lower() in hide})
    mesh_src = layers.source_of(skn_path) if skn_path else None

    # The mod's files aren't what the current game loads -> preview the mod's own mesh/textures.
    if mod_path and not used_mod:
        mod_src = dict(layers.sources).get("mod", {})
        skns, texs = [], []
        for h, g in list(mod_src.items()):
            try:
                d = g()
            except Exception:
                continue
            if d[:4] == b"\x33\x22\x11\x00":
                skns.append((h, d))
            elif d[:4] in (b"TEX\0", b"DDS "):
                texs.append((h, d))
        if skns:
            def skn_rank(item):
                nm = (name_of(item[0]) or "").lower()
                slot = f"/skin{skin_num:02d}/" if skin_num else "/base/"
                return (slot in nm, len(item[1]))
            h, d = max(skns, key=skn_rank)
            mesh = parse_skn(d); skn_path = name_of(h) or f"<mod mesh {h:016x}>"; mesh_src = "mod"
            good = [(h2, d2) for h2, d2 in texs if not BAD_TEX.search(name_of(h2) or "")]
            good.sort(key=lambda x: (("tx_cm" in (name_of(x[0]) or "").lower()), len(x[1])), reverse=True)
            def tex_for(subname):
                toks = [t for t in re.split(r"[^a-z0-9]+", subname.lower()) if len(t) > 2 and t not in ("mat", "skin", "base")]
                for h2, d2 in good:
                    nm = (name_of(h2) or "").lower()
                    if nm and any(t in os.path.basename(nm) for t in toks):
                        return h2, d2
                return good[0] if good else (None, None)
            game_subs = {x["name"].lower(): x["tex"] for x in subs}
            subs = []
            for sm in mesh["submeshes"]:
                h2, d2 = tex_for(sm["name"])
                k = game_subs.get(sm["name"].lower(), default_key)
                if d2:
                    dds = decode_texture(d2)
                    if dds:
                        k = f"{h2:016x}"; textures[k] = dds
                subs.append({"name": sm["name"], "start": sm["start"], "count": sm["count"], "tex": k,
                             "hidden": sm["name"].lower() in hide})
            notes.append("This mod's files use older file paths than the current game loads, so in game it will most likely "
                         "not show up (outdated mod). Showing the mod's own model here.")
        elif mesh and texs:
            good = [(h2, d2) for h2, d2 in texs if not BAD_TEX.search(name_of(h2) or "")]
            good.sort(key=lambda x: len(x[1]), reverse=True)
            for sub in subs:
                toks = [t for t in re.split(r"[^a-z0-9]+", sub["name"].lower()) if len(t) > 2 and t not in ("mat", "skin")]
                pick = None
                for h2, d2 in good:
                    nm = os.path.basename(name_of(h2) or "").lower()
                    if nm and any(t in nm for t in toks):
                        pick = (h2, d2); break
                if pick is None and sub["tex"] == default_key and good:
                    pick = good[0]
                if pick:
                    dds = decode_texture(pick[1])
                    if dds:
                        sub["tex"] = f"{pick[0]:016x}"; textures[sub["tex"]] = dds
            notes.append("This mod's textures use older file paths than the current game loads, so in game it will most likely "
                         "not show up (outdated mod). Showing its textures on the current model here.")
        elif mesh:
            notes.append("This mod doesn't change the 3D model or main textures (it may only change effects, sounds or "
                         "animations), so this looks like the original skin.")
    if mesh is None:
        raise RuntimeError("Couldn't find a 3D mesh for this skin")

    sources = {"mesh": mesh_src, "skin_data": bin_src,
               "texture": "mod" if any(t and layers.source_of(int(t, 16)) == "mod" for t in [x["tex"] for x in subs]) or
                          (mesh_src == "mod" and not gw) else (default_src or "game")}
    model = {
        "positions": base64.b64encode(mesh["positions"]).decode(),
        "normals": base64.b64encode(mesh["normals"]).decode(),
        "uvs": base64.b64encode(mesh["uvs"]).decode(),
        "indices": base64.b64encode(mesh["indices"]).decode(),
        "submeshes": subs, "scale": scale, "notes": notes, "sources": sources,
        "mesh_path": skn_path if not isinstance(skn_path, int) else (name_of(skn_path) or f"{skn_path:016x}"),
        "texture_path": tex_path if not isinstance(tex_path, int) else (name_of(tex_path) or f"{tex_path:016x}"),
    }
    return model, textures


# ------------------------------------------------------------------ which skins does a mod really change?
import json as _json, time as _time
_REFS = {}
SKIN_CLASSES = {fnv1a("SkinCharacterDataProperties"), fnv1a("StaticMaterialDef")}

def _add_ref(d, key, weight, siblings=False):
    if not key:
        return
    h = key if isinstance(key, int) else path_hash(key)
    d[h] = max(d.get(h, 0), weight)
    nm = name_of(key) if siblings else None
    if nm:
        base = os.path.splitext(nm)[0]
        for e in (".tex", ".dds"):
            hh = path_hash(base + e)
            d[hh] = max(d.get(hh, 0), weight)

def skin_refs(game_dir, champ_id):
    """{skin_num: {file_hash: weight}} of the mesh/textures/skeleton each official skin loads.
    weight: 3 = mesh, 2 = texture, 1 = skeleton. Cached on disk per game patch."""
    if not game_dir:
        return None
    key = champ_id.lower()
    if key in _REFS:
        return _REFS[key]
    w = game_wad(game_dir, champ_id)
    if not w:
        _REFS[key] = None
        return None
    mtime = int(os.path.getmtime(w.f.name)) if hasattr(w.f, "name") else 0
    cdir = os.path.join(DATA_DIR, "refs"); os.makedirs(cdir, exist_ok=True)
    cpath = os.path.join(cdir, f"{key}.v2.json")
    try:
        j = _json.load(open(cpath))
        if j.get("mtime") == mtime:
            refs = {int(n): {int(h, 16): wt for h, wt in v} for n, v in j["refs"].items()}
            _REFS[key] = refs
            return refs
    except Exception:
        pass
    refs = {}
    for n in range(0, 200):
        h = path_hash(f"data/characters/{key}/skins/skin{n}.bin")
        if h not in w.entries:
            continue
        try:
            b = BinReader(w.read(h), SKIN_CLASSES)
        except Exception:
            continue
        smp, _ = find_skin_mesh([b])
        if not smp:
            continue
        d = {}
        _add_ref(d, smp.get(H["simpleSkin"]), 3)
        _add_ref(d, smp.get(H["texture"]), 2)
        _add_ref(d, smp.get(H["skeleton"]), 1)
        if smp.get(H["material"]):
            _add_ref(d, material_texture([b], smp[H["material"]]), 2)
        for o in smp.get(H["materialOverride"]) or []:
            if isinstance(o, dict):
                t = o.get(H["texture"]) or (material_texture([b], o[H["material"]]) if o.get(H["material"]) else None)
                _add_ref(d, t, 2)
        refs[n] = d
    try:
        _json.dump({"mtime": mtime, "refs": {n: [[f"{h:016x}", wt] for h, wt in v.items()] for n, v in refs.items()}},
                   open(cpath, "w"))
    except Exception:
        pass
    _REFS[key] = refs
    return refs

def match_skins(refs, mod_hashes):
    """Score each official skin by how much of what it loads the mod replaces."""
    out = {}
    for n, d in refs.items():
        sc = 0; kinds = set()
        for h, wt in d.items():
            if h in mod_hashes:
                sc += wt; kinds.add(wt)
        if sc:
            out[n] = (sc, 3 in kinds, 2 in kinds)
    return out
