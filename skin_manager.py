"""
Skin Vault - League of Legends skin mod manager
-----------------------------------------------
Local web app that indexes a folder of League of Legends skin mods
(.fantome / .zip / .wad.client / extracted folders), works out which champion
and in-game skin each one applies to, flags problems, shows them in 3D, and
installs them into LTK Manager.

Run:  python skin_manager.py      (or double-click "Start Skin Vault.bat")
The browser opens at http://127.0.0.1:8765
"""
import os, sys, re, io, json, time, zlib, struct, array, bisect, shutil, zipfile, socket
import threading, hashlib, difflib, urllib.request, urllib.parse, webbrowser, traceback, subprocess, uuid

VERSION = "1.0.0"
# GitHub "owner/repo" that update checks look at (config.json "update_repo" overrides it)
GITHUB_REPO = "ZuperTheGod/skin-vault"

def ensure_deps():
    """zstandard + xxhash are needed to read the game's own files (3D viewer, skin matching)."""
    missing = []
    for mod in ("zstandard", "xxhash"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    if missing and not os.environ.get("SKINVAULT_NO_PIP"):
        print("Installing", ", ".join(missing), "(one-time)...", flush=True)
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", "--user", "--quiet"] + missing, timeout=300)
        except Exception as e:
            print("Could not install:", e, flush=True)

ensure_deps()
import lol3d
import fixer
import updater
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BUNDLED_DATA = os.path.join(APP_DIR, "data")          # shipped with the app (hash tables, champion list)
HOME_DIR = os.path.abspath(os.environ.get("SKINVAULT_HOME") or APP_DIR)   # where settings/caches/logs live
CONFIG_PATH = os.path.join(HOME_DIR, "config.json")
PORT = 8765

def _looks_like_library(d):
    """A folder with several champion-named subfolders (used to keep old installs working)."""
    try:
        names = {re.sub(r"[^a-z]", "", n.lower()) for n in os.listdir(d)}
    except Exception:
        return False
    champs = {"ahri", "jinx", "lux", "ashe", "yasuo", "zed", "katarina", "evelynn", "akali", "ezreal", "missfortune", "caitlyn"}
    return len(names & champs) >= 3

def load_config():
    cfg = {"library": None, "port": PORT, "open_browser": True}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    if os.environ.get("SKINVAULT_LIBRARY"):
        cfg["library"] = os.environ["SKINVAULT_LIBRARY"]
    if os.environ.get("SKINVAULT_PORT"):
        cfg["port"] = int(os.environ["SKINVAULT_PORT"])
    if not cfg.get("library") and _looks_like_library(os.path.dirname(APP_DIR)):
        cfg["library"] = os.path.dirname(APP_DIR)        # app was unzipped inside the mod folder
    return cfg

def save_config(updates):
    cfg = {}
    try:
        cfg = json.load(open(CONFIG_PATH, encoding="utf-8"))
    except Exception:
        pass
    cfg.update(updates)
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    json.dump(cfg, open(CONFIG_PATH, "w", encoding="utf-8"), indent=1)
    CFG.update(updates)

CFG = load_config()
SETUP_MODE = not (CFG.get("library") and os.path.isdir(CFG["library"]))
LIB = os.path.abspath(CFG["library"]) if not SETUP_MODE else os.path.join(HOME_DIR, "_no_library_yet")
DATA_DIR = os.path.join(HOME_DIR, "data")
LOG_DIR = os.path.join(HOME_DIR, "logs")
DROP_DIR = os.path.join(LIB, "_Drop Here")
DUP_DIR = os.path.join(LIB, "_Duplicates")
UNSORTED_DIR = os.path.join(LIB, "_Unsorted")
PACKS_DIR = os.path.join(LIB, "_Imported Packs")
INCOMING_DIR = os.path.join(HOME_DIR, "incoming")
for d in ([DATA_DIR, LOG_DIR, INCOMING_DIR] + ([] if SETUP_MODE else [DROP_DIR])):
    os.makedirs(d, exist_ok=True)
lol3d.DATA_DIR = DATA_DIR
lol3d.BUNDLED_DATA = BUNDLED_DATA

def data_file(name):
    """A data file from the user's data folder, falling back to the copy shipped with the app."""
    p = os.path.join(DATA_DIR, name)
    return p if os.path.exists(p) else os.path.join(BUNDLED_DATA, name)

APP_NAME = os.path.basename(APP_DIR)
# Top-level folders that are never treated as mods / never walked
SKIP_TOP = {APP_NAME.lower(), os.path.basename(HOME_DIR).lower(), "_drop here", "_duplicates", "_imported packs",
            "_originals", "_deleted", "assets", "data"}
ORIGINALS_DIR = os.path.join(LIB, "_Originals")
WORK_DIR = os.path.join(HOME_DIR, "work")
UNPACKED_DIR = os.path.join(HOME_DIR, "unpacked")
MOD_EXTS = (".fantome", ".zip", ".wad.client", ".wad")
UNREADABLE_EXTS = (".rar", ".7z")
NON_CHAMP_CATEGORIES = {"map": "Maps", "maps": "Maps", "items": "Items", "emotes": "Emotes",
                        "other": "Other", "skin": "Unsorted", "_unsorted": "Unsorted"}

def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)

# ---------------------------------------------------------------- Data Dragon
DD = {"version": None, "champs": {}, "by_lower": {}}
CD_CACHE = {}

def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "LoLSkinModManager/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def load_ddragon(force=False):
    path = os.path.join(DATA_DIR, "championFull.json")
    if not os.path.exists(path) and os.path.exists(os.path.join(BUNDLED_DATA, "championFull.json")):
        shutil.copy2(os.path.join(BUNDLED_DATA, "championFull.json"), path)
    data = None
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
    except Exception:
        data = None
    stale = data is None or force or (time.time() - os.path.getmtime(path) > 3 * 86400)
    if stale:
        try:
            ver = json.loads(http_get("https://ddragon.leagueoflegends.com/api/versions.json"))[0]
            if force or data is None or data.get("version") != ver:
                log("Downloading champion data", ver)
                raw = http_get(f"https://ddragon.leagueoflegends.com/cdn/{ver}/data/en_US/championFull.json", 120)
                data = json.loads(raw)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            else:
                os.utime(path, None)
        except Exception as e:
            log("Could not refresh Data Dragon:", e)
    if data is None:
        log("WARNING: no champion data (offline on first run?)")
        return
    champs = {}
    for cid, c in data["data"].items():
        champs[cid] = {
            "id": cid, "key": int(c["key"]), "name": c["name"], "title": c["title"],
            "icon": c["image"]["full"],
            "skins": [{"num": s["num"], "name": ("Default" if s["name"] == "default" else s["name"]),
                       "chromas": s.get("chromas", False)} for s in c["skins"]],
        }
    DD["version"] = data.get("version")
    DD["champs"] = champs
    DD["by_lower"] = {k.lower(): k for k in champs}
    build_name_index()

def cdragon_skin_names(champ_id):
    """Chroma / unlisted skin names from CommunityDragon (cached)."""
    if champ_id in CD_CACHE:
        return CD_CACHE[champ_id]
    names = {}
    c = DD["champs"].get(champ_id)
    if not c:
        return names
    path = os.path.join(DATA_DIR, f"cd_{champ_id}.json")
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < 14 * 86400:
            raw = open(path, "rb").read()
        else:
            raw = http_get(f"https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/champions/{c['key']}.json")
            open(path, "wb").write(raw)
        j = json.loads(raw)
        for s in j.get("skins", []):
            names[s["id"] % 1000] = {"name": s["name"], "chroma_of": None}
            for ch in s.get("chromas", []) or []:
                names[ch["id"] % 1000] = {"name": ch["name"], "chroma_of": s["id"] % 1000}
            for tier in (s.get("questSkinInfo") or {}).get("tiers", []) or []:
                names[tier["id"] % 1000] = {"name": tier["name"], "chroma_of": None}
    except Exception as e:
        log("CommunityDragon lookup failed for", champ_id, e)
    CD_CACHE[champ_id] = names
    return names

def skin_label(champ_id, num):
    c = DD["champs"].get(champ_id)
    if not c or num is None:
        return None
    for s in c["skins"]:
        if s["num"] == num:
            return s["name"]
    extra = cdragon_skin_names(champ_id).get(num)
    if extra:
        if extra["chroma_of"] is not None:
            return f'{extra["name"]} (chroma)'
        return extra["name"]
    return f"Skin #{num}"

# ------------------------------------------------------- champion name matching
ALIASES = {
    "fiddles": "Fiddlesticks", "fiddle": "Fiddlesticks", "raka": "Soraka", "pant": "Pantheon", "panth": "Pantheon",
    "veigo": "Viego", "yummi": "Yuumi", "mordkaiser": "Mordekaiser", "morde": "Mordekaiser", "mf": "MissFortune",
    "tf": "TwistedFate", "wukong": "MonkeyKing", "j4": "JarvanIV", "jarvan": "JarvanIV", "lee": "LeeSin",
    "yi": "MasterYi", "kog": "KogMaw", "cait": "Caitlyn", "asol": "AurelionSol", "ez": "Ezreal",
    "morg": "Morgana", "blitz": "Blitzcrank", "heimer": "Heimerdinger", "kass": "Kassadin", "kat": "Katarina",
    "liss": "Lissandra", "malz": "Malzahar", "noc": "Nocturne", "ori": "Orianna", "sej": "Sejuani",
    "tahm": "TahmKench", "trynd": "Tryndamere", "vlad": "Vladimir", "voli": "Volibear", "ww": "Warwick",
    "xin": "XinZhao", "gp": "Gangplank", "cho": "Chogath", "kha": "Khazix", "reksai": "RekSai",
    "nunu": "Nunu", "willump": "Nunu", "renata": "Renata", "glasc": "Renata", "seraph": "Seraphine",
    "sera": "Seraphine", "vel": "Velkoz", "kaisa": "Kaisa", "belveth": "Belveth", "ksante": "KSante",
    "aurelion": "AurelionSol", "jarvaniv": "JarvanIV", "dr mundo": "DrMundo", "mundo": "DrMundo",
    "naut": "Nautilus", "nid": "Nidalee", "shyv": "Shyvana", "trist": "Tristana", "zil": "Zilean",
}
NAME_INDEX = {}   # normalized token -> champ id

def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())

def build_name_index():
    NAME_INDEX.clear()
    for cid, c in DD["champs"].items():
        NAME_INDEX[norm(cid)] = cid
        NAME_INDEX[norm(c["name"])] = cid
    for a, cid in ALIASES.items():
        if cid in DD["champs"]:
            NAME_INDEX.setdefault(norm(a), cid)

def resolve_champ_name(name, fuzzy=True):
    """Exact-ish match of a whole string (folder name) to a champion id."""
    n = norm(name)
    if not n:
        return None
    if n in NAME_INDEX:
        return NAME_INDEX[n]
    if fuzzy and len(n) >= 4:
        m = difflib.get_close_matches(n, list(NAME_INDEX.keys()), n=1, cutoff=0.8)
        if m:
            return NAME_INDEX[m[0]]
    return None

def find_champ_in_text(text):
    """Find a champion mentioned anywhere in free text (mod name, file name)."""
    if not text:
        return None
    words = [w for w in re.split(r"[^A-Za-z0-9']+", text.replace("_", " ")) if w]
    words = [re.sub(r"'", "", w) for w in words]
    best = None
    # longest n-gram first (e.g. "Miss Fortune", "Tahm Kench", "Aurelion Sol")
    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            n = norm("".join(words[i:i + size]))
            if len(n) < 3 and size == 1 and n not in ("mf", "tf", "j4", "ww", "gp", "ez", "yi"):
                continue
            if n in NAME_INDEX:
                return NAME_INDEX[n]
        if best:
            return best
    return None

# ------------------------------------------------------------ WAD hash database
class HashDB:
    def __init__(self):
        self.loaded = False
        self.names = []
        self.H = self.C = self.S = self.K = None

    def load(self):
        path = data_file("skinhashes3.bin")
        if not os.path.exists(path):
            log("skinhashes.bin missing - skin detection inside WADs will be limited")
            return
        blob = zlib.decompress(open(path, "rb").read())
        magic, n, ln = struct.unpack_from("<4sII", blob, 0)
        off = 12
        self.names = blob[off:off + ln].decode().split(","); off += ln
        self.H = array.array("Q"); self.H.frombytes(blob[off:off + 8 * n]); off += 8 * n
        self.C = array.array("H"); self.C.frombytes(blob[off:off + 2 * n]); off += 2 * n
        self.S = array.array("h"); self.S.frombytes(blob[off:off + 2 * n]); off += 2 * n
        if magic in (b"SKH2", b"SKH3"):
            self.K = array.array("B"); self.K.frombytes(blob[off:off + n])
        self.loaded = True
        log(f"Hash database loaded: {n:,} known champion file paths")

    def lookup(self, h):
        if not self.loaded:
            return None
        i = bisect.bisect_left(self.H, h)
        if i < len(self.H) and self.H[i] == h:
            return self.names[self.C[i]], self.S[i], (self.K[i] if self.K is not None else 0)
        return None

HDB = HashDB()

def rebuild_hashdb(src_file=None):
    """Download CommunityDragon's hash list and rebuild data/skinhashes3.bin + data/texnames.bin
    (run after big patches so new skins are recognised)."""
    tmp = src_file
    if not tmp:
        url = "https://raw.communitydragon.org/data/hashes/lol/hashes.game.txt"
        log("Downloading hash list (~230 MB) ...")
        tmp = os.path.join(DATA_DIR, "hashes.game.txt.tmp")
        req = urllib.request.Request(url, headers={"User-Agent": "SkinVault/" + VERSION})
        with urllib.request.urlopen(req, timeout=600) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
    texnames = []
    tex_rx = re.compile(r"^assets/characters/([a-z0-9_]+)/skins/.*\.(tex|dds|skn)$")
    ids = {k.lower() for k in DD["champs"]}
    rx = re.compile(r"^(?:assets|data)/characters/([a-z0-9_]+)/(?:skins/(base|skin(\d+))|.*)")
    rows = []
    with open(tmp, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            h, _, p = line.rstrip("\n").partition(" ")
            m = rx.match(p)
            if not m or m.group(1) not in ids:
                continue
            k = file_kind(p)
            mb = re.search(r"^data/characters/[a-z0-9_]+/(?:skins|animations)/skin(\d+)\.bin$", p)
            if mb:
                s = int(mb.group(1))
            elif m.group(2) == "base":
                s = 0
            elif m.group(3):
                s = int(m.group(3))
            else:
                s = -1
            rows.append((int(h, 16), m.group(1), s, k))
            if tex_rx.match(p) and "/particles/" not in p:
                texnames.append(p)
    names = sorted({r[1] for r in rows}); idx = {c: i for i, c in enumerate(names)}
    rows.sort()
    H = array.array("Q", [r[0] for r in rows]); C = array.array("H", [idx[r[1]] for r in rows])
    S = array.array("h", [max(-1, min(r[2], 32000)) for r in rows])
    K = array.array("B", [r[3] for r in rows])
    nb = ",".join(names).encode()
    blob = struct.pack("<4sII", b"SKH3", len(rows), len(nb)) + nb + H.tobytes() + C.tobytes() + S.tobytes() + K.tobytes()
    open(os.path.join(DATA_DIR, "skinhashes3.bin"), "wb").write(zlib.compress(blob, 9))
    open(os.path.join(DATA_DIR, "texnames.bin"), "wb").write(zlib.compress("\n".join(texnames).encode(), 9))
    if not src_file:
        os.remove(tmp)
    lol3d._NAMES = None
    HDB.load()

# ---------------------------------------------------------------- WAD parsing
def wad_toc(f):
    """Read a WAD header + table of contents from a file-like object.
    Returns (version_str, [path_hash, ...]) or raises ValueError."""
    head = f.read(4)
    if len(head) < 4 or head[:2] != b"RW":
        raise ValueError("not a WAD file")
    major, minor = head[2], head[3]
    if major == 1:
        toc_off, esize, count = struct.unpack("<HHI", f.read(8))
        consumed = 12
    elif major == 2:
        rest = f.read(84 + 8 + 8)
        toc_off, esize, count = struct.unpack_from("<HHI", rest, 92)
        consumed = 4 + len(rest)
    elif major == 3:
        rest = f.read(268)
        count = struct.unpack_from("<I", rest, 264)[0]
        toc_off, esize, consumed = 272, 32, 272
    else:
        raise ValueError(f"unknown WAD version {major}.{minor}")
    if count > 500000:
        raise ValueError("corrupt WAD header")
    if toc_off > consumed:
        f.read(toc_off - consumed)
    toc = f.read(esize * count)
    if len(toc) < esize * count:
        raise ValueError("WAD is truncated")
    hashes = [struct.unpack_from("<Q", toc, i * esize)[0] for i in range(count)]
    return f"{major}.{minor}", hashes

RAW_RX = re.compile(r"(?:^|/)characters/([a-z0-9_]+)/(?:skins/(base|skin(\d+))|.*?/?skin(\d+)\.bin$)?", re.I)
GAME_FILE_EXTS = (".anm", ".skn", ".skl", ".dds", ".tex", ".bin", ".bnk", ".wpk", ".scb", ".sco", ".mapgeo", ".troybin", ".luaobj")
LOCALE_RX = re.compile(r"\.[a-z]{2}_[a-z]{2}\.wad", re.I)

KIND_NAMES = {1: "skin data", 2: "animation data", 3: "model", 4: "skeleton", 5: "texture", 6: "animation"}
KIND_WEIGHT = {1: 0, 2: 0, 3: 10, 4: 2, 5: 3, 6: 0.2, 0: 0.5}

def file_kind(p):
    p = p.lower()
    if re.search(r"(?:^|/)skins/skin\d+\.bin$", p):
        return 1
    if re.search(r"(?:^|/)animations/skin\d+\.bin$", p):
        return 2
    return {"skn": 3, "skl": 4, "dds": 5, "tex": 5, "anm": 6}.get(p.rsplit(".", 1)[-1], 0)

def classify_raw_path(p):
    p = p.replace("\\", "/").lower()
    hx = re.match(r"^(?:.*/)?([0-9a-f]{16})(\.\w+)?$", p)
    if hx:  # extracted WAD file named by its hash
        return HDB.lookup(int(hx.group(1), 16))
    mb = re.search(r"(?:^|/)characters/([a-z0-9_]+)/skins/skin(\d+)\.bin$", p)
    if mb:
        return mb.group(1), int(mb.group(2)), 1
    ma = re.search(r"(?:^|/)characters/([a-z0-9_]+)/animations/skin(\d+)\.bin$", p)
    if ma:
        return ma.group(1), int(ma.group(2)), 2
    m = RAW_RX.search(p)
    if not m:
        return None
    champ = m.group(1)
    k = file_kind(p)
    if m.group(2) == "base":
        return champ, 0, k
    if m.group(3):
        return champ, int(m.group(3)), k
    return champ, -1, k

# ---------------------------------------------------------------- analysis
class Collector:
    def __init__(self):
        self.wads = []          # (name, version)
        self.hits = {}          # champ -> {skin: count}
        self.bins = {}          # champ -> {skin numbers whose skin .bin the mod replaces}
        self.folders = {}       # champ -> {skin: {kind: count}}
        self.anim_bins = {}
        self.raw_count = 0
        self.total_entries = 0
        self.unknown_hashes = 0
        self.errors = []
        self.sig = hashlib.md5()
        self.meta = None
        self.image = None       # (container member path) or file path
        self.children = []      # nested mods (packs)
        self.has_content = False
        self.loose_files = 0
        self.voice_paths = 0
        self.hashes = set()     # path hashes of every file the mod ships

    def add_hit(self, champ, skin, kind=0):
        d = self.hits.setdefault(champ, {})
        d[skin] = d.get(skin, 0) + 1
        if skin >= 0:   # which skin folder (skins/base, skins/skin07, ...) holds what kind of file
            f = self.folders.setdefault(champ, {}).setdefault(skin, {})
            f[kind] = f.get(kind, 0) + 1
        if kind == 1:
            self.bins.setdefault(champ, set()).add(skin)
        elif kind == 2:
            self.anim_bins.setdefault(champ, set()).add(skin)

    def add_wad(self, name, fobj):
        try:
            ver, hashes = wad_toc(fobj)
        except Exception as e:
            self.errors.append(f"Broken WAD '{name}': {e}")
            return
        self.wads.append((name, ver))
        self.has_content = True
        self.total_entries += len(hashes)
        self.hashes.update(hashes)
        for h in sorted(hashes):
            self.sig.update(struct.pack("<Q", h))
            r = HDB.lookup(h)
            if r:
                self.add_hit(*r)
            else:
                self.unknown_hashes += 1

    def add_loose(self, relpath):
        self.loose_files += 1
        self.sig.update(relpath.lower().encode("utf-8", "replace"))
        r = classify_raw_path(relpath)
        if r:
            self.add_hit(*r)

    def _hash_rel(self, relpath):
        rp = relpath.replace("\\", "/")
        hx = re.match(r"^(?:.*/)?([0-9a-fA-F]{16})(\.\w+)?$", rp)
        self.hashes.add(int(hx.group(1), 16) if hx else lol3d.path_hash(rp))

    def add_raw(self, relpath):
        if "/sounds/" in relpath.lower().replace("\\", "/") or relpath.lower().startswith("assets/sounds"):
            self.voice_paths += 1
        self.has_content = True
        self.raw_count += 1
        self.total_entries += 1
        self._hash_rel(relpath)
        self.sig.update(relpath.lower().encode("utf-8", "replace"))
        r = classify_raw_path(relpath)
        if r:
            self.add_hit(*r)

def read_meta_json(raw):
    try:
        txt = raw.decode("utf-8-sig", "replace")
        return json.loads(txt)
    except Exception:
        try:
            return json.loads(re.sub(r",\s*}", "}", raw.decode("latin-1")))
        except Exception:
            return None

def scan_zip(zf, col, depth=0):
    names = [(i, i.filename.replace("\\", "/")) for i in zf.infolist()]
    # find META root (some archives wrap everything in a top folder)
    root = ""
    for i, n in names:
        if n.lower().endswith("meta/info.json"):
            root = n[: -len("meta/info.json")]
            break
    nested = [(i, n) for i, n in names if not i.is_dir() and n.lower().endswith((".fantome", ".zip", ".wad.client")) and
              not n[len(root):].lower().startswith("wad/") and ".wad.client/" not in n.lower()]
    nested_archives = [(i, n) for i, n in nested if n.lower().endswith((".fantome", ".zip"))]
    for i, n in names:
        ln = n.lower()
        rel = n[len(root):] if n.startswith(root) else n
        rl = rel.lower()
        if i.is_dir():
            continue
        if rl == "meta/info.json":
            col.meta = read_meta_json(zf.read(i))
        elif rl.startswith("meta/") and rl.endswith((".png", ".jpg", ".jpeg")):
            col.image = col.image or n
        elif rl.startswith("wad/"):
            inner = rel[4:]
            if "/" in inner:  # extracted WAD folder: WAD/Ahri.wad.client/data/...
                wadname, sub = inner.split("/", 1)
                if (wadname, "raw") not in col.wads:
                    col.wads.append((wadname, "raw"))
                col.add_raw(sub)
            elif rl.endswith((".wad.client", ".wad")):
                try:
                    with zf.open(i) as f:
                        col.add_wad(inner, f)
                except Exception as e:
                    col.errors.append(f"Could not read '{inner}': {e}")
        elif rl.startswith("raw/"):
            col.add_raw(rel[4:])
        elif rl.endswith((".wad.client",)) and (i, n) in nested:
            try:
                with zf.open(i) as f:
                    col.add_wad(os.path.basename(n), f)
            except Exception as e:
                col.errors.append(f"Could not read '{n}': {e}")
        elif re.match(r"(assets|data)/", rl):
            col.add_raw(rel)
        elif ln.endswith(GAME_FILE_EXTS):
            col.add_loose(rel)
    if nested_archives and depth == 0:
        for i, n in nested_archives:
            child = {"name": os.path.basename(n), "member": n, "size": i.file_size}
            if i.file_size < 300 * 1024 * 1024:
                try:
                    with zf.open(i) as f:
                        data = f.read()
                    cc = Collector()
                    with zipfile.ZipFile(io.BytesIO(data)) as z2:
                        scan_zip(z2, cc, depth + 1)
                    child["col"] = cc
                except Exception as e:
                    child["error"] = str(e)
            col.children.append(child)

def scan_dir(path, col):
    for dp, dns, fns in os.walk(path):
        for fn in fns:
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, path).replace("\\", "/")
            rl = rel.lower()
            if rl == "meta/info.json":
                try:
                    col.meta = read_meta_json(open(full, "rb").read())
                except Exception:
                    pass
            elif rl.startswith("meta/") and rl.endswith((".png", ".jpg", ".jpeg")):
                col.image = col.image or full
            elif rl.endswith((".wad.client", ".wad")) and os.path.isfile(full):
                try:
                    with open(full, "rb") as f:
                        col.add_wad(fn, f)
                except Exception as e:
                    col.errors.append(f"Could not read '{fn}': {e}")
            elif rl.startswith("raw/"):
                col.add_raw(rel[4:])
            elif ".wad.client/" in rl:
                col.add_raw(rl.split(".wad.client/", 1)[1])
            elif re.match(r"(assets|data)/", rl):
                col.add_raw(rel)
            elif rl.endswith(GAME_FILE_EXTS):
                col.add_loose(rel)

def tokens(s):
    return [w for w in re.split(r"[^a-z0-9]+", (s or "").lower()) if w]

def infer_skin_from_text(champ_id, *texts):
    """Guess which skin a mod *looks like* from its name/description."""
    c = DD["champs"].get(champ_id)
    if not c:
        return None
    joined = " ".join(t for t in texts if t)
    m = re.search(r"skin\s*_?(\d{1,3})\b", joined, re.I)
    if m:
        return int(m.group(1))
    tk = set(tokens(joined))
    champ_tk = set(tokens(c["name"])) | set(tokens(champ_id))
    best, best_score = None, 0
    for s in c["skins"]:
        if s["num"] == 0:
            continue
        st = [w for w in tokens(s["name"]) if w not in champ_tk]
        if not st:
            continue
        hit = sum(1 for w in st if w in tk)
        score = hit / len(st)
        if hit and score > best_score or (score == best_score and hit > 0 and best is not None and len(st) > 1):
            best, best_score = s["num"], score
    if best is not None and best_score >= 0.99:
        return best
    if best is not None and best_score >= 0.5:
        return best
    if re.search(r"\b(base|default|classic)\b", joined, re.I):
        return 0
    return None

def finalize(col, display_name, context_champ=None):
    """Turn collected facts into a mod record (no path info)."""
    meta = col.meta or {}
    name = (meta.get("Name") or "").strip() or display_name
    desc = (meta.get("Description") or "").strip()
    rec = {"name": name, "author": (meta.get("Author") or "").strip(), "version": str(meta.get("Version") or "").strip(),
           "description": desc, "wads": [w[0] for w in col.wads], "wad_versions": sorted({w[1] for w in col.wads}),
           "entries": col.total_entries, "has_image": bool(col.image), "issues": [], "has_meta": col.meta is not None}
    issues = rec["issues"]
    for e in col.errors:
        issues.append({"level": "error", "msg": e})

    # champion: WAD file name > hash hits > name text > folder context
    champ = None; champ_src = None
    for w, _ in col.wads:
        stem = w.split(".")[0]
        cid = DD["by_lower"].get(stem.lower())
        if cid:
            champ, champ_src = cid, "wad"
            break
    hit_champs = {DD["by_lower"].get(k): v for k, v in col.hits.items() if DD["by_lower"].get(k)}
    if not champ and hit_champs:
        champ = max(hit_champs, key=lambda k: sum(hit_champs[k].values())); champ_src = "content"
    text_champ = find_champ_in_text(name) or find_champ_in_text(display_name)
    if not champ and text_champ:
        champ, champ_src = text_champ, "name"
    if not champ and context_champ and not col.wads:
        champ, champ_src = context_champ, "folder"
    rec["champ"] = champ; rec["champ_source"] = champ_src
    others = sorted(k for k in hit_champs if k != champ)
    if others and champ:
        rec["also_affects"] = others

    # mod type
    allnames = " ".join([name, display_name] + rec["wads"]).lower()
    mtype = "Skin"
    if any(LOCALE_RX.search(w) for w in rec["wads"]) or re.search(r"\b(voice|vo|sfx|sound|audio)\b", allnames) or (col.raw_count and col.voice_paths / col.raw_count > 0.7):
        mtype = "Voice / SFX"
    elif re.search(r"load\s*-?screen|loadscreen", allnames):
        mtype = "Loading screen"
    elif not champ:
        if re.search(r"\bmap\d*|summoner|rift|aram|howling", allnames):
            mtype = "Map"
        elif re.search(r"\b(ui|hud|font|announcer)\b", allnames):
            mtype = "UI"
        elif re.search(r"\bemote", allnames):
            mtype = "Emote"
        elif re.search(r"\bitem|ward\b", allnames):
            mtype = "Items"
        else:
            mtype = "Other"
    rec["type"] = mtype

    # skins actually touched (in-game slot)
    skins = sorted(s for s, n in hit_champs.get(champ, {}).items() if s >= 0) if champ else []
    rec["skins"] = skins
    looks = infer_skin_from_text(champ, name, display_name, desc) if champ else None
    rec["looks_like"] = looks
    applies = None; src = None
    counts = {s: n for s, n in hit_champs.get(champ, {}).items() if s >= 0} if champ else {}
    bin_skins = sorted(col.bins.get(champ.lower(), set())) if champ else []
    total = sum(counts.values()) or 1
    top = max(counts, key=lambda s: (counts[s], s != 0)) if counts else None
    if bin_skins:
        # The skin .bin decides which in-game skin a mod lives on - it points the game at the mod's model/textures.
        rec["skins"] = skins = bin_skins
        if len(bin_skins) == 1:
            applies, src = bin_skins[0], "bin"
        elif looks in bin_skins:
            applies, src = looks, "bin+name"
        else:
            applies, src = None, "multi"
    else:
        # Which skin folder the mod's files live in: skins/base -> Default, skins/skin07 -> skin 7, ...
        # Models and textures count far more than animations (mods often ship base animations too).
        fold = col.folders.get(champ.lower(), {}) if champ else {}
        fscore = {n: sum(KIND_WEIGHT.get(k, 0.5) * c for k, c in kinds.items()) for n, kinds in fold.items()}
        if fscore:
            fbest = max(fscore, key=lambda n: (fold[n].get(3, 0) > 0, fscore[n], n == looks, n != 0))
            rivals = [n for n in fscore if n != fbest and fscore[n] >= fscore[fbest] * 0.8 and fold[n].get(3, 0) == fold[fbest].get(3, 0)]
            if rivals and looks in rivals + [fbest]:
                fbest = looks
            if rivals and len(rivals) >= 3 and looks is None:
                applies, src = None, "multi"
            else:
                applies, src = fbest, "folder"
        elif looks is not None and mtype == "Skin":
            applies, src = looks, "name"
    folder_pick = applies if src == "folder" else None
    # Ground truth: compare against what each official skin actually loads in the installed game.
    rec["outdated"] = False
    if champ and mtype == "Skin" and col.hashes:
        try:
            refs = lol3d.skin_refs(game_dir(), champ)
        except Exception:
            refs = None
        if refs:
            ms = lol3d.match_skins(refs, col.hashes)
            for n in bin_skins:
                sc = ms.get(n, (0, False, False))
                ms[n] = (sc[0] + 5, True, sc[2])
            if ms:
                official = {s["num"] for s in DD["champs"][champ]["skins"]}
                best = max(ms, key=lambda n: (ms[n][0], ms[n][1], n == folder_pick, n in official, n == looks, -n))
                tops = sorted(n for n in ms if ms[n][0] == ms[best][0] and n != best)
                # trust the game match when it's about the model/textures; a skeleton-only match
                # doesn't beat the folder the mod's model/textures are in
                if ms[best][1] or ms[best][2] or folder_pick is None:
                    applies, src = best, "game"
                if src == "game" and folder_pick is not None and folder_pick != best:
                    rec["folder_disagrees"] = folder_pick
                rec["skins"] = skins = sorted(ms)
                if tops:
                    rec["also_slots"] = tops
            elif col.has_content and not bin_skins:
                rec["outdated"] = True
    rec["applies_to"] = applies; rec["applies_source"] = src
    # folder breakdown for the UI: [{"skin": 7, "folder": "skin07", "model": 1, "texture": 4, ...}]
    fl = []
    for n, kinds in sorted((col.folders.get(champ.lower(), {}) if champ else {}).items()):
        row = {"skin": n, "folder": "base" if n == 0 else f"skin{n:02d}"}
        for k, c in kinds.items():
            nm = KIND_NAMES.get(k, "other")
            row[nm] = row.get(nm, 0) + c
        fl.append(row)
    rec["folders"] = fl

    # issues
    if not col.has_content and not col.children:
        if col.loose_files:
            issues.append({"level": "error", "msg": f"Raw game files ({col.loose_files}) not packaged as a mod - open cs:lol Manager > Create new mod and add these files, then drop the result here"})
        else:
            issues.append({"level": "error", "msg": "No game files inside - this isn't a usable mod"})
    if rec.get("folder_disagrees") is not None and champ:
        issues.append({"level": "info", "msg": f"Its files are in the {('base' if rec['folder_disagrees']==0 else 'skin%02d' % rec['folder_disagrees'])} folder, "
                       f"but the game loads them on {skin_label(champ, applies)} - pick that one in game"})
    if rec.get("outdated"):
        issues.append({"level": "warn", "msg": "Outdated: none of its files are used by your current game version - it most likely won't show up in game (check the 3D view)"})
    if rec.get("also_slots") and champ:
        names_ = [skin_label(champ, n) for n in rec["also_slots"][:6]]
        issues.append({"level": "info", "msg": "Also shows on: " + ", ".join(n for n in names_ if n)})
    if col.children:
        rec["type"] = f"Pack ({len(col.children)} mods)"
        issues.append({"level": "info", "msg": f"Pack containing {len(col.children)} mods - use 'Unpack' to split it into separate mods"})
    if any(v.startswith(("1.", "2.")) for v in rec["wad_versions"]):
        issues.append({"level": "warn", "msg": "Uses an old WAD format (v1/v2) - very likely won't load on current patches"})
    if col.meta is None and not col.children and col.has_content:
        issues.append({"level": "info", "msg": "No META/info.json - name/author unknown"})
    if not champ and mtype in ("Skin", "Other"):
        issues.append({"level": "warn", "msg": "Couldn't tell which champion this is for - assign it manually"})
    if champ and mtype == "Skin" and applies is None:
        if src == "multi":
            issues.append({"level": "info", "msg": f"Changes {len(skins)} skins at once"})
        else:
            issues.append({"level": "warn", "msg": "Couldn't tell which in-game skin this replaces"})
    if champ and applies is not None and looks is not None and looks != applies and src in ("content", "bin", "folder", "game"):
        issues.append({"level": "info", "msg": f"Looks like {skin_label(champ, looks)}, but you must pick {skin_label(champ, applies)} in game"})
    if rec.get("also_affects"):
        issues.append({"level": "info", "msg": "Also contains files for: " + ", ".join(DD['champs'][c]['name'] for c in rec['also_affects'][:5])})
    if col.total_entries and HDB.loaded and col.wads and col.unknown_hashes == col.total_entries and not col.raw_count:
        if mtype == "Skin" and champ:
            issues.append({"level": "info", "msg": "Contents use unknown/custom file paths - detection based on name only"})
    rec["content_sig"] = col.sig.hexdigest() if col.total_entries else None
    if champ and applies is not None:
        rec["applies_label"] = skin_label(champ, applies)
    if champ and looks is not None:
        rec["looks_label"] = skin_label(champ, looks)
    return rec

def fingerprint(path, size):
    h = hashlib.md5(str(size).encode())
    try:
        with open(path, "rb") as f:
            h.update(f.read(256 * 1024))
            if size > 512 * 1024:
                f.seek(-256 * 1024, 2)
                h.update(f.read())
    except Exception:
        return None
    return h.hexdigest()

def analyze_path(path, context_champ=None):
    """Analyze one mod unit (file or folder). Returns record dict."""
    is_dir = os.path.isdir(path)
    base = os.path.basename(path.rstrip("\\/"))
    lower = base.lower()
    col = Collector()
    display = re.sub(r"\.(fantome|zip|wad\.client|wad|rar|7z)$", "", base, flags=re.I)
    display = re.sub(r"[_]+", " ", display).strip()
    rec_extra = {}
    try:
        if is_dir:
            scan_dir(path, col)
        elif lower.endswith((".fantome", ".zip")):
            try:
                with zipfile.ZipFile(path) as zf:
                    bad = None
                    scan_zip(zf, col)
            except zipfile.BadZipFile:
                col.errors.append("Archive is corrupted or not a real zip/fantome (re-download it)")
        elif lower.endswith((".wad.client", ".wad")):
            with open(path, "rb") as f:
                col.add_wad(base, f)
        elif lower.endswith(UNREADABLE_EXTS):
            col.errors.append(f"Can't read {os.path.splitext(lower)[1]} archives - extract it, then drop the .fantome/.zip in")
            col.has_content = True
    except Exception as e:
        col.errors.append(f"Read error: {e}")
    wrapped = None
    if len(col.children) == 1 and "col" in col.children[0] and not col.has_content:
        ch = col.children[0]
        wrapped = ch["member"]
        errs = col.errors
        col = ch["col"]; col.errors = errs + col.errors
        col.image = None
        display = re.sub(r"\.(fantome|zip)$", "", os.path.basename(ch["member"]), flags=re.I)
    rec = finalize(col, display, context_champ)
    if wrapped:
        rec["wrapped"] = wrapped
        rec["issues"].append({"level": "info", "msg": f"Zip wrapping {os.path.basename(wrapped)} - Organize/Import unwraps it automatically"})
    rec["kind"] = "folder" if is_dir else "file"
    rec["filename"] = base
    rec["children"] = []
    for ch in col.children:
        c = {"name": ch["name"], "member": ch["member"], "size": ch["size"]}
        if "col" in ch:
            r2 = finalize(ch["col"], re.sub(r"\.(fantome|zip)$", "", ch["name"], flags=re.I))
            c.update({k: r2.get(k) for k in ("champ", "applies_to", "applies_label", "type", "name", "author")})
        if "error" in ch:
            c["error"] = ch["error"]
        rec["children"].append(c)
    rec["_image_ref"] = col.image
    return rec

# ---------------------------------------------------------------- library scan
STATE = {"status": "idle", "progress": 0, "total": 0, "current": "", "last_scan": None,
         "mods": {}, "folders": {}, "loose": [], "events": [], "scan_seconds": 0}
LOCK = threading.RLock()
CACHE_PATH = os.path.join(DATA_DIR, "scan_cache.json")
SCAN_VERSION = 7

def load_cache():
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            c = json.load(f)
        if c.get("v") == SCAN_VERSION and c.get("dd") == DD["version"]:
            return c.get("items", {})
    except Exception:
        pass
    return {}

def save_cache(items):
    try:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": SCAN_VERSION, "dd": DD["version"], "items": items}, f)
        os.replace(tmp, CACHE_PATH)
    except Exception as e:
        log("cache save failed", e)

MOD_PARTS = {"meta", "wad", "raw"}

def mixed_mod_dir(path):
    """A folder holding loose META/WAD/RAW *and* other mods (someone extracted a mod straight into it)."""
    try:
        entries = list(os.scandir(path))
    except Exception:
        return False
    names = {e.name.lower() for e in entries}
    if not (names & MOD_PARTS):
        return False
    for e in entries:
        n = e.name.lower()
        if n in MOD_PARTS:
            continue
        if e.is_file() and n.endswith(MOD_EXTS + UNREADABLE_EXTS):
            return True
        if e.is_dir():
            if n.endswith(".wad.client"):
                return True
            try:
                if {x.lower() for x in os.listdir(e.path)} & MOD_PARTS:
                    return True
            except Exception:
                pass
    return False

def is_mod_dir(path, depth=1):
    if depth == 0:
        return False           # a top-level champion folder is never one mod
    if mixed_mod_dir(path):
        return False
    try:
        names = {n.lower() for n in os.listdir(path)}
    except Exception:
        return False
    if "meta" in names and os.path.exists(os.path.join(path, "META", "info.json")):
        return True
    if "wad" in names or "raw" in names:
        return True
    if path.lower().endswith(".wad.client"):
        return True
    return False

def dir_size(path):
    total = 0
    for dp, dn, fn in os.walk(path):
        for f in fn:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except Exception:
                pass
    return total

def discover():
    """Find every mod unit in the library."""
    units = []
    loose = []
    def walk(d, depth):
        try:
            entries = list(os.scandir(d))
        except Exception:
            return
        for e in entries:
            name_l = e.name.lower()
            if depth > 0 and name_l == "meta" and e.is_dir():
                continue
            if depth == 0 and name_l in SKIP_TOP:
                if name_l in ("assets", "data"):
                    loose.append(e.name)
                continue
            if e.is_dir(follow_symlinks=False):
                if is_mod_dir(e.path, depth):
                    units.append(e.path)
                elif depth < 6:
                    try:
                        has_parts = bool({x.lower() for x in os.listdir(e.path)} & MOD_PARTS)
                    except Exception:
                        has_parts = False
                    if has_parts:
                        loose.append(os.path.relpath(e.path, LIB) + " (has loose META/WAD/RAW folders from an extracted mod)")
                    walk(e.path, depth + 1)
            elif e.is_file():
                if name_l.endswith(MOD_EXTS + UNREADABLE_EXTS) and not name_l.startswith("__rzi_"):
                    units.append(e.path)
    walk(LIB, 0)
    return units, loose

def top_folder(path):
    rel = os.path.relpath(path, LIB)
    parts = rel.replace("\\", "/").split("/")
    return parts[0] if len(parts) > 1 else None

def folder_champ(path):
    """Champion implied by the top-level folder the mod sits in."""
    top = top_folder(path)
    if not top:
        return None
    return resolve_champ_name(top)

def rid(rel):
    return hashlib.md5(rel.lower().encode("utf-8")).hexdigest()[:12]

_RESCAN = {"pending": False}

# ---- "NEW" tags: when each mod first showed up (keyed by file fingerprint, so moving/organizing keeps it)
ADDED_PATH = os.path.join(DATA_DIR, "added.json")
NEW_DAYS = 3
_ADDED_LOCK = threading.Lock()

def load_added():
    try:
        return json.load(open(ADDED_PATH, encoding="utf-8"))
    except Exception:
        return {}

def save_added(d):
    try:
        tmp = ADDED_PATH + ".tmp"
        json.dump(d, open(tmp, "w", encoding="utf-8")); os.replace(tmp, ADDED_PATH)
    except Exception as e:
        log("added.json save failed", e)

def added_key(m):
    return m.get("fp") or ("rel:" + m["rel"].lower())

def stamp_added(mods):
    with _ADDED_LOCK:
        d = load_added()
        first_run = "_baseline" not in d
        now = time.time()
        changed = first_run
        recent = {}
        if first_run:   # seed from the import logs, so mods dropped in over the last few days are NEW right away
            try:
                for f in os.listdir(LOG_DIR):
                    mm = re.match(r"moves-(\d{8}-\d{6})-(import|zip-to-fantome)\.json$", f)
                    if not mm:
                        continue
                    ts = time.mktime(time.strptime(mm.group(1), "%Y%m%d-%H%M%S"))
                    if now - ts > NEW_DAYS * 86400:
                        continue
                    for mv in json.load(open(os.path.join(LOG_DIR, f), encoding="utf-8")):
                        if mm.group(2) == "import":
                            recent[os.path.normcase(os.path.relpath(mv["to"], LIB))] = ts
            except Exception as e:
                log("couldn't read import logs", e)
        for m in mods.values():
            k = added_key(m)
            if k not in d:
                r_ = os.path.normcase(m["rel"])
                d[k] = (recent.get(r_) or recent.get(re.sub(r"\.fantome$", ".zip", r_)) or 0) if first_run else now   # old library = not new
                changed = True
            m["added_at"] = d[k] or None
        if first_run:
            d["_baseline"] = now
        if changed:
            save_added(d)
        return d

def carry_added(old_key, new_key, as_new=False):
    with _ADDED_LOCK:
        d = load_added()
        d[new_key] = time.time() if as_new else d.get(old_key, 0)
        save_added(d)

def scan_library():
    with LOCK:
        if STATE["status"] == "scanning":
            _RESCAN["pending"] = True   # run again when the current scan finishes, so new files are never missed
            return
        STATE.update(status="scanning", progress=0, total=0, current="Finding mods...")
    t0 = time.time()
    try:
        cache = load_cache()
        units, loose = discover()
        STATE["total"] = len(units)
        new_cache = {}
        mods = {}
        for n, p in enumerate(units):
            rel = os.path.relpath(p, LIB)
            STATE["progress"] = n; STATE["current"] = rel
            try:
                st = os.stat(p)
                is_dir = os.path.isdir(p)
                key = f"{st.st_mtime:.0f}:{0 if is_dir else st.st_size}"
                c = cache.get(rel)
                if c and c.get("key") == key:
                    rec = c["rec"]
                else:
                    rec = analyze_path(p, folder_champ(p))
                    rec["size"] = dir_size(p) if is_dir else st.st_size
                    rec["fp"] = None if is_dir else fingerprint(p, st.st_size)
                    rec["mtime"] = st.st_mtime
                new_cache[rel] = {"key": key, "rec": rec}
            except Exception as e:
                rec = {"name": os.path.basename(p), "filename": os.path.basename(p), "kind": "file", "issues":
                       [{"level": "error", "msg": f"Scan failed: {e}"}], "champ": None, "type": "Other", "skins": [],
                       "applies_to": None, "size": 0, "mtime": 0, "children": [], "wads": []}
            rec = dict(rec)
            rec["rel"] = rel
            rec["id"] = rid(rel)
            rec["top"] = top_folder(p)
            mods[rec["id"]] = rec
        apply_overrides(mods)
        stamp_added(mods)
        post_process(mods)
        save_cache(new_cache)
        with LOCK:
            STATE["mods"] = mods
            STATE["loose"] = loose
            STATE["folders"] = champ_folders()
            STATE["last_scan"] = time.time()
            STATE["scan_seconds"] = round(time.time() - t0, 1)
        log(f"Scan done: {len(mods)} mods in {STATE['scan_seconds']}s")
        if AUTO_CONVERT:
            rels = AUTO_CONVERT[:]; AUTO_CONVERT.clear()
            ids = [rid(r) for r in rels if rid(r) in mods]
            if ids:
                with JOB_LOCK:
                    CONV_Q.extend(i for i in ids if i not in CONV_Q)
                CONV_EVENT.set()
        refresh_ltk_bg()
    except Exception:
        traceback.print_exc()
    finally:
        STATE["status"] = "idle"; STATE["current"] = ""
        if _RESCAN["pending"]:
            _RESCAN["pending"] = False
            threading.Thread(target=scan_library, daemon=True).start()

def post_process(mods):
    """Cross-mod issues: duplicates, misplaced files, extracted copies, slot conflicts."""
    now = time.time()
    by_fp, by_sig = {}, {}
    for m in mods.values():
        m["issues"] = [i for i in m.get("issues", []) if not i.get("cross")]
        if m.get("fp"):
            by_fp.setdefault(m["fp"], []).append(m)
        if m.get("content_sig"):
            by_sig.setdefault(m["content_sig"], []).append(m)
        m["dup_of"] = None
    def better(a, b):  # which copy to keep: correct folder, shorter name, newer
        sa = (a.get("top") and folder_champ(os.path.join(LIB, a["rel"])) == a.get("champ"), -len(a["filename"]), a.get("mtime", 0))
        sb = (b.get("top") and folder_champ(os.path.join(LIB, b["rel"])) == b.get("champ"), -len(b["filename"]), b.get("mtime", 0))
        return sa >= sb
    seen_dupe = set()
    for group in list(by_fp.values()) + list(by_sig.values()):
        if len(group) < 2:
            continue
        keep = group[0]
        for g in group[1:]:
            if better(g, keep):
                keep = g
        exact = len({g.get("fp") for g in group}) == 1 and group[0].get("fp")
        for g in group:
            if g is keep or g["id"] in seen_dupe:
                continue
            seen_dupe.add(g["id"])
            g["dup_of"] = keep["id"]
            g["issues"].append({"level": "warn", "cross": True, "code": "dup",
                                "msg": ("Exact duplicate of " if exact else "Same content as ") + keep["rel"]})
    # extracted folder next to its archive
    rels = {m["rel"].lower(): m for m in mods.values()}
    for m in mods.values():
        if m["kind"] == "folder":
            for ext in (".zip", ".fantome"):
                other = rels.get((m["rel"] + ext).lower())
                if other and not m.get("dup_of"):
                    m["dup_of"] = other["id"]
                    m["issues"].append({"level": "warn", "cross": True, "code": "dup",
                                        "msg": f"Extracted copy of {other['filename']} (you only need one)"})
    for m in mods.values():
        fc = folder_champ(os.path.join(LIB, m["rel"]))
        if m.get("champ") and fc and fc != m["champ"]:
            m["issues"].append({"level": "warn", "cross": True, "code": "misplaced",
                                "msg": f"Sitting in the '{m['top']}' folder but it's a {DD['champs'][m['champ']]['name']} mod"})
        if m.get("mtime") and now - m["mtime"] > 3 * 365 * 86400 and m.get("type") == "Skin":
            m["issues"].append({"level": "info", "cross": True, "code": "old",
                                "msg": f"File is from {time.strftime('%Y', time.localtime(m['mtime']))} - older mods often break after patches; test it or run it through cslol's fixer"})
    # same in-game slot used by several mods
    slots = {}
    for m in mods.values():
        if m.get("champ") and m.get("applies_to") is not None and m.get("type") == "Skin" and not m.get("dup_of"):
            slots.setdefault((m["champ"], m["applies_to"]), []).append(m)
    for (c, s), ms in slots.items():
        if len(ms) > 1:
            for m in ms:
                m["slot_conflicts"] = len(ms) - 1
    for m in mods.values():
        lv = [i["level"] for i in m["issues"]]
        m["severity"] = "error" if "error" in lv else "warn" if "warn" in lv else "info" if lv else "ok"

def champ_folders():
    out = {}
    try:
        for e in os.scandir(LIB):
            if e.is_dir():
                cid = resolve_champ_name(e.name)
                if cid:
                    out.setdefault(cid, []).append(e.name)
    except Exception:
        pass
    return out

# ---------------------------------------------------------------- moving files
INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def safe_name(s):
    s = INVALID.sub("", s).strip().rstrip(". ")
    return s or "Unnamed"

def champ_dir_for(cid):
    folders = STATE["folders"].get(cid) or champ_folders().get(cid) or []
    c = DD["champs"][cid]
    for f in folders:
        if norm(f) == norm(c["name"]):
            return os.path.join(LIB, f)
    if folders:
        # prefer the folder holding the most mods
        counts = {f: sum(1 for m in STATE["mods"].values() if m.get("top") == f) for f in folders}
        return os.path.join(LIB, max(folders, key=lambda f: counts[f]))
    return os.path.join(LIB, safe_name(c["name"]))

def skin_folder_name(rec):
    t = rec.get("type") or "Skin"
    cid = rec.get("champ")
    if t.startswith("Voice"):
        return "Voice & SFX"
    if t == "Loading screen":
        return "Loading Screens"
    if rec.get("applies_to") is not None and cid:
        lbl = skin_label(cid, rec["applies_to"]) or f"Skin {rec['applies_to']}"
        if rec["applies_to"] == 0:
            lbl = "Base (Default)"
        return safe_name(lbl)
    if rec.get("applies_source") == "multi":
        return "Multiple skins"
    return "Unknown skin"

def target_path(rec, src_path):
    cid = rec.get("champ")
    if cid:
        d = os.path.join(champ_dir_for(cid), skin_folder_name(rec))
    else:
        cat = {"Map": "Map", "Items": "Items", "Emote": "Emotes"}.get(rec.get("type"))
        d = os.path.join(LIB, cat) if cat else UNSORTED_DIR
    return os.path.join(d, os.path.basename(src_path.rstrip("\\/")))

def unique_path(p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if not os.path.exists(p):
        return p
    root, ext = p, ""
    for e in (".wad.client", ".fantome", ".zip", ".wad"):
        if p.lower().endswith(e):
            root, ext = p[: -len(e)], p[-len(e):]
            break
    i = 2
    while os.path.exists(f"{root} ({i}){ext}"):
        i += 1
    return f"{root} ({i}){ext}"

def move_logged(moves, label):
    done = []
    for src, dst in moves:
        try:
            if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            dst = unique_path(dst)
            shutil.move(src, dst)
            done.append({"from": src, "to": dst})
            remove_empty_parents(os.path.dirname(src))
        except Exception as e:
            log("move failed", src, e)
    if done:
        with open(os.path.join(LOG_DIR, f"moves-{time.strftime('%Y%m%d-%H%M%S')}-{label}.json"), "w", encoding="utf-8") as f:
            json.dump(done, f, indent=1)
    return done

PROTECTED_DIRS = {os.path.normcase(os.path.abspath(x)) for x in (LIB, APP_DIR, DROP_DIR, INCOMING_DIR, DUP_DIR, PACKS_DIR, UNSORTED_DIR)}

def remove_empty_parents(d):
    a = os.path.normcase(os.path.abspath(d))
    if a.startswith(os.path.normcase(APP_DIR)) or not a.startswith(os.path.normcase(LIB)):
        return
    try:
        while os.path.normcase(os.path.abspath(d)) not in PROTECTED_DIRS and os.path.isdir(d) and not os.listdir(d):
            parent = os.path.dirname(d)
            if os.path.abspath(parent) == os.path.abspath(LIB) and resolve_champ_name(os.path.basename(d)):
                break  # keep champion folders even if empty
            os.rmdir(d)
            d = parent
    except Exception:
        pass

def undo_last():
    logs = sorted(f for f in os.listdir(LOG_DIR) if f.startswith("moves-") and f.endswith(".json"))
    if not logs:
        return {"undone": 0, "msg": "Nothing to undo"}
    path = os.path.join(LOG_DIR, logs[-1])
    moves = json.load(open(path, encoding="utf-8"))
    n = 0
    for m in reversed(moves):
        if os.path.exists(m["to"]) and not os.path.exists(m["from"]):
            os.makedirs(os.path.dirname(m["from"]), exist_ok=True)
            shutil.move(m["to"], m["from"]); n += 1
            remove_empty_parents(os.path.dirname(m["to"]))
    os.rename(path, path + ".undone")
    return {"undone": n, "msg": f"Moved {n} item(s) back"}

def push_event(ev):
    ev["t"] = time.time()
    with LOCK:
        STATE["events"].append(ev)
        STATE["events"] = STATE["events"][-200:]

# ---------------------------------------------------------------- importing
def import_path(src, origin="drop"):
    """Analyze a newly added mod and move it to <Champion>/<Skin>/.
    Packs are split; wrapper zips holding one .fantome are unwrapped."""
    results = []
    base = os.path.basename(src)
    rec = analyze_path(src)
    if rec["children"]:
        # unpack each nested mod
        with zipfile.ZipFile(src) as zf:
            for ch in rec["children"]:
                try:
                    data = zf.read(ch["member"])
                    tmp = unique_path(os.path.join(INCOMING_DIR, os.path.basename(ch["member"])))
                    with open(tmp, "wb") as f:
                        f.write(data)
                    results += import_path(tmp, origin)
                except Exception as e:
                    results.append({"file": ch["name"], "ok": False, "msg": f"Couldn't extract: {e}"})
        if len(rec["children"]) > 1:
            move_logged([(src, os.path.join(PACKS_DIR, base))], "pack")
        else:
            move_logged([(src, os.path.join(PACKS_DIR, base))], "wrapper")
        return results
    # exact duplicate already in library?
    size = os.path.getsize(src) if os.path.isfile(src) else 0
    fp = fingerprint(src, size) if size else None
    for m in STATE["mods"].values():
        if fp and m.get("fp") == fp and os.path.exists(os.path.join(LIB, m["rel"])):
            dst = move_logged([(src, os.path.join(DUP_DIR, base))], "dupe")
            r = {"file": base, "ok": True, "duplicate": True, "msg": f"Already have this one: {m['rel']} (moved to _Duplicates)",
                 "champ": m.get("champ"), "id": m["id"], "origin": origin}
            push_event(r); results.append(r)
            return results
    dst = target_path(rec, src)
    if os.path.isfile(src):
        stem = re.sub(r"( \(\d+\))?(\.(fantome|zip|wad\.client|wad))$", "", os.path.basename(dst), flags=re.I)
        ddir = os.path.dirname(dst)
        if os.path.isdir(ddir):
            for f in os.listdir(ddir):
                fp_ = os.path.join(ddir, f)
                if f.lower().startswith(stem.lower()) and os.path.isfile(fp_) and os.path.getsize(fp_) == size \
                        and fingerprint(fp_, size) == fp:
                    move_logged([(src, os.path.join(DUP_DIR, base))], "dupe")
                    rel_ = os.path.relpath(fp_, LIB)
                    r = {"file": base, "ok": True, "duplicate": True, "msg": f"Already have this one: {rel_} (moved to _Duplicates)",
                         "champ": rec.get("champ"), "id": rid(rel_), "origin": origin}
                    push_event(r); results.append(r)
                    return results
    done = move_logged([(src, dst)], "import")
    final = done[0]["to"] if done else dst
    cname = DD["champs"][rec["champ"]]["name"] if rec.get("champ") else None
    msg = f"{rec['name']} -> " + os.path.relpath(final, LIB)
    r = {"file": base, "ok": bool(done), "champ": rec.get("champ"), "champ_name": cname, "skin": rec.get("applies_label"),
         "type": rec.get("type"), "issues": rec["issues"], "dest": os.path.relpath(final, LIB), "msg": msg,
         "id": rid(os.path.relpath(final, LIB)), "origin": origin}
    push_event(r); results.append(r)
    log("Imported:", msg)
    if done and final.lower().endswith(".zip") and rec.get("champ") and CFG.get("auto_fantome", True) and ltmao_dir():
        AUTO_CONVERT.append(os.path.relpath(final, LIB))
    return results

def watch_drop_folder():
    """Anything placed in '_Drop Here' gets auto-sorted."""
    seen = {}
    while True:
        try:
            for e in os.scandir(DROP_DIR):
                name_l = e.name.lower()
                if e.is_file() and not name_l.endswith(MOD_EXTS + UNREADABLE_EXTS):
                    continue
                st = e.stat()
                sig = (st.st_size, st.st_mtime)
                if seen.get(e.path) != sig:  # wait until size stops changing (download finished)
                    seen[e.path] = sig
                    continue
                if e.is_dir() and not is_mod_dir(e.path):
                    continue
                if name_l.endswith(UNREADABLE_EXTS):
                    continue
                try:
                    tmp = unique_path(os.path.join(INCOMING_DIR, e.name))
                    shutil.move(e.path, tmp)
                    import_path(tmp, "folder")
                except Exception as ex:
                    log("drop import failed", e.name, ex)
                seen.pop(e.path, None)
                threading.Thread(target=scan_library, daemon=True).start()
        except Exception:
            pass
        time.sleep(3)

# ---------------------------------------------------------------- organize / dedupe / assign
def organize_plan():
    plan = []
    for m in STATE["mods"].values():
        if m.get("dup_of") or m["children"] or not m.get("champ"):
            continue
        src = os.path.join(LIB, m["rel"])
        dst = target_path(m, src)
        if m.get("wrapped") or os.path.normcase(os.path.dirname(src)) != os.path.normcase(os.path.dirname(dst)):
            to = os.path.relpath(dst, LIB)
            if m.get("wrapped"):
                to = os.path.join(os.path.dirname(to), os.path.basename(m["wrapped"])) + "   (unzipped)"
            plan.append({"id": m["id"], "from": m["rel"], "to": to, "name": m["name"]})
    plan.sort(key=lambda x: x["to"].lower())
    return plan

def organize_apply(ids):
    plan = {p["id"]: p for p in organize_plan()}
    moves = []
    for i in ids:
        if i not in plan:
            continue
        m = STATE["mods"].get(i, {})
        src = os.path.join(LIB, plan[i]["from"]); dst = os.path.join(LIB, plan[i]["to"])
        if m.get("wrapped") and m["kind"] == "file":
            try:  # unwrap: extract the inner .fantome next to where the zip would go, park the zip
                inner = unique_path(os.path.join(os.path.dirname(dst), os.path.basename(m["wrapped"])))
                with zipfile.ZipFile(src) as zf, open(inner, "wb") as f:
                    f.write(zf.read(m["wrapped"]))
                moves.append((src, os.path.join(PACKS_DIR, os.path.basename(src))))
                continue
            except Exception as e:
                log("unwrap failed", src, e)
        moves.append((src, dst))
    done = move_logged(moves, "organize")
    return {"moved": len(done)}

def dedupe_apply(ids=None):
    moves = []
    for m in STATE["mods"].values():
        if m.get("dup_of") and (ids is None or m["id"] in ids):
            src = os.path.join(LIB, m["rel"])
            moves.append((src, os.path.join(DUP_DIR, m["rel"])))
    done = move_logged(moves, "dedupe")
    return {"moved": len(done)}

OVR_PATH = os.path.join(DATA_DIR, "overrides.json")

def load_overrides():
    try:
        return json.load(open(OVR_PATH, encoding="utf-8"))
    except Exception:
        return {}

def save_override(key, champ, skin):
    o = load_overrides(); o[key] = {"champ": champ, "skin": skin}
    json.dump(o, open(OVR_PATH, "w", encoding="utf-8"), indent=1)

def override_key(m):
    return m.get("fp") or m.get("content_sig") or m["filename"].lower()

def apply_overrides(mods):
    o = load_overrides()
    if not o:
        return
    for m in mods.values():
        v = o.get(override_key(m))
        if not v:
            continue
        m["champ"] = v["champ"]; m["applies_to"] = v["skin"]; m["applies_source"] = "manual"
        if m.get("type") in ("Other", None):
            m["type"] = "Skin"
        m["applies_label"] = skin_label(v["champ"], v["skin"]) if v["skin"] is not None else None
        m["issues"] = [i for i in m["issues"] if not any(w in i["msg"] for w in
                       ("Couldn't tell", "Looks like", "Changes ", "champion"))]
        m["issues"].append({"level": "info", "msg": "Champion/skin set by you"})

def assign(mid, champ, skin):
    m = STATE["mods"].get(mid)
    if not m or champ not in DD["champs"]:
        return {"ok": False, "msg": "Unknown mod or champion"}
    save_override(override_key(m), champ, skin)
    rec = dict(m); rec["champ"] = champ
    rec["type"] = "Skin" if rec.get("type") in ("Other", None) else rec["type"]
    rec["applies_to"] = skin if skin is not None else None
    rec["applies_source"] = "manual"
    src = os.path.join(LIB, m["rel"])
    done = move_logged([(src, target_path(rec, src))], "assign")
    return {"ok": bool(done), "to": os.path.relpath(done[0]["to"], LIB) if done else None}

def unpack(mid):
    m = STATE["mods"].get(mid)
    if not m or not m["children"]:
        return {"ok": False}
    src = os.path.join(LIB, m["rel"])
    tmp = unique_path(os.path.join(INCOMING_DIR, os.path.basename(src)))
    shutil.copy2(src, tmp)
    res = import_path(tmp, "unpack")
    move_logged([(src, os.path.join(PACKS_DIR, os.path.basename(src)))], "unpacked")
    return {"ok": True, "results": res}

def send_to_recycle_bin(path):
    """Delete to the Windows Recycle Bin (restorable) - never a permanent delete."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return False
    if os.name != "nt":
        dst = os.path.join(LIB, "_Deleted", os.path.basename(path))
        os.makedirs(os.path.dirname(dst), exist_ok=True); shutil.move(path, unique_path(dst)); return True
    import ctypes
    from ctypes import wintypes
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, path + "\0", None,
                         FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI, False, None, None)
    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return rc == 0 and not op.fAnyOperationsAborted and not os.path.exists(path)

def delete_mods(ids, remove_ltk=False):
    done, failed, ltk_ids = [], [], []
    for mid in ids:
        m = STATE["mods"].get(mid)
        if not m:
            failed.append({"id": mid, "msg": "not found"}); continue
        if remove_ltk:
            lid = ltk_entry_for(mid)
            if lid:
                ltk_ids.append(lid)
        path = os.path.join(LIB, m["rel"])
        try:
            ok = send_to_recycle_bin(path)
        except Exception as e:
            ok = False; log("delete failed", path, e)
        (done if ok else failed).append({"id": mid, "name": m["name"], "rel": m["rel"]})
        if ok:
            remove_empty_parents(os.path.dirname(path))
            with LOCK:
                STATE["mods"].pop(mid, None)
    if done:
        with open(os.path.join(LOG_DIR, f"deleted-{time.strftime('%Y%m%d-%H%M%S')}.json"), "w", encoding="utf-8") as f:
            json.dump(done, f, indent=1)
    ltk_res = None
    if ltk_ids:
        try:
            ltk_res = ltk_apply([{"op": "remove", "id": i} for i in ltk_ids])
        except Exception as e:
            ltk_res = {"error": str(e)}
    threading.Thread(target=scan_library, daemon=True).start()
    return {"deleted": done, "failed": failed, "ltk": ltk_res}

def open_in_explorer(path):
    try:
        if sys.platform.startswith("win"):
            if os.path.isdir(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])
    except Exception as e:
        log("explorer failed", e)

def mod_image(mid):
    m = STATE["mods"].get(mid)
    if not m:
        return None
    path = os.path.join(LIB, m["rel"])
    ref = m.get("_image_ref")
    try:
        if m["kind"] == "folder":
            if ref and os.path.exists(ref):
                return open(ref, "rb").read()
            for cand in ("META/image.png", "META/image.jpg"):
                p = os.path.join(path, cand)
                if os.path.exists(p):
                    return open(p, "rb").read()
        elif path.lower().endswith((".zip", ".fantome")):
            with zipfile.ZipFile(path) as zf:
                if ref:
                    return zf.read(ref)
    except Exception:
        return None
    return None

# ---------------------------------------------------------------- 3D viewer
TEX_SESSIONS = {}
TEX_ORDER = []

_GAME_DIR = {"v": None, "t": 0}

def game_dir():
    if time.time() - _GAME_DIR["t"] > 60:
        _GAME_DIR["v"] = lol3d.find_game_dir(CFG.get("game_dir")); _GAME_DIR["t"] = time.time()
    return _GAME_DIR["v"]

def get_mod(mid):
    """Library mod or LTK Manager mod ('ltk:<id>') -> (record, absolute path)."""
    if mid and mid.startswith("ltk:"):
        m = LTK["mods"].get(mid)
        return (m, m["path"]) if m else (None, None)
    m = STATE["mods"].get(mid)
    return (m, os.path.join(LIB, m["rel"])) if m else (None, None)

def model_payload(champ, skin, mid=None):
    mod_path = None
    if mid:
        m, mod_path = get_mod(mid)
        if not m:
            raise RuntimeError("Unknown mod")
        champ = champ or m.get("champ")
        if skin is None:
            skin = m.get("applies_to")
        if skin is None:
            skin = (m.get("skins") or [0])[0]
    if not champ:
        raise RuntimeError("No champion for this mod - use Reassign first")
    model, textures = lol3d.build_model(champ, int(skin or 0), game_dir(), mod_path)
    sid = uuid.uuid4().hex[:10]
    TEX_SESSIONS[sid] = textures; TEX_ORDER.append(sid)
    while len(TEX_ORDER) > 8:
        TEX_SESSIONS.pop(TEX_ORDER.pop(0), None)
    model["session"] = sid
    model["champ"] = champ; model["skin"] = int(skin or 0)
    model["skin_label"] = skin_label(champ, int(skin or 0))
    return model

# ---------------------------------------------------------------- auto-fix (LtMAO)
JOBS = []          # list of job dicts (newest last)
JOB_Q = []
JOB_LOCK = threading.Lock()
JOB_EVENT = threading.Event()

def ltmao_dir():
    return fixer.find_ltmao(CFG.get("ltmao_dir"))

def ltk_entry_for(mid):
    """LTK id of the installed copy of a library mod (or of an ltk: mod itself)."""
    if mid.startswith("ltk:"):
        return mid[4:]
    for m in LTK["mods"].values():
        if m.get("lib_id") == mid:
            return m["ltk_id"]
    return None

def queue_fix(mid, slot=None, swap_ltk=False):
    m, path = get_mod(mid)
    if not m:
        return None
    job = {"id": uuid.uuid4().hex[:8], "mod_id": mid, "name": m.get("name"), "champ": m.get("champ"),
           "slot": slot if slot is not None else m.get("applies_to"), "status": "queued", "steps": [],
           "report": None, "error": None, "t": time.time(),
           "swap_ltk": ltk_entry_for(mid) if swap_ltk else None}
    if job["slot"] is None:
        job["slot"] = (m.get("skins") or [0])[0]
    with JOB_LOCK:
        JOBS.append(job); JOB_Q.append(job)
        del JOBS[:-300]
    JOB_EVENT.set()
    return job

def fix_worker():
    while True:
        JOB_EVENT.wait()
        with JOB_LOCK:
            job = JOB_Q.pop(0) if JOB_Q else None
            if not JOB_Q:
                JOB_EVENT.clear()
        if not job:
            continue
        job["status"] = "running"
        try:
            run_fix(job)
            job["status"] = "done"
        except Exception as e:
            traceback.print_exc()
            job["status"] = "error"; job["error"] = str(e)
        push_event({"file": job["name"], "ok": job["status"] == "done", "fix": True, "champ": job["champ"],
                    "msg": ("Fixed -> " + (job.get("out_rel") or "")) if job["status"] == "done" else ("Fix failed: " + str(job["error"])),
                    "id": job.get("out_id"), "origin": "fix"})
        if job["status"] == "done" and job.get("swap_ltk") and job.get("out_path"):
            PENDING_SWAPS.append((job["swap_ltk"], job["out_path"], job))
        if not JOB_Q:
            if PENDING_SWAPS:
                swaps = PENDING_SWAPS[:]; PENDING_SWAPS.clear()
                try:
                    ltk_apply([{"op": "remove", "id": old} for old, new, j in swaps], restart=False)
                    ltk_add([new for old, new, j in swaps])
                    for old, new, j in swaps:
                        j["ltk_swapped"] = True
                    push_event({"file": f"{len(swaps)} mod(s)", "ok": True, "fix": True, "msg": "Swapped the fixed versions into LTK Manager", "origin": "fix"})
                except Exception as e:
                    for old, new, j in swaps:
                        j["ltk_swap_error"] = str(e)
                    push_event({"file": "LTK swap", "ok": False, "fix": True, "msg": str(e), "origin": "fix"})
            threading.Thread(target=scan_library, daemon=True).start()

def run_fix(job):
    lt = ltmao_dir()
    if not lt:
        raise RuntimeError("LtMAO-hai not found - set its folder in Settings")
    gd = game_dir()
    if not gd:
        raise RuntimeError("League game folder not found")
    m, path = get_mod(job["mod_id"])
    if not m or not os.path.exists(path):
        raise RuntimeError("Mod file not found (rescan?)")
    champ = job["champ"] or m.get("champ")
    if not champ:
        raise RuntimeError("No champion detected - use Reassign first")
    is_ltk = job["mod_id"].startswith("ltk:")
    stem = re.sub(r"\.(fantome|zip|wad\.client|wad)$", "", os.path.basename(path.rstrip("\\/")), flags=re.I)
    stem = re.sub(r"\s*\(fixed\)$", "", stem)
    if is_ltk:
        out_dir = os.path.join(champ_dir_for(champ), skin_folder_name({"champ": champ, "applies_to": job["slot"], "type": "Skin"}))
        stem = safe_name(m.get("name") or stem)
    else:
        out_dir = os.path.dirname(path)
    os.makedirs(out_dir, exist_ok=True)
    out = unique_path(os.path.join(out_dir, stem + " (fixed).fantome"))
    os.makedirs(WORK_DIR, exist_ok=True)
    rep = fixer.fix_mod(path, champ, int(job["slot"]), gd, lt, WORK_DIR, out, log=lambda s: job["steps"].append(s))
    # verify the result the same way the scanner does
    rec = analyze_path(out)
    rep["verify"] = {"applies_to": rec.get("applies_to"), "applies_label": rec.get("applies_label"),
                     "outdated": rec.get("outdated"), "issues": [i["msg"] for i in rec["issues"] if i["level"] != "info"]}
    fp = fingerprint(out, os.path.getsize(out))
    if fp:
        save_override(fp, champ, int(job["slot"]))
    if not is_ltk:
        rel = m["rel"]
        move_logged([(path, os.path.join(ORIGINALS_DIR, rel))], "fixed-original")
        job["original"] = os.path.relpath(os.path.join(ORIGINALS_DIR, rel), LIB)
    job["report"] = rep
    job["out_rel"] = os.path.relpath(out, LIB)
    job["out_id"] = rid(job["out_rel"])
    job["out_path"] = out

def unpack_ltmao(mid):
    lt = ltmao_dir()
    if not lt:
        raise RuntimeError("LtMAO-hai not found")
    m, path = get_mod(mid)
    out = unique_path(os.path.join(UNPACKED_DIR, safe_name(m.get("name") or "mod")))
    fixer.unpack_mod(path, lt, out)
    open_in_explorer(out)
    return out

# ---------------------------------------------------------------- LTK Manager
LTK = {"mods": {}, "found": False, "dir": None, "profile": None, "t": 0, "error": None}
LTK_CACHE_PATH = os.path.join(DATA_DIR, "ltk_cache.json")

def ltk_dir():
    cands = [CFG.get("ltk_dir"), os.path.join(os.environ.get("APPDATA", ""), "dev.leaguetoolkit.manager")]
    for c in cands:
        if c and os.path.exists(os.path.join(c, "library.json")):
            return c
    return None

def load_ltk(force=False):
    d = ltk_dir()
    LTK["dir"] = d
    if not d:
        LTK.update(found=False, mods={}); return LTK
    try:
        lib = json.load(open(os.path.join(d, "library.json"), encoding="utf-8"))
        try:
            health = json.load(open(os.path.join(d, "mod-health-verdicts.json"), encoding="utf-8")).get("verdicts", {})
        except Exception:
            health = {}
        try:
            reports = json.load(open(os.path.join(d, "wad-reports.json"), encoding="utf-8")).get("reports", {})
        except Exception:
            reports = {}
        prof = next((p for p in lib.get("profiles", []) if p.get("id") == lib.get("activeProfileId")), (lib.get("profiles") or [{}])[0])
        enabled = set(prof.get("enabledMods") or [])
        order = {mid: i for i, mid in enumerate(prof.get("modOrder") or [])}
        try:
            cache = json.load(open(LTK_CACHE_PATH, encoding="utf-8"))
        except Exception:
            cache = {}
        if cache.get("v") != SCAN_VERSION:
            cache = {"v": SCAN_VERSION, "items": {}}
        mods = {}
        mdir = os.path.join(d, "mods")
        for e in lib.get("mods", []):
            slug = e.get("slug") or ""
            cfgp = os.path.join(mdir, slug, "mod.config.json")
            try:
                mc = json.load(open(cfgp, encoding="utf-8"))
            except Exception:
                mc = {}
            arch = None
            for ext in (".fantome", ".modpkg", ".zip"):
                if os.path.exists(os.path.join(mdir, slug + ext)):
                    arch = os.path.join(mdir, slug + ext); break
            if not arch and os.path.isdir(os.path.join(mdir, slug)) and is_mod_dir(os.path.join(mdir, slug)):
                arch = os.path.join(mdir, slug)
            rec = {}
            if arch:
                st = os.stat(arch)
                key = f"{st.st_size}:{st.st_mtime:.0f}"
                c = cache["items"].get(arch)
                lid0 = load_links().get(e.get("sourceSha256")) if e.get("sourceSha256") else None
                if c and c.get("key") == key and not force:
                    rec = c["rec"]
                elif lid0 and lid0 in STATE["mods"] and os.path.isfile(arch):
                    # same mod we already analysed in the library - reuse it instead of re-reading the archive
                    rec = {k: v for k, v in STATE["mods"][lid0].items() if not k.startswith("_")}
                    rec["fp"] = fingerprint(arch, st.st_size); rec["size"] = st.st_size
                    cache["items"][arch] = {"key": key, "rec": rec}
                else:
                    try:
                        rec = analyze_path(arch)
                        rec["fp"] = fingerprint(arch, st.st_size) if os.path.isfile(arch) else None
                        rec["size"] = st.st_size
                        rec.pop("_image_ref", None)
                    except Exception as ex:
                        rec = {"issues": [{"level": "error", "msg": f"Couldn't read: {ex}"}]}
                    cache["items"][arch] = {"key": key, "rec": rec}
            hv = health.get(e["id"], {})
            der = (reports.get(e["id"], {}) or {}).get("derived", {}) or {}
            champ = rec.get("champ")
            if not champ and der.get("primaryChampion"):
                champ = resolve_champ_name(der["primaryChampion"])
            mid = "ltk:" + e["id"]
            mods[mid] = {
                "id": mid, "ltk_id": e["id"], "slug": slug, "path": arch,
                "name": mc.get("display_name") or rec.get("name") or slug,
                "author": ", ".join(mc.get("authors") or []) or rec.get("author", ""),
                "version": mc.get("version") or rec.get("version", ""), "description": mc.get("description") or "",
                "enabled": e["id"] in enabled, "order": order.get(e["id"]), "installed": e.get("installedAt"),
                "format": e.get("format"), "champ": champ, "applies_to": rec.get("applies_to"),
                "applies_label": rec.get("applies_label"), "skins": rec.get("skins") or [], "type": rec.get("type") or "Skin",
                "outdated": rec.get("outdated"), "issues": rec.get("issues", []), "fp": rec.get("fp"),
                "health": hv.get("health"), "health_counts": hv.get("counts"), "wads": (reports.get(e["id"]) or {}).get("affectedWads", []),
                "lib_id": None, "filename": os.path.basename(arch) if arch else slug, "sha": e.get("sourceSha256"),
            }
        try:
            json.dump(cache, open(LTK_CACHE_PATH, "w", encoding="utf-8"))
        except Exception:
            pass
        # link to library copies + slot conflicts among enabled mods
        by_fp = {m.get("fp"): m["id"] for m in STATE["mods"].values() if m.get("fp")}
        by_name = {norm(m["name"]): m["id"] for m in STATE["mods"].values()}
        links = load_links()
        for m in mods.values():
            lid = links.get(m.get("sha"))
            if lid and lid not in STATE["mods"]:
                lid = None
            m["lib_id"] = lid or by_fp.get(m["fp"]) or by_name.get(norm(m["name"]))
        slots = {}
        for m in mods.values():
            if m["enabled"] and m["champ"] and m["applies_to"] is not None and m["type"] == "Skin":
                slots.setdefault((m["champ"], m["applies_to"]), []).append(m)
        for ms in slots.values():
            if len(ms) > 1:
                ms.sort(key=lambda x: (x["order"] is None, x["order"] or 0))
                for m in ms:
                    m["conflict_with"] = [x["name"] for x in ms if x is not m]
        LTK.update(found=True, mods=mods, profile=prof.get("name"), t=time.time(), error=None)
        push_event({"ltk_changed": True, "file": "", "ok": True, "msg": "", "origin": "ltk"})
    except Exception as ex:
        traceback.print_exc()
        LTK.update(error=str(ex))
    return LTK

# ---- LTK Manager control (add via its own file handler; toggle/remove by editing its library while it's closed)
PENDING_SWAPS = []
LTK_LOCK = threading.Lock()

def ltk_exe():
    for c in (CFG.get("ltk_exe"), r"C:\Program Files\LTK Manager\ltk-manager.exe",
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "LTK Manager", "ltk-manager.exe")):
        if c and os.path.exists(c):
            return c
    return None

def _tasklist(image):
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}", "/FO", "CSV", "/NH"], capture_output=True,
                             text=True, timeout=20, creationflags=fixer.NO_WINDOW).stdout
        return [l for l in out.splitlines() if l.lower().startswith(f'"{image.lower()}"')]
    except Exception:
        return []

def league_in_game():
    return bool(_tasklist("League of Legends.exe"))

_RUN_CACHE = {"t": 0, "v": False}

def ltk_running(fresh=False):
    if fresh or time.time() - _RUN_CACHE["t"] > 3:
        _RUN_CACHE["v"] = bool(_tasklist("ltk-manager.exe")); _RUN_CACHE["t"] = time.time()
    return _RUN_CACHE["v"]

def stop_ltk():
    if not ltk_running(fresh=True):
        return False
    subprocess.run(["taskkill", "/IM", "ltk-manager.exe", "/T"], capture_output=True, timeout=20, creationflags=fixer.NO_WINDOW)
    for _ in range(20):
        time.sleep(0.5)
        if not ltk_running(fresh=True):
            break
    if ltk_running(fresh=True):
        subprocess.run(["taskkill", "/IM", "ltk-manager.exe", "/T", "/F"], capture_output=True, timeout=20, creationflags=fixer.NO_WINDOW)
        time.sleep(1.5)
    subprocess.run(["taskkill", "/IM", "ltk_patcher_host.exe", "/F"], capture_output=True, timeout=20, creationflags=fixer.NO_WINDOW)
    time.sleep(0.5)
    return True

_LTK_STARTED = {"t": 0}

def start_ltk(args=()):
    if not args:
        _LTK_STARTED["t"] = time.time()
    exe = ltk_exe()
    if not exe:
        raise RuntimeError("ltk-manager.exe not found")
    subprocess.Popen([exe] + list(args), cwd=os.path.dirname(exe), close_fds=True,
                     creationflags=0x00000008 | 0x00000200 if os.name == "nt" else 0)

LINKS_PATH = os.path.join(DATA_DIR, "ltk_links.json")

def load_links():
    try:
        return json.load(open(LINKS_PATH, encoding="utf-8"))
    except Exception:
        return {}

def save_link(sha, lib_id):
    d = load_links(); d[sha] = lib_id
    json.dump(d, open(LINKS_PATH, "w", encoding="utf-8"), indent=1)

def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def ltk_library_shas():
    d = ltk_dir()
    try:
        lib = json.load(open(os.path.join(d, "library.json"), encoding="utf-8"))
        return {m.get("sourceSha256"): m.get("id") for m in lib.get("mods", []) if m.get("sourceSha256")}
    except Exception:
        return {}

CONV_LOCK = threading.Lock()

def champ_wad_name(champ):
    w = lol3d.game_wad(game_dir(), champ) if champ else None
    return os.path.basename(w.name)[: -len(".client")] if w else f"{champ or 'Mod'}.wad"

def convert_to_fantome(mid):
    """Library .zip -> .fantome next to it (original kept in _Originals). Returns (new_path, new_id, note)."""
    m = STATE["mods"].get(mid)
    if not m:
        raise RuntimeError("mod not found")
    path = os.path.join(LIB, m["rel"])
    if not path.lower().endswith(".zip"):
        return path, mid, "already fantome"
    if not m.get("champ"):
        raise RuntimeError("no champion detected - can't tell which WAD to pack into")
    with CONV_LOCK:
        out = unique_path(re.sub(r"\.zip$", ".fantome", path, flags=re.I))
        os.makedirs(WORK_DIR, exist_ok=True)
        note = fixer.zip_to_fantome(path, champ_wad_name(m["champ"]), ltmao_dir(), WORK_DIR, out)
        fp = fingerprint(out, os.path.getsize(out))
        if fp:
            carry_added(added_key(m), fp)
        if fp and m.get("applies_to") is not None:
            save_override(fp, m["champ"], m["applies_to"])   # keep it under the same skin
        move_logged([(path, os.path.join(ORIGINALS_DIR, m["rel"]))], "zip-to-fantome")
        new_rel = os.path.relpath(out, LIB)
        new_id = rid(new_rel)
        # make the new file visible immediately (full rescan follows)
        rec = dict(m); rec.update(rel=new_rel, id=new_id, filename=os.path.basename(out), fp=fp, size=os.path.getsize(out))
        with LOCK:
            STATE["mods"].pop(mid, None); STATE["mods"][new_id] = rec
        return out, new_id, note

AUTO_CONVERT = []   # rel paths imported as .zip -> converted once the rescan has picked them up
CONV_Q = []
CONV_EVENT = threading.Event()

def conv_worker():
    while True:
        CONV_EVENT.wait()
        with JOB_LOCK:
            batch = CONV_Q[:]; CONV_Q.clear(); CONV_EVENT.clear()
        for mid in batch:
            m = STATE["mods"].get(mid)
            name = m["filename"] if m else mid
            push_event({"origin": "ltk-stage", "lib_id": mid, "stage": "converting", "file": name, "ok": True, "msg": ""})
            try:
                out, new_id, note = convert_to_fantome(mid)
                push_event({"origin": "convert", "lib_id": mid, "new_id": new_id, "file": name, "ok": True,
                            "msg": f"Converted to .fantome ({note})"})
            except Exception as e:
                push_event({"origin": "convert", "lib_id": mid, "file": name, "ok": False, "msg": f"Couldn't convert: {e}"})
        if batch:
            threading.Thread(target=scan_library, daemon=True).start()

def ltk_ready():
    """True once the newest LTK Manager start has finished booting (read from its own log)."""
    d = ltk_dir()
    try:
        logs = sorted((os.path.join(d, "logs", f) for f in os.listdir(os.path.join(d, "logs"))), key=os.path.getmtime)
        with open(logs[-1], "rb") as f:
            f.seek(0, 2); f.seek(max(0, f.tell() - 200000))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return True
    last_start = max((i for i, l in enumerate(lines) if "Starting LTK Manager" in l), default=-1)
    tail = lines[last_start + 1:]
    return any(("String key index built" in l) or ("Swept mod health" in l) or ("patcher" in l) for l in tail)

def wait_ltk_ready(timeout=25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if ltk_running(fresh=True) and ltk_ready():
            time.sleep(0.6)
            return True
        time.sleep(0.4)
    return False

ADD_Q = []
ADD_EVENT = threading.Event()

def queue_ltk_add(paths, lib_ids):
    with JOB_LOCK:
        for p, l in zip(paths, lib_ids):
            if not any(x[0] == p for x in ADD_Q):
                ADD_Q.append((p, l))
    ADD_EVENT.set()

def ltk_add_worker():
    while True:
        ADD_EVENT.wait()
        time.sleep(0.7)          # collect quick successive clicks into one batch
        with JOB_LOCK:
            batch = ADD_Q[:]; ADD_Q.clear(); ADD_EVENT.clear()
        if batch:
            conv = []
            for p, l in batch:
                if p.lower().endswith(".zip") and l and l in STATE["mods"] and STATE["mods"][l].get("champ"):
                    push_event({"origin": "ltk-stage", "lib_id": l, "stage": "converting", "file": os.path.basename(p), "ok": True, "msg": ""})
                    try:
                        out, new_id, note = convert_to_fantome(l)
                        push_event({"origin": "convert", "lib_id": l, "new_id": new_id, "file": os.path.basename(p), "ok": True,
                                    "msg": f"Converted to .fantome ({note})"})
                        p, l = out, new_id
                    except Exception as e:
                        log("convert before LTK add failed:", e)   # fall back to letting LTK convert it
                push_event({"origin": "ltk-stage", "lib_id": l, "stage": "adding", "file": os.path.basename(p), "ok": True, "msg": ""})
                conv.append((p, l))
            batch = conv
            threading.Thread(target=scan_library, daemon=True).start()
            try:
                ltk_add([p for p, l in batch], [l for p, l in batch])
            except Exception as e:
                traceback.print_exc()
                for p, l in batch:
                    push_event({"file": os.path.basename(p), "ok": False, "fix": True, "origin": "ltk-add", "lib_id": l, "msg": str(e)})

def ltk_add(paths, lib_ids=None):
    """Install mods through LTK Manager itself (same as double-clicking them) - all in one launch - and confirm
    each one actually landed in LTK's library, retrying the ones LTK dropped."""
    exe = ltk_exe()
    if not exe:
        raise RuntimeError("LTK Manager isn't installed where expected")
    lib_ids = lib_ids or [None] * len(paths)
    with LTK_LOCK:
        if not ltk_running(fresh=True):
            start_ltk()
            wait_ltk_ready()
        elif time.time() - _LTK_STARTED["t"] < 30 and not ltk_ready():
            wait_ltk_ready()
        todo = {}
        have = ltk_library_shas()
        for p, lid in zip(paths, lib_ids):
            if not os.path.exists(p):
                push_event({"file": os.path.basename(p), "ok": False, "fix": True, "origin": "ltk-add", "lib_id": lid, "msg": "file missing"})
                continue
            sha = sha256_file(p)
            if lid:
                save_link(sha, lid)
            if sha in have:
                push_event({"file": os.path.basename(p), "ok": True, "fix": True, "origin": "ltk-add", "lib_id": lid, "msg": "Already in LTK Manager"})
                continue
            todo[sha] = (p, lid)
        for attempt in range(3):
            if not todo:
                break
            if not ltk_running(fresh=True):
                start_ltk(); wait_ltk_ready()
            start_ltk([p for p, l in todo.values()])
            t0 = time.time()
            while todo and time.time() - t0 < 15:
                time.sleep(0.3)
                have = ltk_library_shas()
                for sha in [x for x in todo if x in have]:
                    p, lid = todo.pop(sha)
                    push_event({"file": os.path.basename(p), "ok": True, "fix": True, "origin": "ltk-add", "lib_id": lid,
                                "msg": "Added to LTK Manager (enabled)"})
            if todo:
                log("LTK didn't pick up", len(todo), "file(s) - retrying")
                wait_ltk_ready(8)
        for sha, (p, lid) in todo.items():
            push_event({"file": os.path.basename(p), "ok": False, "fix": True, "origin": "ltk-add", "lib_id": lid,
                        "msg": "LTK Manager didn't accept it - try dragging the file into LTK Manager"})
    refresh_ltk_bg()

def ltk_apply(changes, restart=True):
    """changes: [{"op": "enable"|"disable"|"remove", "id": <ltk uuid>}]"""
    d = ltk_dir()
    if not d:
        raise RuntimeError("LTK Manager data folder not found")
    if league_in_game():
        raise RuntimeError("League is running - finish your game first (LTK Manager has to restart to apply this)")
    with LTK_LOCK:
        was_running = stop_ltk()
        libp = os.path.join(d, "library.json")
        bdir = os.path.join(DATA_DIR, "ltk_backups"); os.makedirs(bdir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(libp, os.path.join(bdir, f"library-{stamp}.json"))
        backups = sorted(f for f in os.listdir(bdir) if f.startswith("library-"))
        for old in backups[:-30]:
            try:
                os.remove(os.path.join(bdir, old))
            except Exception:
                pass
        lib = json.load(open(libp, encoding="utf-8"))
        prof = next((p for p in lib.get("profiles", []) if p.get("id") == lib.get("activeProfileId")), None)
        if prof is None:
            raise RuntimeError("Couldn't find LTK Manager's active profile")
        done = []
        for ch in changes:
            mid, op = ch["id"], ch["op"]
            entry = next((m for m in lib.get("mods", []) if m.get("id") == mid), None)
            if not entry:
                continue
            en = prof.setdefault("enabledMods", [])
            order = prof.setdefault("modOrder", [])
            if op == "enable":
                if mid not in en:
                    en.append(mid)
                if mid not in order:
                    order.append(mid)
            elif op == "disable":
                if mid in en:
                    en.remove(mid)
            elif op == "remove":
                lib["mods"] = [m for m in lib["mods"] if m.get("id") != mid]
                for f in lib.get("folders", []):
                    if mid in (f.get("modIds") or []):
                        f["modIds"].remove(mid)
                for p in lib.get("profiles", []):
                    for k in ("enabledMods", "modOrder"):
                        if mid in (p.get(k) or []):
                            p[k].remove(mid)
                    if isinstance(p.get("layerStates"), dict):
                        p["layerStates"].pop(mid, None)
                # keep the mod's files instead of deleting them
                slug = entry.get("slug") or mid
                rdir = os.path.join(DATA_DIR, "ltk_removed", f"{slug}-{stamp}")
                os.makedirs(rdir, exist_ok=True)
                mdir = os.path.join(d, "mods")
                for name in os.listdir(mdir):
                    if name == slug or name.startswith(slug + "."):
                        try:
                            shutil.move(os.path.join(mdir, name), os.path.join(rdir, name))
                        except Exception as e:
                            log("couldn't move LTK mod file", name, e)
                json.dump(entry, open(os.path.join(rdir, "library-entry.json"), "w"), indent=2)
            done.append(ch)
        tmp = libp + ".skinvault.tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(lib, f, indent=2, ensure_ascii=False)
        os.replace(tmp, libp)
        if restart and ltk_exe():
            start_ltk()
        LTK["t"] = 0
        threading.Timer(4, refresh_ltk_bg).start()
        return {"applied": len(done), "restarted": restart, "backup": f"library-{stamp}.json"}

_LTK_LOADING = threading.Lock()

_LTK_DIRTY = threading.Event()

def ltk_lib_stamp():
    d = ltk_dir()
    try:
        return os.path.getmtime(os.path.join(d, "library.json")) if d else None
    except Exception:
        return None

def refresh_ltk_bg(force=False):
    """Reload LTK state. If a load is already running, it reloads again once it finishes,
    so a change LTK writes mid-load is never missed."""
    _LTK_DIRTY.set()
    def job():
        if not _LTK_LOADING.acquire(blocking=False):
            return
        try:
            while _LTK_DIRTY.is_set():
                _LTK_DIRTY.clear()
                load_ltk(force)
        finally:
            _LTK_LOADING.release()
    threading.Thread(target=job, daemon=True).start()

def ltk_watch():
    """Reload whenever LTK Manager rewrites its library (installs, removes, toggles - from here or in LTK)."""
    last = None
    while True:
        st = ltk_lib_stamp()
        if st != last:
            if last is not None:
                time.sleep(1.0)   # let LTK finish writing
                refresh_ltk_bg()
            last = st
        time.sleep(2)

def ltk_payload():
    if time.time() - LTK["t"] > 120:
        refresh_ltk_bg()
    return {"found": LTK["found"], "dir": ltk_dir(), "profile": LTK["profile"], "error": LTK["error"],
            "loading": _LTK_LOADING.locked() or (not LTK["t"] and bool(ltk_dir())), "t": LTK["t"],
            "mods": list(LTK["mods"].values())}

# ---------------------------------------------------------------- updates
UPDATE = {"current": VERSION, "available": False}

def update_cache_path():
    return os.path.join(DATA_DIR, "update.json")

def update_repo():
    r = (CFG.get("update_repo") or updater.repo_from_git(APP_DIR) or GITHUB_REPO or "").strip()
    return "" if r.startswith("OWNER/") else r

def check_updates(force=False):
    if not force and not CFG.get("check_updates", True):
        return UPDATE
    try:
        UPDATE.clear(); UPDATE.update(updater.check(update_repo(), VERSION, update_cache_path(), force))
        if UPDATE.get("available"):
            log(f"Update available: {VERSION} -> {UPDATE.get('latest')}  ({UPDATE.get('url')})")
    except Exception as e:
        log("update check failed", e)
    return UPDATE

def update_watch():
    time.sleep(8)
    while True:
        check_updates()
        time.sleep(3600)

def apply_update():
    STATE["status"] = "updating"; STATE["current"] = "Downloading update..."
    try:
        def prog(msg):
            STATE["current"] = msg
        files = updater.apply(UPDATE.get("zip"), APP_DIR, VERSION, DATA_DIR, prog)
        log(f"Updated to {UPDATE.get('latest')} ({len(files)} files). Restarting...")
        push_event({"origin": "update", "ok": True, "version": UPDATE.get("latest")})
        time.sleep(1.5)
        restart_self()
    except Exception as e:
        log("update failed", e)
        push_event({"origin": "update", "ok": False, "msg": str(e)[:300]})
        STATE["status"] = "idle"; STATE["current"] = ""

def update_public():
    u = UPDATE
    return {k: u.get(k) for k in ("current", "latest", "available", "url", "notes", "name", "dismissed", "error", "checked")}

# ---------------------------------------------------------------- HTTP server
def public_mod(m):
    return {k: v for k, v in m.items() if not k.startswith("_")}

def state_payload(since=0):
    with LOCK:
        mods = [public_mod(m) for m in STATE["mods"].values()]
        events = [e for e in STATE["events"] if e["t"] > since]
    return {
        "status": STATE["status"], "progress": STATE["progress"], "total": STATE["total"], "current": STATE["current"],
        "last_scan": STATE["last_scan"], "scan_seconds": STATE["scan_seconds"], "library": LIB,
        "ddragon": DD["version"], "hashdb": HDB.loaded, "loose": STATE["loose"], "events": events,
        "champions": list(DD["champs"].values()), "mods": mods, "drop_dir": DROP_DIR,
        "game_dir": game_dir(), "ltmao": bool(ltmao_dir()), "ltk": bool(ltk_dir()),
        "new_days": CFG.get("new_days", NEW_DAYS), "seen_before": load_added().get("_seen_before", 0),
        "version": VERSION, "update": update_public(),
    }

def request_allowed(h, post):
    """Only answer pages served by Skin Vault itself: the Host must be localhost (blocks DNS rebinding) and
    state-changing requests must carry the X-SkinVault header (blocks other websites posting to us)."""
    host = (h.headers.get("Host") or "").split(":")[0].lower()
    if host not in ("127.0.0.1", "localhost", "[::1]", ""):
        h.send_response(403); h.end_headers(); return False
    if post and h.headers.get("X-SkinVault") != "1":
        h.send_response(403); h.end_headers(); return False
    return True

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        if not request_allowed(self, False):
            return
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self.send(200, open(os.path.join(APP_DIR, "index.html"), "rb").read(), "text/html; charset=utf-8")
        if u.path.startswith("/static/"):
            return serve_static(self, u.path)
        if u.path == "/api/state":
            return self.send(200, state_payload())
        if u.path == "/api/status":
            since = float(q.get("since", ["0"])[0])
            with LOCK:
                ev = [e for e in STATE["events"] if e["t"] > since]
            return self.send(200, {"status": STATE["status"], "progress": STATE["progress"], "total": STATE["total"],
                                   "current": STATE["current"], "last_scan": STATE["last_scan"], "events": ev})
        if u.path == "/api/image":
            img = mod_image(q.get("id", [""])[0])
            if not img:
                return self.send(404, {"error": "no image"})
            ctype = "image/jpeg" if img[:3] == b"\xff\xd8\xff" else "image/png"
            return self.send(200, img, ctype)
        if u.path == "/api/organize/plan":
            return self.send(200, organize_plan())
        if u.path == "/api/3d/model":
            try:
                sk = q.get("skin", [None])[0]
                m = model_payload(q.get("champ", [None])[0], int(sk) if sk not in (None, "") else None,
                                  q.get("id", [None])[0])
                return self.send(200, m)
            except Exception as e:
                traceback.print_exc()
                return self.send(200, {"error": str(e)})
        if u.path == "/api/3d/tex":
            t = TEX_SESSIONS.get(q.get("s", [""])[0], {}).get(q.get("k", [""])[0])
            if not t:
                return self.send(404, {"error": "no texture"})
            return self.send(200, t, "application/octet-stream")
        if u.path == "/api/settings":
            return self.send(200, {"game_dir": game_dir(), "library": LIB, "ltmao_dir": ltmao_dir(), "ltk_dir": ltk_dir(),
                                   "check_updates": CFG.get("check_updates", True), "update_repo": update_repo(), "version": VERSION})
        if u.path == "/api/update":
            return self.send(200, update_public() if not q.get("force") else (check_updates(True) and update_public()))
        if u.path == "/api/jobs":
            with JOB_LOCK:
                return self.send(200, {"jobs": JOBS[-100:], "queued": len(JOB_Q)})
        if u.path == "/api/ltk":
            if q.get("refresh"):
                refresh_ltk_bg()
            return self.send(200, ltk_payload())
        if u.path == "/api/skinnames":
            cid = q.get("champ", [""])[0]
            return self.send(200, cdragon_skin_names(cid))
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        if not request_allowed(self, True):
            return
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path == "/api/import":
                name = urllib.parse.unquote(self.headers.get("X-Filename") or "upload.fantome")
                name = safe_name(os.path.basename(name))
                n = int(self.headers.get("Content-Length") or 0)
                tmp = unique_path(os.path.join(INCOMING_DIR, name))
                with open(tmp, "wb") as f:
                    remaining = n
                    while remaining > 0:
                        chunk = self.rfile.read(min(remaining, 1 << 20))
                        if not chunk:
                            break
                        f.write(chunk); remaining -= len(chunk)
                if not name.lower().endswith(MOD_EXTS):
                    os.remove(tmp)
                    return self.send(200, {"results": [{"file": name, "ok": False, "msg": "Not a mod file (.fantome, .zip, .wad.client)"}]})
                res = import_path(tmp, "browser")
                threading.Thread(target=scan_library, daemon=True).start()
                return self.send(200, {"results": res})
            data = self.body_json()
            if u.path == "/api/settings":
                save_config({k: data[k] for k in ("game_dir", "ltmao_dir", "ltk_dir", "check_updates", "update_repo") if k in data})
                _GAME_DIR["t"] = 0; LTK["t"] = 0
                newlib = (data.get("library") or "").strip().strip('"')
                if newlib and os.path.normcase(os.path.abspath(newlib)) != os.path.normcase(LIB):
                    os.makedirs(newlib, exist_ok=True)
                    save_config({"library": os.path.abspath(newlib)})
                    threading.Timer(0.8, restart_self).start()
                    return self.send(200, {"restarting": True})
                return self.send(200, {"game_dir": game_dir(), "ltmao_dir": ltmao_dir(), "ltk_dir": ltk_dir()})
            if u.path == "/api/update/apply":
                if not UPDATE.get("available") or not UPDATE.get("zip"):
                    return self.send(200, {"ok": False, "msg": "No update available"})
                if STATE["status"] == "updating":
                    return self.send(200, {"ok": True})
                threading.Thread(target=apply_update, daemon=True).start()
                return self.send(200, {"ok": True})
            if u.path == "/api/update/dismiss":
                updater.dismiss(update_cache_path(), data.get("version")); UPDATE["dismissed"] = data.get("version")
                return self.send(200, {"ok": True})
            if u.path == "/api/shutdown":
                self.send(200, {"ok": True})
                threading.Timer(0.3, lambda: os._exit(0)).start()
                return
            if u.path == "/api/rescan":
                threading.Thread(target=scan_library, daemon=True).start()
                return self.send(200, {"ok": True})
            if u.path == "/api/open":
                m = STATE["mods"].get(data.get("id"))
                if m:
                    open_in_explorer(os.path.join(LIB, m["rel"]))
                elif data.get("path"):
                    open_in_explorer(os.path.join(LIB, data["path"]))
                return self.send(200, {"ok": True})
            if u.path == "/api/organize/apply":
                r = organize_apply(data.get("ids", []))
                scan_library()
                return self.send(200, r)
            if u.path == "/api/dedupe":
                r = dedupe_apply(data.get("ids"))
                scan_library()
                return self.send(200, r)
            if u.path == "/api/assign":
                r = assign(data.get("id"), data.get("champ"), data.get("skin"))
                scan_library()
                return self.send(200, r)
            if u.path == "/api/unpack":
                r = unpack(data.get("id"))
                scan_library()
                return self.send(200, r)
            if u.path == "/api/fix":
                ids = data.get("ids") or []
                if data.get("all_outdated"):
                    ids = [m["id"] for m in STATE["mods"].values() if m.get("outdated") and not m.get("dup_of") and m.get("champ")]
                jobs = [queue_fix(i, data.get("slot"), bool(data.get("swap_ltk"))) for i in ids]
                return self.send(200, {"queued": len([j for j in jobs if j])})
            if u.path == "/api/unpack-ltmao":
                try:
                    return self.send(200, {"ok": True, "path": unpack_ltmao(data.get("id"))})
                except Exception as e:
                    return self.send(200, {"ok": False, "msg": str(e)})
            if u.path == "/api/ltk/apply":
                try:
                    return self.send(200, {"ok": True, **ltk_apply(data.get("changes") or [])})
                except Exception as e:
                    traceback.print_exc()
                    return self.send(200, {"ok": False, "msg": str(e)})
            if u.path == "/api/ltk/add":
                try:
                    paths, lids = [], []
                    for i in data.get("ids") or []:
                        m, p = get_mod(i)
                        if m and p and os.path.isfile(p) and p.lower().endswith((".fantome", ".zip", ".modpkg")):
                            paths.append(p); lids.append(i)
                    if not paths:
                        return self.send(200, {"ok": False, "msg": "Only .fantome / .zip mods can be added to LTK Manager"})
                    queue_ltk_add(paths, lids)
                    return self.send(200, {"ok": True, "count": len(paths)})
                except Exception as e:
                    return self.send(200, {"ok": False, "msg": str(e)})
            if u.path == "/api/convert":
                ids = data.get("ids") or []
                if data.get("all"):
                    ids = [m["id"] for m in STATE["mods"].values() if m["filename"].lower().endswith(".zip") and m.get("champ")
                           and not m.get("dup_of") and not m.get("children") and m.get("type") != "Pack"]
                with JOB_LOCK:
                    CONV_Q.extend(i for i in ids if i not in CONV_Q)
                CONV_EVENT.set()
                return self.send(200, {"queued": len(ids)})
            if u.path == "/api/seen":
                with _ADDED_LOCK:
                    d = load_added(); d["_seen_before"] = time.time(); save_added(d)
                return self.send(200, {"seen_before": d["_seen_before"]})
            if u.path == "/api/delete":
                return self.send(200, delete_mods(data.get("ids") or [], bool(data.get("remove_ltk"))))
            if u.path == "/api/ltk/open":
                m = LTK["mods"].get(data.get("id"))
                if m and m.get("path"):
                    open_in_explorer(m["path"])
                return self.send(200, {"ok": True})
            if u.path == "/api/undo":
                r = undo_last()
                scan_library()
                return self.send(200, r)
            if u.path == "/api/update-data":
                def job():
                    STATE["status"] = "updating"; STATE["current"] = "Updating champion list..."
                    try:
                        load_ddragon(force=True)
                        if data.get("hashes"):
                            STATE["current"] = "Downloading file-path database (~230 MB)..."
                            rebuild_hashdb()
                    except Exception as e:
                        log("update failed", e)
                    STATE["status"] = "idle"
                    try:
                        os.remove(CACHE_PATH)
                    except Exception:
                        pass
                    scan_library()
                threading.Thread(target=job, daemon=True).start()
                return self.send(200, {"ok": True})
        except Exception as e:
            traceback.print_exc()
            return self.send(500, {"error": str(e)})
        return self.send(404, {"error": "not found"})

# ---------------------------------------------------------------- first-run setup
def setup_suggestions():
    home = os.path.expanduser("~")
    cands = [os.path.join(home, "Documents", "LoL Skins"), os.path.join(home, "LoL Skins")]
    for d in "CDEFG":
        for n in ("LoL skins", "LoL Skins", "League Skins", "LoL Mods"):
            p = f"{d}:\\{n}"
            if os.path.isdir(p):
                cands.insert(0, p)
    return {"setup": True, "version": VERSION, "suggest_library": cands[0], "candidates": cands[:6],
            "game_dir": lol3d.find_game_dir(CFG.get("game_dir")), "ltmao_dir": fixer.find_ltmao(CFG.get("ltmao_dir")),
            "ltk_dir": ltk_dir(), "python": sys.version.split()[0]}

STATIC_DIR = os.path.join(APP_DIR, "static")

def serve_static(h, path):
    rel = os.path.normpath(urllib.parse.unquote(path[len("/static/"):])).lstrip("\\/")
    full = os.path.join(STATIC_DIR, rel)
    if not os.path.abspath(full).startswith(os.path.abspath(STATIC_DIR)) or not os.path.isfile(full):
        return h.send(404, {"error": "not found"})
    ctype = "text/javascript; charset=utf-8" if full.endswith(".js") else "application/octet-stream"
    body = open(full, "rb").read()
    h.send_response(200); h.send_header("Content-Type", ctype); h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "max-age=86400"); h.end_headers(); h.wfile.write(body)

class SetupHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json"):
        body = json.dumps(body).encode() if isinstance(body, (dict, list)) else (body.encode() if isinstance(body, str) else body)
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.send_header("Cache-Control", "no-store"); self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not request_allowed(self, False):
            return
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self.send(200, open(os.path.join(APP_DIR, "index.html"), "rb").read(), "text/html; charset=utf-8")
        if u.path.startswith("/static/"):
            return serve_static(self, u.path)
        if u.path in ("/api/state", "/api/setup"):
            return self.send(200, setup_suggestions())
        if u.path == "/api/status":
            return self.send(200, {"status": "setup", "events": [], "progress": 0, "total": 0})
        return self.send(404, {"error": "setup mode"})

    def do_POST(self):
        if not request_allowed(self, True):
            return
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if u.path == "/api/shutdown":
            self.send(200, {"ok": True}); threading.Timer(0.3, lambda: os._exit(0)).start(); return
        if u.path == "/api/setup":
            lib = (data.get("library") or "").strip().strip('"')
            if not lib:
                return self.send(200, {"ok": False, "msg": "Pick a folder for your mods"})
            try:
                os.makedirs(lib, exist_ok=True)
            except Exception as e:
                return self.send(200, {"ok": False, "msg": f"Can't use that folder: {e}"})
            upd = {"library": os.path.abspath(lib)}
            for k in ("game_dir", "ltmao_dir", "ltk_dir"):
                if data.get(k):
                    upd[k] = data[k].strip().strip('"')
            save_config(upd)
            self.send(200, {"ok": True})
            threading.Timer(0.5, restart_self).start()
            return
        return self.send(404, {"error": "setup mode"})

def restart_self():
    log("Restarting with the new settings...")
    args = [a for a in sys.argv if a != "--no-browser"] + ["--no-browser"]
    if os.name == "nt":
        subprocess.Popen([sys.executable] + args, cwd=APP_DIR)
        os._exit(0)
    else:
        os.execv(sys.executable, [sys.executable] + args)

def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sck:
        return sck.connect_ex(("127.0.0.1", port)) == 0

def main():
    port = int(CFG.get("port", PORT))
    url = f"http://127.0.0.1:{port}"
    want_browser = CFG.get("open_browser", True) and "--no-browser" not in sys.argv
    if port_in_use(port):
        for _ in range(20):            # a restart may still be releasing the port
            time.sleep(0.25)
            if not port_in_use(port):
                break
        else:
            print(f"Skin Vault is already running - opening {url}", flush=True)
            if want_browser:
                webbrowser.open(url)
            return
    if SETUP_MODE:
        log(f"Skin Vault {VERSION} - first run: pick your mod folder in the browser ({url})")
        srv = ThreadingHTTPServer(("127.0.0.1", port), SetupHandler)
        if want_browser:
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return
    log(f"Skin Vault {VERSION}")
    log("Library:", LIB)
    load_ddragon()
    HDB.load()
    threading.Thread(target=scan_library, daemon=True).start()
    threading.Thread(target=watch_drop_folder, daemon=True).start()
    threading.Thread(target=fix_worker, daemon=True).start()
    threading.Thread(target=ltk_watch, daemon=True).start()
    threading.Thread(target=ltk_add_worker, daemon=True).start()
    threading.Thread(target=conv_worker, daemon=True).start()
    threading.Thread(target=update_watch, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    log("Running at", url, "(close this window to stop)")
    if want_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
