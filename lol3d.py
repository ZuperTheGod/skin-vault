"""
lol3d - read League of Legends game/mod files well enough to show a skin in 3D.

  * WAD archives (v3.x): table of contents + entry decompression (raw/gzip/zstd/zstd-chunked)
  * PROP .bin files: generic reader, used to find a skin's mesh / skeleton / textures
  * SKN meshes -> flat arrays for three.js
  * TEX textures -> DDS (three.js DDSLoader renders DXT1/DXT5/BGRA)

Mod files are layered on top of the game's own files, exactly like the game does
when the mod is enabled, so the viewer shows what you'd actually see in game.
"""
import os, io, re, struct, zlib, gzip, zipfile, base64, threading, math
from array import array

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

def bin_type_map(data):
    """(enclosing class hash, field hash) -> {type signature: occurrences}, for every property in a PROP bin.
    Values are skipped; only the layout is recorded. Used to spot properties whose type changed in a patch."""
    d = data; p = [0]; types = {}
    def u8():
        v = d[p[0]]; p[0] += 1; return v
    def u16():
        v = struct.unpack_from("<H", d, p[0])[0]; p[0] += 2; return v
    def u32():
        v = struct.unpack_from("<I", d, p[0])[0]; p[0] += 4; return v
    def fields(scope, n):
        for _ in range(n):
            name = u32(); t = u8()
            sig = value(t)
            ts = types.setdefault((scope, name), {})
            ts[sig] = ts.get(sig, 0) + 1
    def value(t):
        if t in BinReader.PRIM:
            p[0] += BinReader.PRIM[t]; return str(t)
        if t == 16:
            n = u16(); p[0] += n; return "16"
        if t in (BinReader.LIST, BinReader.LIST2):
            et = u8(); size = u32(); end = p[0] + size; cnt = u32()
            for _ in range(cnt):
                value(et)
            p[0] = end; return f"L{et}"
        if t in (BinReader.POINTER, BinReader.EMBED):
            cls = u32()
            if cls:
                size = u32(); end = p[0] + size
                fields(cls, u16()); p[0] = end
            return str(t)
        if t == BinReader.LINK:
            p[0] += 4; return str(t)
        if t == BinReader.OPTION:
            et = u8()
            if u8():
                value(et)
            return f"O{et}"
        if t == BinReader.MAP:
            kt = u8(); vt = u8(); size = u32(); end = p[0] + size; cnt = u32()
            for _ in range(cnt):
                value(kt); value(vt)
            p[0] = end; return f"M{kt},{vt}"
        if t == BinReader.FLAG:
            p[0] += 1; return str(t)
        raise ValueError(f"unknown bin type {t}")
    if d[:4] == b"PTCH":
        p[0] = 16
    if d[p[0]:p[0] + 4] != b"PROP":
        raise ValueError("not a PROP bin")
    p[0] += 4
    if u32() >= 2:
        for _ in range(u32()):
            n = u16(); p[0] += n
    count = u32()
    cts = struct.unpack_from(f"<{count}I", d, p[0]); p[0] += 4 * count
    for ct in cts:
        size = u32(); end = p[0] + size
        try:
            u32(); fields(ct, u16())
        except Exception:
            pass
        p[0] = end
    return types

_STRUCT = {str(BinReader.POINTER), str(BinReader.EMBED)}

def bin_type_report(mod_bin, game_bin):
    """Properties in the mod's bin whose type differs from the current game's copy (Riot changed it in a patch).
    simple:     same value, new type (string -> file, u8 -> u16, embed <-> pointer...). LTK Manager repairs these
                by itself when the mod is imported.
    structural: the value moved into a new struct (e.g. textureMult string -> pointer). LTK can't repair these
                ("unrepairable" / fatal bin/property-type); Skin Vault's Auto-fix can."""
    g = bin_type_map(game_bin); m = bin_type_map(mod_bin)
    simple = structural = 0
    for k, ts in m.items():
        if k not in g or len(g[k]) != 1:
            continue
        gt = next(iter(g[k]))
        for sig, n in ts.items():
            if sig == gt:
                continue
            if gt in _STRUCT and sig not in _STRUCT:
                structural += n
            else:
                simple += n
    return {"simple": simple, "structural": structural}

def bin_type_mismatches(mod_bin, game_bin):
    r = bin_type_report(mod_bin, game_bin)
    return r["simple"] + r["structural"]

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

# ------------------------------------------------------------------ skeletons (.skl) + mesh skinning checks
def _qrot(q, v):
    x, y, z, w = q; vx, vy, vz = v
    tx = 2 * (y * vz - z * vy); ty = 2 * (z * vx - x * vz); tz = 2 * (x * vy - y * vx)
    return (vx + w * tx + (y * tz - z * ty), vy + w * ty + (z * tx - x * tz), vz + w * tz + (x * ty - y * tx))

def parse_skl(data):
    """Modern skeleton (0x22FD4FC3). Returns {"joints": [{id, parent, hash, name, pos}], "influences": [joint ids]}.
    pos = the joint's bind-pose position in model space. Raises on legacy r3d2sklt skeletons."""
    size, fmt, ver = struct.unpack_from("<III", data, 0)
    if fmt != 0x22FD4FC3:
        if data[:8] == b"r3d2sklt":
            raise ValueError("legacy skeleton format")
        raise ValueError("not a skeleton")
    flags, jc, ic = struct.unpack_from("<HHI", data, 12)
    jo, jio, io = struct.unpack_from("<3i", data, 20)
    joints = []
    for i in range(jc):
        o = jo + i * 100
        jf, jid, par, _pad, nh, rad = struct.unpack_from("<HhhHIf", data, o)
        it = struct.unpack_from("<3f", data, o + 56); isc = struct.unpack_from("<3f", data, o + 68)
        ir = struct.unpack_from("<4f", data, o + 80)
        noff = struct.unpack_from("<i", data, o + 96)[0]
        ns = o + 96 + noff
        try:
            name = data[ns:data.index(b"\0", ns)].decode("latin-1")
        except Exception:
            name = ""
        r = _qrot((-ir[0], -ir[1], -ir[2], ir[3]), it)
        pos = (-r[0] / (isc[0] or 1), -r[1] / (isc[1] or 1), -r[2] / (isc[2] or 1))
        joints.append({"id": jid, "parent": par, "hash": nh, "name": name, "pos": pos})
    infl = list(struct.unpack_from(f"<{ic}H", data, io)) if ic else []
    return {"joints": joints, "influences": infl}

def skn_skinning(data):
    """Where the per-vertex bone data lives in an SKN: {"start", "stride", "count", "pos_y_range"}."""
    magic, major, minor = struct.unpack_from("<IHH", data, 0)
    if magic != 0x00112233:
        raise ValueError("not an SKN mesh")
    p = 8; stride = 52
    if major == 0:
        ic, vc = struct.unpack_from("<II", data, p); p += 8
    else:
        n = struct.unpack_from("<I", data, p)[0]; p += 4 + n * 80
        if major == 4:
            p += 4
        ic, vc = struct.unpack_from("<II", data, p); p += 8
        if major == 4:
            stride = struct.unpack_from("<I", data, p)[0]; p += 8 + 40
    p += 2 * ic
    return {"start": p, "stride": stride, "count": vc}

def skn_vertices(data, info=None, step=1):
    info = info or skn_skinning(data)
    p, st = info["start"], info["stride"]
    for i in range(0, info["count"], step):
        o = p + i * st
        yield i, struct.unpack_from("<3f", data, o), data[o + 12:o + 16], struct.unpack_from("<4f", data, o + 16)

def bind_error(skn, skl, step=7):
    """How far vertices sit from the bones they're attached to, relative to model height.
    A correctly rigged League model scores ~0.04-0.11; bones pointing at the wrong joints score 0.2+."""
    info = skn_skinning(skn)
    J, inf = skl["joints"], skl["influences"]
    ys = [v[1][1] for v in skn_vertices(skn, info, max(1, info["count"] // 400))]
    h = (max(ys) - min(ys)) if ys else 1
    tot = n = bad = 0
    for i, pos, b, w in skn_vertices(skn, info, step):
        best = None
        for k in range(4):
            if w[k] > 0.15:
                if b[k] >= len(inf) or inf[b[k]] >= len(J):
                    bad += 1; best = None; break
                dd = math.dist(pos, J[inf[b[k]]]["pos"])
                best = dd if best is None else min(best, dd)
        if best is not None:
            tot += best; n += 1
    return {"error": tot / max(n, 1) / (h or 1), "bad_refs": bad, "height": h}

def skeleton_diff(mod_skl, game_skl):
    """Joint-level comparison of a skeleton a mod ships against the game's current one."""
    g = {j["hash"]: j for j in game_skl["joints"]}
    m = {j["hash"]: j for j in mod_skl["joints"]}
    missing = [j["name"] for h, j in g.items() if h not in m]
    extra = [j["name"] for h, j in m.items() if h not in g]
    moved = 0
    for h, j in m.items():
        if h in g and math.dist(j["pos"], g[h]["pos"]) > 2.0:
            moved += 1
    same_order = [j["hash"] for j in mod_skl["joints"]] == [j["hash"] for j in game_skl["joints"]]
    same_infl = [mod_skl["joints"][i]["hash"] for i in mod_skl["influences"] if i < len(mod_skl["joints"])] == \
                [game_skl["joints"][i]["hash"] for i in game_skl["influences"] if i < len(game_skl["joints"])]
    return {"missing": missing, "extra": extra, "moved": moved, "same_order": same_order, "same_influences": same_infl,
            "identical": not missing and not extra and not moved and same_order and same_infl}

def _top_joint(b, w, skl):
    k = max(range(4), key=lambda i: w[i])
    inf = skl["influences"]
    if b[k] >= len(inf) or inf[b[k]] >= len(skl["joints"]):
        return None
    return skl["joints"][inf[b[k]]]["hash"]

def _grid(points, cell):
    g = {}
    for idx, (x, y, z) in enumerate(points):
        g.setdefault((int(x // cell), int(y // cell), int(z // cell)), []).append(idx)
    return g

def _nearest(g, points, p, cell, maxd):
    cx, cy, cz = int(p[0] // cell), int(p[1] // cell), int(p[2] // cell)
    best, bd = None, maxd
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for dz in (-1, 0, 1):
                for idx in g.get((cx + dx, cy + dy, cz + dz), ()):
                    d = math.dist(points[idx], p)
                    if d < bd:
                        best, bd = idx, d
    return best

def _neighbors(skl):
    """joint hash -> {itself, parent, children} (by hash) - small rig differences between neighbours are fine."""
    by_id = {j["id"]: j for j in skl["joints"]}
    nb = {j["hash"]: {j["hash"]} for j in skl["joints"]}
    for j in skl["joints"]:
        p = by_id.get(j["parent"])
        if p:
            nb[j["hash"]].add(p["hash"]); nb[p["hash"]].add(j["hash"])
    return nb

def _rig_points(skn, skl):
    pts, top = [], []
    for i, pos, b, w in skn_vertices(skn):
        pts.append(pos); top.append(_top_joint(b, w, skl))
    return pts, top

def compare_rig(mod_skn, mod_skl, game_skn, game_skl, step=4):
    """Compare a mod's mesh with the original game mesh for the same skin.
    overlap:   share of the mod's vertices sitting where an original vertex is (edits ~0.3-1.0, new models ~0)
    agreement: of those, how many follow the same (or a neighbouring) main bone as the original. Vertices that
               follow a bone the game skeleton doesn't have (the mod's own extra bones) aren't judged.
               Correct edits ~0.85-1.0; scrambled / wrong bone order ~0.0-0.5."""
    gpts, gtop = _rig_points(game_skn, game_skl)
    ys = [p[1] for p in gpts]; h = (max(ys) - min(ys)) or 1
    cell = h * 0.01; maxd = h * 0.004
    g = _grid(gpts, cell)
    nb = _neighbors(game_skl); known = set(nb)
    near = judged = same = n = 0
    for i, pos, b, w in skn_vertices(mod_skn, None, step):
        n += 1
        j = _nearest(g, gpts, pos, cell, maxd)
        if j is None:
            continue
        near += 1
        mj = _top_joint(b, w, mod_skl)
        if mj is None or mj not in known or gtop[j] is None:
            continue
        judged += 1
        if mj in nb.get(gtop[j], ()):
            same += 1
    return {"overlap": near / max(n, 1), "agreement": same / max(judged, 1), "checked": n, "judged": judged}

def repair_rig(mod_skn, mod_skl, game_skn, game_skl, max_dist=0.03):
    """Re-attach vertices that follow the wrong bones: copy bones + weights from the nearest vertex of the original
    game mesh (within max_dist x model height), translated into the mod skeleton's bone list. Vertices on the
    mod's own extra bones, or far from any original vertex (new parts), are left alone.
    Returns (new_skn_bytes, vertices_changed)."""
    info = skn_skinning(mod_skn)
    gi = skn_skinning(game_skn)
    gpts, gbones = [], []
    for i, pos, b, w in skn_vertices(game_skn, gi):
        gpts.append(pos); gbones.append((b, w))
    ys = [p[1] for p in gpts]; h = (max(ys) - min(ys)) or 1
    cell = h * 0.02; maxd = h * max_dist
    g = _grid(gpts, cell)
    nb = _neighbors(game_skl); known = set(nb)
    gj = game_skl["joints"]; ginf = game_skl["influences"]
    # mod skeleton: joint hash -> index in its influence list (what the SKN's bone bytes point at)
    mj = mod_skl["joints"]; minf = mod_skl["influences"]
    infl_of = {mj[ji]["hash"]: k for k, ji in enumerate(minf) if ji < len(mj)}
    parent_of = {}
    by_id = {j["id"]: j for j in mj}
    for j in mj:
        p = by_id.get(j["parent"]); parent_of[j["hash"]] = p["hash"] if p else None
    gparent = {}
    gby = {j["id"]: j for j in gj}
    for j in gj:
        p = gby.get(j["parent"]); gparent[j["hash"]] = p["hash"] if p else None
    def to_mod_index(jh):
        seen = 0
        while jh is not None and seen < 64:
            if jh in infl_of:
                return infl_of[jh]
            jh = gparent.get(jh) or parent_of.get(jh); seen += 1
        return None
    out = bytearray(mod_skn); changed = 0
    for i, pos, b, w in skn_vertices(mod_skn, info):
        cur = _top_joint(b, w, mod_skl)
        if cur is not None and cur not in known:
            continue                      # the mod's own extra bone - leave it
        j = _nearest(g, gpts, pos, cell, maxd)
        if j is None:
            continue
        gb, gw_ = gbones[j]
        gk = max(range(4), key=lambda k: gw_[k])
        if gb[gk] >= len(ginf):
            continue
        gtop = gj[ginf[gb[gk]]]["hash"]
        if cur is not None and cur in nb.get(gtop, ()):
            continue                      # already follows the right bone
        nbones, nweights = [0, 0, 0, 0], [0.0, 0.0, 0.0, 0.0]
        ok = True
        for k in range(4):
            if gw_[k] <= 0:
                continue
            if gb[k] >= len(ginf):
                ok = False; break
            idx = to_mod_index(gj[ginf[gb[k]]]["hash"])
            if idx is None or idx > 255:
                ok = False; break
            nbones[k] = idx; nweights[k] = gw_[k]
        if not ok:
            continue
        tot = sum(nweights) or 1
        o = info["start"] + i * info["stride"]
        out[o + 12:o + 16] = bytes(nbones)
        struct.pack_into("<4f", out, o + 16, *[x / tot for x in nweights])
        changed += 1
    return bytes(out), changed

def _bbox(skn):
    xs = []; 
    for i, pos, b, w in skn_vertices(skn, None, 3):
        xs.append(pos)
    lo = [min(p[k] for p in xs) for k in range(3)]; hi = [max(p[k] for p in xs) for k in range(3)]
    return lo, hi

def _shifted(skn, d):
    out = bytearray(skn); info = skn_skinning(skn)
    for i in range(info["count"]):
        o = info["start"] + i * info["stride"]
        x, y, z = struct.unpack_from("<3f", out, o)
        struct.pack_into("<3f", out, o, x + d[0], y + d[1], z + d[2])
    return bytes(out)

def analyze_bones(champ_id, skin_num, game_dir, mod_path):
    """Check a mod's model against the original model + skeleton for that skin.
    Returns None when the mod doesn't replace the model, else a dict with numbers and a verdict:
      ok          - follows the same bones as the original (or a brand-new model that can't be compared)
      twisted     - an edit of the original, but its vertices follow the wrong bones -> twists/stretches in game
      partly      - some areas follow the wrong bones
      offset      - the whole model is shifted away from the skeleton ("misaligned")
      broken      - vertices point at bones that don't exist in the skeleton
      unknown     - couldn't be read"""
    c = champ_id.lower()
    gw = game_wad(game_dir, champ_id)
    if not gw:
        return None
    layers = Layers()
    layers.add_files(mod_layer(mod_path), "mod")
    layers.add_wad(gw, "game")
    def mesh_paths(getter):
        raw = getter(f"data/characters/{c}/skins/skin{skin_num}.bin")
        if not raw:
            return None, None
        try:
            smp, _ = find_skin_mesh([BinReader(raw)])
        except Exception:
            return None, None
        return (smp.get(H["simpleSkin"]), smp.get(H["skeleton"])) if smp else (None, None)
    gread = lambda k: gw.read(path_hash(k)) if path_hash(k) in gw.entries else None
    gskn_p, gskl_p = mesh_paths(gread)
    skn_p, skl_p = mesh_paths(layers.get)
    skn_p = skn_p or gskn_p; skl_p = skl_p or gskl_p
    masks = None
    if skl_p and gskl_p and layers.source_of(skl_p) == "mod":
        try:
            masks = skin_mask_check(layers, gread, champ_id, skin_num, skl_p, gskl_p)
        except Exception:
            masks = None
    if not skn_p or not skl_p or layers.source_of(skn_p) != "mod":
        return {"verdict": "ok", "masks": masks, "skeleton": skl_p, "skeleton_from": "mod"} if masks else None
    out = {"mesh": skn_p, "skeleton": skl_p, "skeleton_from": layers.source_of(skl_p), "masks": masks}
    try:
        gsk = gread(gskn_p) if gskn_p else None
        out["names"] = part_name_fixes(layers.get(skn_p), gsk) if gsk else {}
    except Exception:
        out["names"] = {}
    try:
        skl = parse_skl(layers.get(skl_p))
        skn = layers.get(skn_p)
        be = bind_error(skn, skl)
    except Exception as e:
        out.update(verdict="unknown", note=str(e)[:120]); return out
    out["error"] = round(be["error"], 3); out["bad_refs"] = be["bad_refs"]
    if be["bad_refs"] > 20:
        out["verdict"] = "broken"; return out
    try:
        gskn = gread(gskn_p) if gskn_p else None
        gskl = parse_skl(gread(gskl_p)) if gskl_p and gread(gskl_p) else None
    except Exception:
        gskn = gskl = None
    if not gskn or not gskl:
        out["verdict"] = "ok"; out["note"] = "no original model to compare with"; return out
    out.update(rig_verdict(skn, skl, gskn, gskl))
    return out

def rig_verdict(skn, skl, gskn, gskl):
    """ok | twisted | partly | offset (+ numbers) for a mod mesh vs the original mesh of the same skin."""
    cmp_ = compare_rig(skn, skl, gskn, gskl)
    out = {"overlap": round(cmp_["overlap"], 2), "agreement": round(cmp_["agreement"], 2)}
    if cmp_["overlap"] < 0.25:
        try:
            (ml, mh), (gl, gh) = _bbox(skn), _bbox(gskn)
            msz = [mh[k] - ml[k] for k in range(3)]; gsz = [gh[k] - gl[k] for k in range(3)]
            height = gsz[1] or 1
            if all(abs(msz[k] - gsz[k]) <= 0.15 * max(gsz[k], 1) for k in range(3)):
                d = [((gl[k] + gh[k]) - (ml[k] + mh[k])) / 2 for k in range(3)]
                if max(abs(x) for x in d) > 0.03 * height:
                    c2 = compare_rig(_shifted(skn, d), skl, gskn, gskl)
                    if c2["overlap"] >= 0.5:
                        out.update(verdict="offset", shift=[round(x, 3) for x in d], overlap_after=round(c2["overlap"], 2))
                        return out
        except Exception:
            pass
        out["verdict"] = "ok"; out["note"] = "a new model (not an edit of the original) - its rig can't be compared"
        return out
    if cmp_["judged"] < 50:
        out["verdict"] = "ok"
    elif cmp_["agreement"] < 0.6:
        out["verdict"] = "twisted"
    elif cmp_["agreement"] < 0.8:
        out["verdict"] = "partly"
    else:
        out["verdict"] = "ok"
    return out

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
                hide |= {x for x in re.split(r"[ ,;]+", v) if x}     # exact match, like the game
    overrides = {}
    for o in (smp.get(H["materialOverride"]) or []) if smp else []:
        if not isinstance(o, dict):
            continue
        sm = o.get(H["submesh"]); t = o.get(H["texture"])
        if not t and o.get(H["material"]):
            t = material_texture(bins, o[H["material"]])
        if sm and t:
            overrides[sm] = t
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
    mod_mesh_fallback = False
    used_mod = bin_src == "mod" or (skn_path and layers.source_of(skn_path) == "mod")
    default_key = default_src = None
    subs = []
    if mesh:
        default_key, default_src = tex_key(tex_path)
        if tex_path and not default_key:
            notes.append("Main texture couldn't be decoded")
        used_mod = used_mod or default_src == "mod"
        for sm in mesh["submeshes"]:
            ov = overrides.get(sm["name"])
            k, src = tex_key(ov) if ov else (default_key, default_src)
            used_mod = used_mod or src == "mod"
            subs.append({"name": sm["name"], "start": sm["start"], "count": sm["count"], "tex": k,
                         "hidden": sm["name"] in hide})
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
            skn = d; mod_mesh_fallback = True
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
                             "hidden": sm["name"] in hide})
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

    if skn and gw and mesh_src == "mod" and isinstance(skn_path, str) and path_hash(skn_path) in gw.entries:
        try:
            fx = part_name_fixes(skn, gw.read(path_hash(skn_path)))
            if fx:
                notes.append("Some part names don't match the game's exactly (" + ", ".join(f"{a} → {b}" for a, b in fx.items()) +
                             "), so in game those parts may show when they should be hidden or get the wrong texture - "
                             "this view shows it the way the game will. 🛠 Auto-fix renames them.")
        except Exception:
            pass
    skin, anim = _model_skin(layers, bins, smp, skn, skn_path, mod_mesh_fallback, champ_id, skin_num, notes)
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
    if skin:
        model["rig"] = skin
        model["anims"] = [{"name": nm, "mod": layers.source_of(k) == "mod"} for nm, k in anim["clips"]]
        model["_anim"] = anim
    return model, textures


def _model_skin(layers, bins, smp, skn, skn_path, mod_mesh_fallback, champ_id, skin_num, notes):
    """Skinning data for the viewer + what the animation endpoint needs later. (None, None) when the mesh can't animate."""
    if not skn:
        return None, None
    skl_path = smp.get(H["skeleton"]) if smp else None
    skl = None
    if mod_mesh_fallback:
        mod_src = dict(layers.sources).get("mod", {})
        stem = os.path.splitext(str(skn_path))[0].lower()
        found = []
        for h, g in list(mod_src.items()):
            nm = (name_of(h) or "").lower()
            if nm.endswith(".skl") or (not nm and h != path_hash(str(skn_path))):
                try:
                    d = g()
                except Exception:
                    continue
                if len(d) > 12 and struct.unpack_from("<I", d, 4)[0] == 0x22FD4FC3:
                    found.append((nm.startswith(stem), d))
        if found:
            skl = max(found, key=lambda x: x[0])[1]
    if skl is None and skl_path:
        skl = layers.get(skl_path)
    if skl is None:
        c = champ_id.lower()
        for p in (f"assets/characters/{c}/skins/skin{skin_num:02d}/{c}_skin{skin_num:02d}.skl", f"assets/characters/{c}/skins/base/{c}.skl"):
            skl = layers.get(p)
            if skl:
                break
    if not skl:
        notes.append("No skeleton found - animations can't be played for this model")
        return None, None
    try:
        joints, infl = skl_bind(skl)
        idx, wts = skn_skin_attrs(skn, infl, len(joints))
    except Exception as e:
        notes.append(f"Animations unavailable ({e})")
        return None, None
    ids = {j["id"]: i for i, j in enumerate(joints)}
    skin = {"joints": [{"n": j["name"], "p": ids.get(j["parent"], -1), "t": j["t"], "r": j["r"], "s": j["s"],
                        "it": j["it"], "ir": j["ir"], "is": j["is"]} for j in joints],
            "idx": base64.b64encode(idx).decode(), "w": base64.b64encode(wts).decode()}
    try:
        clips = list_clips(layers, bins, champ_id, skin_num)
    except Exception:
        clips = []
    return skin, {"layers": layers, "clips": clips, "joints": joints}

def clip_data(anim, i):
    nm, key = anim["clips"][i]
    d = anim["layers"].get(key)
    if not d:
        raise RuntimeError("animation file not found")
    out = anim_payload(d, anim["joints"]); out["name"] = nm
    return out


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


# ------------------------------------------------------------------ animation playback (.anm) for the 3D viewer
def elf_hash(s):
    h = 0
    for c in s.lower().encode("latin-1", "replace"):
        h = ((h << 4) + c) & 0xFFFFFFFF
        hi = h & 0xF0000000
        if hi:
            h ^= hi >> 24
        h &= ~hi & 0xFFFFFFFF
    return h

def skl_bind(data):
    """Per joint: local bind transform (t, r, s) + the inverse bind matrix parts, in file order. Modern SKL only."""
    size, fmt, ver = struct.unpack_from("<III", data, 0)
    if fmt != 0x22FD4FC3:
        raise ValueError("legacy skeleton format" if data[:8] == b"r3d2sklt" else "not a skeleton")
    flags, jc, ic = struct.unpack_from("<HHI", data, 12)
    jo, jio, io = struct.unpack_from("<3i", data, 20)
    out = []
    for i in range(jc):
        o = jo + i * 100
        jf, jid, par, _pad, nh, rad = struct.unpack_from("<HhhHIf", data, o)
        lt = struct.unpack_from("<3f", data, o + 16); ls = struct.unpack_from("<3f", data, o + 28)
        lr = struct.unpack_from("<4f", data, o + 40)
        it = struct.unpack_from("<3f", data, o + 56); isc = struct.unpack_from("<3f", data, o + 68)
        ir = struct.unpack_from("<4f", data, o + 80)
        noff = struct.unpack_from("<i", data, o + 96)[0]
        ns = o + 96 + noff
        try:
            name = data[ns:data.index(b"\0", ns)].decode("latin-1")
        except Exception:
            name = ""
        out.append({"id": jid, "parent": par, "hash": nh, "elf": elf_hash(name), "name": name,
                    "t": lt, "r": lr, "s": ls, "it": it, "ir": ir, "is": isc})
    infl = list(struct.unpack_from(f"<{ic}H", data, io)) if ic else []
    return out, infl

def skn_skin_attrs(data, infl, njoints):
    """Per-vertex joint indices (4 x u16, skeleton order) + weights (4 x f32), aligned with parse_skn's vertices."""
    info = skn_skinning(data)
    p, st, n = info["start"], info["stride"], info["count"]
    idx = array("H"); wts = array("f")
    nin = len(infl)
    for i in range(n):
        o = p + i * st
        b = data[o + 12:o + 16]
        w = struct.unpack_from("<4f", data, o + 16)
        for k in range(4):
            j = infl[b[k]] if b[k] < nin else 0
            idx.append(j if 0 <= j < njoints else 0)
        tot = sum(w) or 1.0
        wts.extend(x / tot for x in w)
    return idx.tobytes(), wts.tobytes()

_S2 = 1.41421356237

def _quat48(a, b, c):
    bits = a | (b << 16) | (c << 32)
    mi = (bits >> 45) & 3
    va = (bits >> 30) & 0x7FFF; vb = (bits >> 15) & 0x7FFF; vc = bits & 0x7FFF
    x = va / 32767.0 * _S2 - 1 / _S2; y = vb / 32767.0 * _S2 - 1 / _S2; z = vc / 32767.0 * _S2 - 1 / _S2
    d = math.sqrt(max(0.0, 1 - (x * x + y * y + z * z)))
    return ((d, x, y, z), (x, d, y, z), (x, y, d, z), (x, y, z, d))[mi]

def parse_anm(data):
    """League animation -> {"duration": sec, "tracks": {joint_hash: {"t": [(time, xyz)], "r": [(time, xyzw)], "s": [...]}}}.
    Handles r3d2anmd v3 (legacy), v4/v5 (uncompressed) and r3d2canm (compressed)."""
    magic = data[:8]; ver = struct.unpack_from("<I", data, 8)[0]
    tracks = {}
    def tr(h):
        t = tracks.get(h)
        if t is None:
            t = tracks[h] = {"t": [], "r": [], "s": []}
        return t
    if magic == b"r3d2anmd" and ver in (4, 5):
        tc, fc = struct.unpack_from("<ii", data, 28)
        fdur = struct.unpack_from("<f", data, 36)[0]
        if not (0 < fdur < 10):
            fdur = 1 / 30
        if ver == 5:
            jho, ano, tmo, vpo, qpo, fro = struct.unpack_from("<6i", data, 40)
            nv = (qpo - vpo) // 12; nq = (jho - qpo) // 6
            vec = [struct.unpack_from("<3f", data, vpo + 12 + i * 12) for i in range(nv)]
            quat = [_quat48(*struct.unpack_from("<3H", data, qpo + 12 + i * 6)) for i in range(nq)]
            hashes = struct.unpack_from(f"<{tc}I", data, jho + 12)
            p = fro + 12
            for f in range(fc):
                tm = f * fdur
                for k in range(tc):
                    ti, si, ri = struct.unpack_from("<3H", data, p); p += 6
                    t = tr(hashes[k])
                    if ti < nv: t["t"].append((tm, vec[ti]))
                    if si < nv: t["s"].append((tm, vec[si]))
                    if ri < nq: t["r"].append((tm, quat[ri]))
        else:
            tko, ano, tmo, vpo, qpo, fro = struct.unpack_from("<6i", data, 40)
            nv = (qpo - vpo) // 12; nq = (fro - qpo) // 16
            vec = [struct.unpack_from("<3f", data, vpo + 12 + i * 12) for i in range(nv)]
            quat = [struct.unpack_from("<4f", data, qpo + 12 + i * 16) for i in range(nq)]
            p = fro + 12
            for f in range(fc):
                tm = f * fdur
                for k in range(tc):
                    h, ti, si, ri, _ = struct.unpack_from("<I4H", data, p); p += 12
                    t = tr(h)
                    if ti < nv: t["t"].append((tm, vec[ti]))
                    if si < nv: t["s"].append((tm, vec[si]))
                    if ri < nq: t["r"].append((tm, quat[ri]))
        return {"duration": max(fdur * (fc - 1), fdur), "tracks": tracks}
    if magic == b"r3d2anmd" and ver in (1, 2, 3):
        skl_id, tc, fc, fps = struct.unpack_from("<I3i", data, 12)
        fdur = 1.0 / fps if fps > 0 else 1 / 30
        p = 28
        for _ in range(tc):
            name = data[p:p + 32].split(b"\0")[0].decode("latin-1"); p += 36
            t = tr(elf_hash(name))
            for f in range(fc):
                q = struct.unpack_from("<7f", data, p); p += 28
                t["r"].append((f * fdur, q[:4])); t["t"].append((f * fdur, q[4:]))
        return {"duration": max(fdur * (fc - 1), fdur), "tracks": tracks}
    if magic == b"r3d2canm":
        rsz, ftok, fl, jc, fc, jcc = struct.unpack_from("<6i", data, 12)
        dur, fps = struct.unpack_from("<2f", data, 36)
        tmin = struct.unpack_from("<3f", data, 68); tmax = struct.unpack_from("<3f", data, 80)
        smin = struct.unpack_from("<3f", data, 92); smax = struct.unpack_from("<3f", data, 104)
        fro, jco, jho = struct.unpack_from("<3i", data, 116)
        hashes = struct.unpack_from(f"<{jc}I", data, jho + 12)
        tsc = [tmax[i] - tmin[i] for i in range(3)]; ssc = [smax[i] - smin[i] for i in range(3)]
        for tm, ji, a, b, c in struct.iter_unpack("<5H", data[fro + 12:fro + 12 + fc * 10]):
            j = ji & 0x3FFF; typ = ji >> 14
            if j >= jc:
                continue
            t = tr(hashes[j]); time = tm / 65535.0 * dur
            if typ == 0:
                t["r"].append((time, _quat48(a, b, c)))
            elif typ == 1:
                t["t"].append((time, (tmin[0] + tsc[0] * a / 65535.0, tmin[1] + tsc[1] * b / 65535.0, tmin[2] + tsc[2] * c / 65535.0)))
            elif typ == 2:
                t["s"].append((time, (smin[0] + ssc[0] * a / 65535.0, smin[1] + ssc[1] * b / 65535.0, smin[2] + ssc[2] * c / 65535.0)))
        for t in tracks.values():
            for k in ("t", "r", "s"):
                t[k].sort(key=lambda x: x[0])
        return {"duration": dur, "tracks": tracks}
    raise ValueError(f"unsupported animation format ({magic[:8]!r} v{ver})")

def anim_payload(data, joints):
    """parse_anm + map tracks onto skeleton joints -> compact JSON for three.js KeyframeTracks."""
    a = parse_anm(data)
    by_hash = {}
    for i, j in enumerate(joints):
        by_hash.setdefault(j["hash"], i); by_hash.setdefault(j["elf"], i)
    out = []; matched = 0
    for h, t in a["tracks"].items():
        i = by_hash.get(h)
        if i is None:
            continue
        matched += 1
        e = {"j": i}
        for k, n in (("t", 3), ("r", 4), ("s", 3)):
            keys = t[k]
            if not keys:
                continue
            # drop keys that repeat the previous value (static channels are common)
            times = array("f"); vals = array("f")
            for idx, (tm, v) in enumerate(keys):
                if 0 < idx < len(keys) - 1 and v == keys[idx - 1][1] and v == keys[idx + 1][1]:
                    continue
                times.append(tm); vals.extend(v)
            e[k] = [base64.b64encode(times.tobytes()).decode(), base64.b64encode(vals.tobytes()).decode()]
        out.append(e)
    return {"duration": a["duration"], "tracks": out, "matched": matched, "total": len(a["tracks"])}

CLIP_WORDS = ["idle", "run", "attack", "crit", "spell", "dance", "laugh", "joke", "taunt", "recall", "death", "channel",
              "spawn", "respawn", "homeguard", "turn", "levelup", "emote", "victory", "stun", "knockup", "passive", "ult",
              "walk", "dash", "jump", "cast", "idle_in", "run_in", "run_haste", "run_fast", "run_slow", "channel_wndup",
              "recall_winddown", "attack_crit"]
_CLIP_NAMES = None

def clip_name(h):
    global _CLIP_NAMES
    if _CLIP_NAMES is None:
        m = {}
        for b in CLIP_WORDS:
            for n in ["", "1", "2", "3", "4", "5", "6"]:
                for suf in ["", "_base", "_in", "_out", "_loop", "_start", "_end", "_a", "_b", "_c", "a", "b", "c", "_run"]:
                    for pre in ["", "spell1_", "spell2_", "spell3_", "spell4_"]:
                        nm = pre + b + n + suf
                        m.setdefault(fnv1a(nm), nm)
        _CLIP_NAMES = m
    return _CLIP_NAMES.get(h)

_CLIP_ORDER = ["idle", "run", "attack", "crit", "spell", "dance", "laugh", "joke", "taunt", "recall", "channel", "death"]

def list_clips(layers, bins, champ_id, skin_num):
    """[(label, file_key)] for the skin's animations, read from its animation graph (mod's copy first)."""
    c = champ_id.lower()
    cands = [l for b in bins[:1] for l in b.linked if "/animations/" in l.lower()]
    cands += [f"data/characters/{c}/animations/skin{skin_num}.bin", f"data/characters/{c}/animations/skin0.bin"]
    H_MAP, H_RES, H_PATH = fnv1a("mClipDataMap"), fnv1a("mAnimationResourceData"), fnv1a("mAnimationFilePath")
    for p in cands:
        raw = layers.get(p)
        if not raw:
            continue
        try:
            b = BinReader(raw)
        except Exception:
            continue
        clips, seen = [], set()
        for _n, (_ct, f) in b.entries.items():
            cm = f.get(H_MAP)
            if not isinstance(cm, dict):
                continue
            for k, v in cm.items():
                r = v.get(H_RES) if isinstance(v, dict) else None
                fp = r.get(H_PATH) if isinstance(r, dict) else None
                if fp is None or fp == "":
                    continue
                key = fp if isinstance(fp, int) else path_hash(fp)
                if key in seen:
                    continue
                seen.add(key)
                nm = clip_name(k) if isinstance(k, int) else str(k)
                if not nm and isinstance(fp, str):
                    nm = os.path.splitext(os.path.basename(fp))[0]
                    nm = re.sub(rf"^{re.escape(c)}_", "", nm.lower())
                clips.append((nm, key))
        if clips:
            def rank(x):
                nm = x[0] or "~"
                for i, w in enumerate(_CLIP_ORDER):
                    if nm.startswith(w) or nm.split("_", 1)[-1].startswith(w):
                        return (i, nm)
                return (len(_CLIP_ORDER), nm)
            clips.sort(key=rank)
            n = 0; out = []
            for nm, key in clips:
                if not nm:
                    n += 1; nm = f"other {n}"
                out.append((nm.replace("_", " "), key))
            return out
    return []


# ------------------------------------------------------------------ animation masks vs. a mod's skeleton
# Animation graphs blend layers (e.g. upper body attacking while the legs run) with masks: one weight per joint,
# listed in the order of the skeleton the masks were made for. A mod that ships its own skeleton with joints in a
# different order (or extra joints) makes those weights land on the wrong bones, so in game limbs jump around
# even though every single animation looks fine on its own (which is all the 3D viewer plays).
H_MASKS, H_WEIGHTS = fnv1a("mMaskDataMap"), fnv1a("mWeightList")

def bin_weight_lists(data):
    """Every mWeightList (list of floats) in a PROP bin: [{"start", "n", "sizes": [offsets of enclosing size fields]}]."""
    d = data; p = [0]; found = []; stack = []
    def u8():
        v = d[p[0]]; p[0] += 1; return v
    def u16():
        v = struct.unpack_from("<H", d, p[0])[0]; p[0] += 2; return v
    def u32():
        v = struct.unpack_from("<I", d, p[0])[0]; p[0] += 4; return v
    def fields(n):
        for _ in range(n):
            name = u32(); t = u8()
            value(t, name)
    def value(t, name=None):
        if t in BinReader.PRIM:
            p[0] += BinReader.PRIM[t]; return
        if t == 16:
            n = u16(); p[0] += n; return
        if t in (BinReader.LIST, BinReader.LIST2):
            et = u8(); so = p[0]; size = u32(); end = p[0] + size; cnt = u32()
            if name == H_WEIGHTS and et == 10:
                found.append({"start": p[0], "n": cnt, "sizes": stack + [so], "count_at": p[0] - 4})
            else:
                stack.append(so)
                for _ in range(cnt):
                    value(et)
                stack.pop()
            p[0] = end; return
        if t in (BinReader.POINTER, BinReader.EMBED):
            if u32():
                so = p[0]; size = u32(); end = p[0] + size
                stack.append(so); fields(u16()); stack.pop(); p[0] = end
            return
        if t == BinReader.LINK:
            p[0] += 4; return
        if t == BinReader.OPTION:
            et = u8()
            if u8():
                value(et)
            return
        if t == BinReader.MAP:
            kt = u8(); vt = u8(); so = p[0]; size = u32(); end = p[0] + size; cnt = u32()
            stack.append(so)
            for _ in range(cnt):
                value(kt); value(vt)
            stack.pop(); p[0] = end; return
        if t == BinReader.FLAG:
            p[0] += 1; return
        raise ValueError(f"unknown bin type {t}")
    if d[:4] == b"PTCH":
        p[0] = 16
    if d[p[0]:p[0] + 4] != b"PROP":
        raise ValueError("not a PROP bin")
    p[0] += 4
    if u32() >= 2:
        for _ in range(u32()):
            n = u16(); p[0] += n
    count = u32()
    p[0] += 4 * count
    for _ in range(count):
        so = p[0]; size = u32(); end = p[0] + size
        stack.append(so); u32(); fields(u16()); stack.pop()
        p[0] = end
    return found

def anim_graph_path(layers, bins, champ_id, skin_num):
    """Path of the skin's animation graph bin (whichever copy the game would load: mod first)."""
    c = champ_id.lower()
    cands = [l for b in bins[:1] for l in b.linked if "/animations/" in l.lower()]
    cands += [f"data/characters/{c}/animations/skin{skin_num}.bin", f"data/characters/{c}/animations/skin0.bin"]
    for p in cands:
        if layers.source_of(p):
            return p
    return None

def _mask_mapping(mod_joints, game_joints):
    """For each mod joint, the game-skeleton index whose mask weight it should get (nearest ancestor for new joints)."""
    gidx = {j["hash"]: i for i, j in enumerate(game_joints)}
    out = []
    for i, j in enumerate(mod_joints):
        k, seen = i, 0
        while k is not None and seen < 64:
            g = gidx.get(mod_joints[k]["hash"])
            if g is not None:
                break
            par = mod_joints[k]["parent"]
            k = next((n for n, x in enumerate(mod_joints) if x["id"] == par), None) if par >= 0 else None
            seen += 1
        out.append(g if k is not None else None)
    return out

def mask_check(mod_skl, game_skl, anim_raw):
    """None when the masks fit the skeleton the game will use, else {"mask_len", "joints", "misplaced", "fixable"}."""
    lists = bin_weight_lists(anim_raw)
    if not lists:
        return None
    mj, _ = skl_bind(mod_skl); gj, _ = skl_bind(game_skl)
    L = max(x["n"] for x in lists)
    if [j["hash"] for j in mj] == [j["hash"] for j in gj][:len(mj)] and len(mj) == L:
        return None
    if len(mj) == L and L != len(gj):
        return None        # masks already made for this (custom) skeleton
    m = _mask_mapping(mj, gj)
    misplaced = sum(1 for i, g in enumerate(m) if g != i)
    if not misplaced and len(mj) == L:
        return None
    return {"mask_len": L, "joints": len(mj), "game_joints": len(gj), "misplaced": misplaced, "fixable": L == len(gj)}

def remap_masks(anim_raw, mod_skl, game_skl):
    """Rewrite every mask so weight i belongs to the mod skeleton's joint i. Returns (new bin bytes, masks changed)."""
    mj, _ = skl_bind(mod_skl); gj, _ = skl_bind(game_skl)
    m = _mask_mapping(mj, gj)
    out = bytearray(anim_raw); changed = 0
    for wl in sorted(bin_weight_lists(anim_raw), key=lambda x: -x["start"]):
        old = struct.unpack_from(f"<{wl['n']}f", anim_raw, wl["start"])
        new = [old[g] if g is not None and g < len(old) else 0.0 for g in m]
        blob = struct.pack(f"<{len(new)}f", *new)
        delta = len(blob) - 4 * wl["n"]
        out[wl["start"]:wl["start"] + 4 * wl["n"]] = blob
        struct.pack_into("<I", out, wl["count_at"], len(new))
        for so in wl["sizes"]:
            struct.pack_into("<I", out, so, struct.unpack_from("<I", out, so)[0] + delta)
        changed += 1
    return bytes(out), changed

def skin_mask_check(layers, gread, champ_id, skin_num, skl_p, gskl_p):
    """mask_check for a mod (layers = mod over game) - or None when it fits / there's nothing to check."""
    c = champ_id.lower()
    raw = layers.get(f"data/characters/{c}/skins/skin{skin_num}.bin")
    bins = [BinReader(raw)] if raw else []
    ap = anim_graph_path(layers, bins, champ_id, skin_num)
    if not ap:
        return None
    gskl = gread(gskl_p) if isinstance(gskl_p, str) else None
    if gskl is None:
        return None
    r = mask_check(layers.get(skl_p), gskl, layers.get(ap))
    if r:
        r["graph"] = ap; r["graph_from"] = layers.source_of(ap)
    return r


# ------------------------------------------------------------------ part (submesh) names vs. the game
def skn_names(data):
    """[(name, offset of its 64-byte name field)] for an SKN's submeshes."""
    magic, major, minor = struct.unpack_from("<IHH", data, 0)
    if magic != 0x00112233 or major == 0:
        return []
    n = struct.unpack_from("<I", data, 8)[0]; out = []
    for i in range(n):
        o = 12 + i * 80
        out.append((data[o:o + 64].split(b"\0")[0].decode("latin-1"), o))
    return out

def part_name_fixes(mod_skn, game_skn):
    """{mod name: game name} for parts whose name only differs in upper/lower case. The game matches part names
    exactly (which parts start hidden, which get their own texture), so 'head' isn't hidden when the game says 'Head'."""
    want = {}
    for n, _ in skn_names(game_skn):
        want.setdefault(n.lower(), n)
    exact = {n for n, _ in skn_names(game_skn)}
    return {n: want[n.lower()] for n, _ in skn_names(mod_skn) if n not in exact and n.lower() in want}

def rename_parts(skn, fixes):
    d = bytearray(skn)
    for n, o in skn_names(skn):
        if n in fixes:
            d[o:o + 64] = fixes[n].encode("latin-1")[:63].ljust(64, b"\0")
    return bytes(d)


# ------------------------------------------------------------------ game compatibility check
# The game ties a skin's files together by name, hash, index and path. Each check below follows one of those links
# from the game's side to the mod's side and reports the ones that no longer meet.
_SKIP_NAME_FIELDS = {fnv1a(k) for k in ("submesh", "initialSubmeshToHide", "initialSubmeshAvatarToHide",
                                        "initialSubmeshShadowsToHide", "initialSubmeshMouseOversToHide")}
_GAME_REFS = {}

def _walk_values(x, field, out):
    if isinstance(x, dict):
        for k, v in x.items():
            if k != "__class__":
                _walk_values(v, k, out)
    elif isinstance(x, list):
        for v in x:
            _walk_values(v, field, out)
    else:
        out.append((field, x))

def _skin_bins(getter, champ_id, skin_num, limit=40):
    raw = getter(f"data/characters/{champ_id.lower()}/skins/skin{skin_num}.bin")
    if not raw:
        return []
    bins = [BinReader(raw)]
    for lk in bins[0].linked[:limit]:
        d = getter(lk)
        if d:
            try:
                bins.append(BinReader(d))
            except Exception:
                pass
    return bins

def joint_refs(bins, joints):
    """Joints the skin's game data refers to by name: {joint name: "effects"|"physics"}.
    Strings (effects, health bar, attachments) and FNV-1a name hashes (springs, dynamics, emitters) both count."""
    by_name = {j["name"].lower(): j["name"] for j in joints if j["name"]}
    by_fnv = {fnv1a(j["name"]): j["name"] for j in joints if j["name"]}
    out = {}
    for b in bins:
        vals = []
        for _n, (_ct, f) in b.entries.items():
            _walk_values(f, None, vals)
        for field, v in vals:
            if field in _SKIP_NAME_FIELDS:
                continue
            if isinstance(v, str):
                n = by_name.get(v.lower())
                if n:
                    out[n] = "effects"
            elif isinstance(v, int) and not isinstance(v, bool) and v in by_fnv:
                out.setdefault(by_fnv[v], "physics")
    return out

def _tex_problem(d):
    if d[:4] != b"TEX\0" or len(d) < 12:
        return None
    w, h = struct.unpack_from("<HH", d, 4)
    fmt, mips = d[9], d[11] & 1
    block = {10: 8, 12: 16}.get(fmt)
    if fmt not in (10, 12, 20):
        return None
    if not w or not h:
        return "has a size of 0"
    if block and (w % 4 or h % 4):
        return f"is {w}x{h}; compressed textures must be a multiple of 4 on each side"
    def lvl(ww, hh):
        return max(1, (ww + 3) // 4) * max(1, (hh + 3) // 4) * block if block else ww * hh * 4
    need = 0; ww, hh = w, h
    while True:
        need += lvl(ww, hh)
        if not mips or (ww == 1 and hh == 1):
            break
        ww, hh = max(1, ww // 2), max(1, hh // 2)
    if len(d) - 12 < need:
        return "is cut short (the file is smaller than its size says) - it will look corrupted or not load"
    return None

def compat_check(champ_id, skin_num, game_dir, mod_path):
    """[{level, code, msg}] - links between the mod's files and what the game expects that are broken."""
    gw = game_wad(game_dir, champ_id)
    if not gw:
        return []
    c = champ_id.lower()
    mod = mod_layer(mod_path)
    if not mod:
        return []
    layers = Layers(); layers.add_files(mod, "mod"); layers.add_wad(gw, "game")
    gget = lambda p: gw.read(path_hash(p)) if (path_hash(p) if isinstance(p, str) else p) in gw.entries else None
    out = []
    key = (c, skin_num, game_dir)
    if key not in _GAME_REFS:
        try:
            gbins = _skin_bins(gget, champ_id, skin_num)
            gsmp, _ = find_skin_mesh(gbins)
            gskl_p = gsmp.get(H["skeleton"]) if gsmp else None
            gj = skl_bind(gget(gskl_p))[0] if gskl_p and gget(gskl_p) else []
            _GAME_REFS[key] = {"bins": gbins, "skl": gskl_p, "skn": gsmp.get(H["simpleSkin"]) if gsmp else None,
                               "refs": joint_refs(gbins, gj)}
        except Exception:
            _GAME_REFS[key] = None
    g = _GAME_REFS[key]
    if not g:
        return []
    mbins = _skin_bins(layers.get, champ_id, skin_num) if layers.source_of(f"data/characters/{c}/skins/skin{skin_num}.bin") == "mod" else g["bins"]
    msmp, _ = find_skin_mesh(mbins)
    skl_p = (msmp or {}).get(H["skeleton"]) or g["skl"]
    skn_p = (msmp or {}).get(H["simpleSkin"]) or g["skn"]

    # 1. bones the game attaches effects / health bar / physics to, missing from the mod's own skeleton
    if skl_p and layers.source_of(skl_p) == "mod":
        try:
            have = {j["name"].lower() for j in skl_bind(layers.get(skl_p))[0]}
            miss = {n: k for n, k in g["refs"].items() if n.lower() not in have}
            fx = sorted(n for n, k in miss.items() if k == "effects")
            ph = sorted(n for n, k in miss.items() if k == "physics")
            if fx:
                out.append({"level": "info", "code": "bones-effects", "msg":
                            f"The mod's skeleton is missing {len(fx)} bone(s) the game attaches effects to "
                            f"({', '.join(fx[:5])}{'…' if len(fx) > 5 else ''}). Effects tied to them (spell "
                            "effects, the health bar, attached items) may show up at the feet or not at all."})
            if ph:
                out.append({"level": "info", "code": "bones-physics", "msg":
                            f"The mod's skeleton is missing {len(ph)} bone(s) the game moves with physics "
                            f"({', '.join(ph[:5])}{'…' if len(ph) > 5 else ''}), so hair/cape/tail sway won't work on those parts."})
        except Exception:
            pass

    # 2. model sanity
    if skn_p and layers.source_of(skn_p) == "mod":
        try:
            skn = layers.get(skn_p); m = parse_skn(skn)
            idx = array("H"); idx.frombytes(m["indices"])
            if idx and max(idx) >= m["vcount"]:
                out.append({"level": "warn", "code": "mesh-indices", "msg":
                            "The model points at vertices that don't exist - it will show spiky, stretched triangles or crash the game."})
            pos = array("f"); pos.frombytes(m["positions"])
            if any(v != v or abs(v) > 1e6 for v in pos):
                out.append({"level": "warn", "code": "mesh-nan", "msg": "The model has broken (NaN/huge) vertex positions - parts will vanish or stretch to infinity."})
            info = skn_skinning(skn); zero = 0
            for i in range(0, info["count"], 3):
                o = info["start"] + i * info["stride"]
                if sum(struct.unpack_from("<4f", skn, o + 16)) < 0.01:
                    zero += 1
            if zero * 3 > info["count"] * 0.01:
                out.append({"level": "warn", "code": "mesh-weights", "msg":
                            "Part of the model isn't attached to any bone (zero weights) - in game it will collapse to the floor or the center."})
        except Exception:
            pass

    # 3. animations the game will play that exist nowhere
    try:
        clips = list_clips(layers, mbins, champ_id, skin_num)
        missing = [nm for nm, k in clips if layers.source_of(k) is None]
        if missing and layers.source_of(anim_graph_path(layers, mbins, champ_id, skin_num) or "") == "mod":
            out.append({"level": "warn", "code": "anims-missing", "msg":
                        f"{len(missing)} animation(s) the mod's animation setup asks for don't exist ({', '.join(missing[:4])}"
                        f"{'…' if len(missing) > 4 else ''}) - the champion will freeze or T-pose during those moves."})
    except Exception:
        pass

    # 4. model / skeleton / textures the mod's own skin data points at that exist neither in the mod nor in the game
    if layers.source_of(f"data/characters/{c}/skins/skin{skin_num}.bin") == "mod" and msmp:
        try:
            refs = [msmp.get(H["simpleSkin"]), msmp.get(H["skeleton"]), msmp.get(H["texture"]),
                    material_texture(mbins, msmp[H["material"]]) if msmp.get(H["material"]) else None]
            for o in msmp.get(H["materialOverride"]) or []:
                if isinstance(o, dict):
                    refs.append(o.get(H["texture"]) or (material_texture(mbins, o[H["material"]]) if o.get(H["material"]) else None))
            gone = sorted({(r if isinstance(r, str) else (name_of(r) or f"{r:016x}")).split("/")[-1]
                           for r in refs if r and layers.source_of(r) is None})
            if gone:
                out.append({"level": "warn", "code": "files-missing", "msg":
                            f"The mod's skin data points at {len(gone)} model/texture file(s) that aren't in the mod or the game "
                            f"({', '.join(gone[:4])}{'…' if len(gone) > 4 else ''}) - those parts will be invisible or untextured."})
        except Exception:
            pass

    # 5. textures the game will refuse or show corrupted
    bad = []
    texs = set()
    if msmp:
        for t in [msmp.get(H["texture"]), material_texture(mbins, msmp[H["material"]]) if msmp.get(H["material"]) else None] + \
                 [o.get(H["texture"]) or (material_texture(mbins, o[H["material"]]) if o.get(H["material"]) else None)
                  for o in (msmp.get(H["materialOverride"]) or []) if isinstance(o, dict)]:
            if t:
                texs.add(t if isinstance(t, int) else path_hash(t))
    for h in texs:                       # only the textures the skin actually draws with (reading every file is slow)
        getf = mod.get(h)
        if not getf:
            continue
        try:
            d = getf()
        except Exception:
            continue
        if d[:4] == b"TEX\0":
            p = _tex_problem(d)
            if p:
                bad.append(f"{(name_of(h) or f'{h:016x}').split('/')[-1]} {p}")
    if bad:
        out.append({"level": "warn", "code": "tex-bad", "msg": "Broken texture(s): " + "; ".join(bad[:3]) + ("…" if len(bad) > 3 else "")})
    return out
