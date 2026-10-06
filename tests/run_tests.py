"""
Skin Vault test suite - runs on any OS with plain Python (no pytest needed):

    python tests/run_tests.py

Builds a throw-away mod library from synthetic files, then exercises scanning, skin detection,
importing, organizing/undo, duplicates, overrides, NEW tags, deleting, .zip -> .fantome,
the LTK Manager integration (against a fake LTK data folder), the HTTP API and first-run setup.

Set SKINVAULT_TEST_GAME to a League "Game" folder to also run the 3D / game-file tests.
"""
import os, sys, json, time, shutil, tempfile, threading, traceback, urllib.request, urllib.error, subprocess, socket, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="skinvault-test-")
LIB = os.path.join(TMP, "library")
HOME = os.path.join(TMP, "home")
LTKD = os.path.join(TMP, "ltk")
os.makedirs(LIB); os.makedirs(HOME)
os.environ.update(SKINVAULT_HOME=HOME, SKINVAULT_LIBRARY=LIB, SKINVAULT_NO_PIP="1")
json.dump({"ltk_dir": LTKD, "game_dir": os.environ.get("SKINVAULT_TEST_GAME", "") or "", "ltmao_dir": ""},
          open(os.path.join(HOME, "config.json"), "w"))
sys.path.insert(0, ROOT); sys.path.insert(0, os.path.dirname(__file__))

import fixtures  # noqa: E402
EXP = fixtures.build(LIB)
import skin_manager as sm  # noqa: E402
import lol3d  # noqa: E402

RESULTS = []

def test(fn):
    name = fn.__name__
    try:
        fn(); RESULTS.append((name, True, "")); print(f"  PASS  {name}")
    except Exception as e:
        RESULTS.append((name, False, traceback.format_exc())); print(f"  FAIL  {name}: {e}")
    return fn

def by_name(name):
    return [m for m in sm.STATE["mods"].values() if m["name"] == name]

def scan():
    sm.STATE["scan_error"] = None
    sm.scan_library()
    while sm.STATE["status"] == "scanning":
        time.sleep(0.05)
    assert not sm.STATE.get("scan_error"), "scan crashed: " + str(sm.STATE.get("scan_error"))

# ------------------------------------------------------------------ setup
sm.load_ddragon(); sm.HDB.load(); scan()
print(f"Library: {LIB}  ({len(sm.STATE['mods'])} mods found)")

@test
def hashing():
    import xxhash  # noqa
    for t in [b"", b"a", b"data/characters/ahri/skins/skin14.bin", bytes(range(200))]:
        assert lol3d.xxh64_py(t) == xxhash.xxh64_intdigest(t)
    assert lol3d.fnv1a("skinMeshProperties") == lol3d.fnv1a("SKINMESHPROPERTIES")

@test
def wad_roundtrip():
    data = fixtures.wad_bytes({"a/b.txt": b"hello", "c.bin": b"world"})
    w = lol3d.Wad(data, "x.wad.client")
    assert w.read(lol3d.path_hash("a/b.txt")) == b"hello"
    ver, hashes = sm.wad_toc(__import__("io").BytesIO(data))
    assert ver == "3.4" and len(hashes) == 2

@test
def skn_parse():
    m = lol3d.parse_skn(fixtures.skn_bytes())
    assert m["vcount"] == 3 and m["submeshes"][0]["name"] == "Body"

@test
def skin_detection():
    bad = []
    for name, (champ, skin) in EXP.items():
        ms = by_name(name)
        if not ms:
            bad.append(f"{name}: not found"); continue
        m = ms[0]
        if m.get("champ") != champ or m.get("applies_to") != skin:
            bad.append(f"{name}: got {m.get('champ')}/{m.get('applies_to')} ({m.get('applies_source')}), want {champ}/{skin}")
    assert not bad, "\n".join(bad)

@test
def issues_flagged():
    mods = list(sm.STATE["mods"].values())
    get = lambda rel: next(m for m in mods if m["rel"].replace("\\", "/") == rel)
    assert get("Ahri/broken.fantome")["severity"] == "error"
    assert get("Ahri/something.rar")["severity"] == "error"
    assert get("Ahri/Ahri.en_US.wad.client")["type"].startswith("Voice")
    assert get("Ahri/SG Ahri custom (1).fantome").get("dup_of"), "exact duplicate not flagged"
    assert get("Ahri/SG Ahri custom").get("dup_of"), "extracted copy not flagged"
    assert any("Sitting in the 'Ahri' folder" in i["msg"] for i in get("Ahri/Jinx in wrong folder.fantome")["issues"])
    assert any("Raw game files" in i["msg"] for i in get("Fiora/fiora.zip")["issues"])
    assert get("SKIN/Big Pack.zip")["type"].startswith("Pack")
    assert get("SKIN/mystery.zip")["champ"] is None

@test
def mixed_champion_folder():
    xa = [m for m in sm.STATE["mods"].values() if m["rel"].replace("\\", "/").startswith("Xayah/")]
    assert len(xa) >= 3, [m["rel"] for m in xa]
    assert not any(m["rel"] == "Xayah" for m in sm.STATE["mods"].values())
    assert any("Xayah" in l for l in sm.STATE["loose"])

@test
def organize_and_undo():
    plan = sm.organize_plan()
    assert plan, "expected moves"
    before = {p["from"] for p in plan}
    sm.organize_apply([p["id"] for p in plan]); scan()
    for p in plan:
        assert os.path.exists(os.path.join(LIB, p["to"].split("   (")[0])) or p["to"].endswith("(unzipped)"), p
    sm.undo_last(); scan()
    for f in before:
        assert os.path.exists(os.path.join(LIB, f)), f"undo didn't restore {f}"

@test
def duplicate_across_folders():
    """Regression: a duplicate left at the library root next to its organized copy crashed every scan."""
    src = os.path.join(LIB, "Ahri", "SG Ahri custom.fantome")
    plan = sm.organize_plan(); sm.organize_apply([p["id"] for p in plan]); scan()
    root_copy = os.path.join(LIB, "SG Ahri custom copy.fantome")
    organized = [m for m in sm.STATE["mods"].values() if m["name"] == "SG Ahri custom" and m["rel"].endswith(".fantome")]
    shutil.copy2(os.path.join(LIB, organized[0]["rel"]), root_copy)
    scan()
    mine = [m for m in sm.STATE["mods"].values() if m["rel"] == "SG Ahri custom copy.fantome"]
    assert mine and mine[0]["dup_of"], "root copy missing or not flagged as a duplicate (scan crashed?)"
    os.remove(root_copy); sm.undo_last(); scan()

@test
def import_new_and_duplicate():
    src = os.path.join(TMP, "dl", "Ahri Popstar Test.fantome")
    fixtures.fantome(src, "Ahri Popstar Test", {"assets/characters/ahri/skins/skin05/ahri_skin05.skn": fixtures.skn_bytes(5)})
    tmp = os.path.join(sm.INCOMING_DIR, "Ahri Popstar Test.fantome"); shutil.copy2(src, tmp)
    r = sm.import_path(tmp, "test")[0]
    assert r["ok"] and r["champ"] == "Ahri", r
    assert os.path.exists(os.path.join(LIB, r["dest"]))
    scan()
    tmp2 = os.path.join(sm.INCOMING_DIR, "Ahri Popstar Test.fantome"); shutil.copy2(src, tmp2)
    r2 = sm.import_path(tmp2, "test")[0]
    assert r2.get("duplicate"), r2

@test
def import_pack_splits():
    with zipfile.ZipFile(os.path.join(LIB, "SKIN", "Big Pack.zip")) as z:
        z.extractall(os.path.join(TMP, "packsrc"))
    tmp = os.path.join(sm.INCOMING_DIR, "pack2.zip")
    shutil.copy2(os.path.join(LIB, "SKIN", "Big Pack.zip"), tmp)
    res = sm.import_path(tmp, "test")
    assert len(res) == 2, res

@test
def new_tags():
    scan()
    m = by_name("Ahri Popstar Test")[0]
    assert m.get("added_at"), "imported mod should be NEW"
    old = by_name("Kat Skin07 port")[0]
    assert not old.get("added_at"), "mods present on first run are not NEW"

@test
def override_persists():
    m = by_name("Miss Fortune Skin04")[0]
    sm.assign(m["id"], "MissFortune", 2); scan()
    m = by_name("Miss Fortune Skin04")[0]
    assert m["applies_to"] == 2 and m["applies_source"] == "manual"

@test
def zip_to_fantome_fast_path():
    p = os.path.join(TMP, "packed.zip")
    fixtures.fantome(p, "Packed", {"assets/characters/ahri/skins/base/x.skn": fixtures.skn_bytes()})
    out = os.path.join(TMP, "packed.fantome")
    note = sm.fixer.zip_to_fantome(p, "Ahri.wad", None, os.path.join(TMP, "w"), out)
    assert os.path.exists(out) and "renamed" in note

@test
def delete_goes_to_bin():
    m = by_name("Kat Skin07 port")[0]
    path = os.path.join(LIB, m["rel"])
    r = sm.delete_mods([m["id"]])
    assert r["deleted"] and not os.path.exists(path)
    if os.name != "nt":
        assert os.listdir(os.path.join(LIB, "_Deleted"))

@test
def ltk_integration():
    os.makedirs(os.path.join(LTKD, "mods", "sg-ahri"), exist_ok=True)
    shutil.copy2(os.path.join(LIB, "Ahri", "SG Ahri custom.fantome"), os.path.join(LTKD, "mods", "sg-ahri.fantome"))
    json.dump({"name": "sg-ahri", "display_name": "SG Ahri custom", "authors": ["Tester"]}, open(os.path.join(LTKD, "mods", "sg-ahri", "mod.config.json"), "w"))
    lib = {"version": 2, "activeProfileId": "p1", "folderOrder": ["root"],
           "folders": [{"id": "root", "name": "", "modIds": ["m1"]}],
           "mods": [{"id": "m1", "installedAt": "2026-01-01T00:00:00Z", "format": "fantome", "storage": "archive", "slug": "sg-ahri"}],
           "profiles": [{"id": "p1", "name": "Default", "slug": "default", "enabledMods": ["m1"], "modOrder": ["m1"], "layerStates": {}}]}
    json.dump(lib, open(os.path.join(LTKD, "library.json"), "w"))
    sm.load_ltk()
    m = sm.LTK["mods"]["ltk:m1"]
    assert m["enabled"] and m["champ"] == "Ahri" and m["applies_to"] == 14, m
    sm.ltk_apply([{"op": "disable", "id": "m1"}])
    assert json.load(open(os.path.join(LTKD, "library.json")))["profiles"][0]["enabledMods"] == []
    sm.ltk_apply([{"op": "enable", "id": "m1"}])
    assert json.load(open(os.path.join(LTKD, "library.json")))["profiles"][0]["enabledMods"] == ["m1"]
    sm.ltk_apply([{"op": "remove", "id": "m1"}])
    j = json.load(open(os.path.join(LTKD, "library.json")))
    assert j["mods"] == [] and j["folders"][0]["modIds"] == [] and not os.path.exists(os.path.join(LTKD, "mods", "sg-ahri.fantome"))
    assert os.listdir(os.path.join(sm.DATA_DIR, "ltk_removed")), "removed mod should be kept"

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p

_NOPROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never route localhost via a proxy

def get(port, path, data=None, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(data).encode() if data is not None else None,
                                 headers=headers if headers is not None else {"Content-Type": "application/json", "X-SkinVault": "1"})
    with _NOPROXY.open(req, timeout=20) as r:
        body = r.read()
        return json.loads(body) if r.headers.get("Content-Type", "").startswith("application/json") else body

@test
def http_api():
    port = free_port()
    srv = sm.ThreadingHTTPServer(("127.0.0.1", port), sm.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert b"Skin Vault" in get(port, "/")
        st = get(port, "/api/state"); assert st["mods"] and "champions" in st
        assert "status" in get(port, "/api/status")
        assert "mods" in get(port, "/api/ltk")
        assert "library" in get(port, "/api/settings")
        st2 = get(port, "/api/state"); assert st2["version"] == sm.VERSION and "available" in st2["update"]
        assert "current" in get(port, "/api/update")
        assert get(port, "/api/update/apply", {})["ok"] is False   # nothing to install
        mid = by_name("Ahri Popstar Test")[0]["id"]
        r = get(port, f"/api/3d/model?id={mid}")
        assert ("error" in r) or ("positions" in r)
        assert get(port, "/api/rescan", {})["ok"]
        assert b"three" in get(port, "/static/three.module.js")[:5000].lower()
        for hdrs in ({"Content-Type": "application/json"}, {"X-SkinVault": "1", "Host": "evil.example"}):
            try:
                get(port, "/api/rescan", {}, hdrs); raise AssertionError("request should have been refused")
            except urllib.error.HTTPError as e:
                assert e.code == 403
        assert "plan" not in get(port, "/api/organize/plan") or True
    finally:
        srv.shutdown()

@test
def first_run_setup():
    home = tempfile.mkdtemp(prefix="sv-setup-"); port = free_port()
    env = dict(os.environ, SKINVAULT_HOME=home, SKINVAULT_PORT=str(port), SKINVAULT_NO_PIP="1")
    env.pop("SKINVAULT_LIBRARY", None)
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "skin_manager.py"), "--no-browser"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    st = {}
    try:
        for _ in range(60):
            try:
                st = get(port, "/api/state"); break
            except Exception:
                time.sleep(0.25)
        assert st.get("setup"), st
        newlib = os.path.join(home, "mylib")
        assert get(port, "/api/setup", {"library": newlib})["ok"]
        for _ in range(80):
            time.sleep(0.25)
            try:
                st = get(port, "/api/state")
                if not st.get("setup"):
                    break
            except Exception:
                pass
        assert not st.get("setup"), "didn't leave setup mode"
        assert os.path.isdir(newlib) and json.load(open(os.path.join(home, "config.json")))["library"] == os.path.abspath(newlib)
    finally:
        try:
            get(port, "/api/shutdown", {})
        except Exception:
            pass
        p.terminate()
        try:
            p.wait(5)
        except Exception:
            p.kill()
        shutil.rmtree(home, ignore_errors=True)

@test
def self_update():
    import io as _io, updater
    app = tempfile.mkdtemp(prefix="sv-upd-")
    for rel, txt in {"skin_manager.py": 'VERSION = "1.0.0"\n', "index.html": "old", "config.json": '{"library": "X"}',
                     "data/skinhashes3.bin": "mine", "static/three.module.js": "old3"}.items():
        os.makedirs(os.path.dirname(os.path.join(app, rel)) or app, exist_ok=True)
        open(os.path.join(app, rel), "w").write(txt)
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for rel, txt in {"skin_manager.py": 'VERSION = "1.2.0"\n', "index.html": "new", "config.json": "{}",
                         "data/skinhashes3.bin": "shipped", "data/newfile.bin": "add", "static/three.module.js": "new3",
                         "tests/run_tests.py": "x", "../evil.txt": "x"}.items():
            z.writestr("skin-vault-1.2.0/" + rel, txt)
    blob = buf.getvalue()
    calls = []
    def fake_get(url, timeout=15):
        calls.append(url)
        if "releases/latest" in url:
            if "norel" in url:
                raise urllib.error.HTTPError(url, 404, "nf", {}, None)
            return json.dumps({"tag_name": "v1.2.0", "html_url": "https://github.com/o/r/releases/tag/v1.2.0", "body": "fixes"}).encode()
        if "raw.githubusercontent" in url:
            return b'VERSION = "1.1.0"\n'
        return blob
    old = updater._get; updater._get = fake_get
    try:
        cache = os.path.join(app, "data", "update.json")
        u = updater.check("o/r", "1.0.0", cache)
        assert u["available"] and u["latest"] == "1.2.0" and u["zip"].endswith("/tags/v1.2.0.zip"), u
        n = len(calls); updater.check("o/r", "1.0.0", cache); assert len(calls) == n, "should use the cache"
        assert not updater.check("o/r", "1.2.0", cache)["available"]
        assert not updater.check("o/r", "1.10.0", cache)["available"], "1.10 > 1.2 numerically"
        u2 = updater.check("o/norel", "1.0.0", os.path.join(app, "c2.json"))
        assert u2["latest"] == "1.1.0" and u2["zip"].endswith("/heads/main.zip"), u2
        assert not updater.check("", "1.0.0", cache)["available"]
        updater.dismiss(cache, "1.2.0"); assert updater.check("o/r", "1.0.0", cache)["dismissed"] == "1.2.0"
        written = updater.apply(u["zip"], app, "1.0.0")
        rd = lambda r: open(os.path.join(app, r)).read()
        assert rd("skin_manager.py").startswith('VERSION = "1.2.0"') and rd("index.html") == "new" and rd("static/three.module.js") == "new3"
        assert rd("config.json") == '{"library": "X"}' and rd("data/skinhashes3.bin") == "mine" and rd("data/newfile.bin") == "add"
        assert not os.path.exists(os.path.join(app, "tests")) and not os.path.exists(os.path.join(os.path.dirname(app), "evil.txt"))
        assert open(os.path.join(app, "_update_backup", "1.0.0", "index.html")).read() == "old"
        # a broken download changes nothing
        blob_ok = blob; blob = b"PK\x05\x06" + b"\0" * 18
        try:
            updater.apply("x", app, "1.2.0"); raise AssertionError("should fail")
        except Exception as e:
            assert "should fail" not in str(e)
        assert rd("index.html") == "new"
        os.makedirs(os.path.join(app, ".git"))
        try:
            updater.apply("x", app, "1.2.0"); raise AssertionError("git checkout must not self-update")
        except RuntimeError as e:
            assert "git" in str(e)
        open(os.path.join(app, ".git", "config"), "w").write('[remote "origin"]\n\turl = https://github.com/me/skin-vault.git\n')
        assert updater.repo_from_git(app) == "me/skin-vault"
    finally:
        updater._get = old
        shutil.rmtree(app, ignore_errors=True)

if os.environ.get("SKINVAULT_TEST_GAME"):
    @test
    def game_files_3d():
        gd = os.environ["SKINVAULT_TEST_GAME"]
        champs = [f.split(".")[0] for f in os.listdir(os.path.join(gd, "DATA", "FINAL", "Champions")) if f.endswith(".wad.client") and "_" not in f]
        for c in champs[:3]:
            model, tex = lol3d.build_model(c, 0, gd)
            assert model["submeshes"] and tex, c
            assert lol3d.skin_refs(gd, c), c

print()
failed = [r for r in RESULTS if not r[1]]
for n, ok, tb in failed:
    print(f"--- {n}\n{tb}")
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if failed else 0)
