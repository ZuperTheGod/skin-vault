"""Update check + one-click self-update from GitHub.

check():  asks GitHub for the latest release (falls back to the VERSION line on the main branch when the repo has
          no releases yet). Cached, so GitHub is asked at most once every few hours.
apply():  downloads that version's source zip and replaces the app's own files. Never touches config.json, the
          user's caches in data/, logs or the mod library. The replaced files are kept in _update_backup/<old version>/.
"""
import io, json, os, re, shutil, time, urllib.error, urllib.request, zipfile

CHECK_EVERY = 6 * 3600
UA = {"User-Agent": "SkinVault-updater", "Accept": "application/vnd.github+json"}
# Files/folders in the download that are never written over a user's install
KEEP = {"config.json", ".git", ".github", "tests", "docs", "logs", "incoming", "work", "unpacked", "_update_backup"}


def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or "0"))[:4]) or (0,)


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def repo_from_git(app_dir):
    """owner/name from a git checkout's origin remote (for people who cloned instead of downloading)."""
    try:
        cfg = open(os.path.join(app_dir, ".git", "config"), encoding="utf-8").read()
        m = re.search(r'url\s*=\s*(?:https://github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?\s*$', cfg, re.M)
        return m.group(1) if m else None
    except Exception:
        return None


def fetch_latest(repo):
    """{'version', 'url', 'notes', 'zip'} for the newest published version, or raises."""
    try:
        rel = json.loads(_get(f"https://api.github.com/repos/{repo}/releases/latest"))
        tag = rel.get("tag_name") or ""
        return {"version": tag.lstrip("vV"), "url": rel.get("html_url") or f"https://github.com/{repo}/releases",
                "notes": (rel.get("body") or "").strip()[:4000], "name": rel.get("name") or tag,
                "zip": f"https://github.com/{repo}/archive/refs/tags/{tag}.zip"}
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    # no releases yet -> read VERSION from the main branch
    src = _get(f"https://raw.githubusercontent.com/{repo}/main/skin_manager.py").decode("utf-8", "replace")
    m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)', src, re.M)
    if not m:
        raise RuntimeError("couldn't read the version on GitHub")
    return {"version": m.group(1), "url": f"https://github.com/{repo}", "notes": "", "name": m.group(1),
            "zip": f"https://github.com/{repo}/archive/refs/heads/main.zip"}


def check(repo, current, cache_path, force=False):
    """Returns {'current', 'latest', 'available', 'url', 'notes', 'checked', 'error'} (cached between calls)."""
    info = {}
    try:
        info = json.load(open(cache_path, encoding="utf-8"))
    except Exception:
        pass
    if not repo:
        return {"current": current, "available": False, "error": "no GitHub repo configured"}
    if force or info.get("repo") != repo or time.time() - info.get("checked", 0) > CHECK_EVERY:
        try:
            latest = fetch_latest(repo)
            info = dict(latest, repo=repo, checked=time.time(), error=None, dismissed=info.get("dismissed"))
        except Exception as e:
            info = dict(info, repo=repo, checked=time.time(), error=str(e)[:200])
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            json.dump(info, open(cache_path, "w", encoding="utf-8"), indent=1)
        except Exception:
            pass
    latest = info.get("version")
    return {"current": current, "latest": latest, "available": bool(latest) and vtuple(latest) > vtuple(current),
            "url": info.get("url"), "notes": info.get("notes", ""), "name": info.get("name"), "zip": info.get("zip"),
            "checked": info.get("checked"), "error": info.get("error"), "dismissed": info.get("dismissed")}


def dismiss(cache_path, version):
    try:
        info = json.load(open(cache_path, encoding="utf-8"))
        info["dismissed"] = version
        json.dump(info, open(cache_path, "w", encoding="utf-8"), indent=1)
    except Exception:
        pass


def apply(zip_url, app_dir, current, data_dir=None, progress=lambda s: None, zip_bytes=None):
    """Download + install. Returns the list of files written. Raises on any problem before files are touched."""
    if os.path.isdir(os.path.join(app_dir, ".git")):
        raise RuntimeError("This copy is a git checkout - update it with 'git pull' instead.")
    progress("Downloading update…")
    blob = zip_bytes if zip_bytes is not None else _get(zip_url, timeout=300)
    z = zipfile.ZipFile(io.BytesIO(blob))
    names = [n for n in z.namelist() if not n.endswith("/")]
    # GitHub zips wrap everything in one "<repo>-<ref>/" folder
    prefix = os.path.commonprefix(names)
    prefix = prefix[:prefix.rfind("/") + 1] if "/" in prefix else ""
    files = {}
    for n in names:
        rel = n[len(prefix):]
        parts = rel.split("/")
        if not rel or parts[0] in KEEP or ".." in parts or rel.startswith("/") or ":" in rel:
            continue
        files[rel] = n
    if "skin_manager.py" not in files or "index.html" not in files:
        raise RuntimeError("The download doesn't look like Skin Vault - nothing was changed.")
    # quick sanity check: the new code at least compiles
    compile(z.read(files["skin_manager.py"]), "skin_manager.py", "exec")

    progress("Installing update…")
    backup = os.path.join(app_dir, "_update_backup", str(current))
    written = []
    data_dir = os.path.normcase(os.path.abspath(data_dir)) if data_dir else None
    for rel, n in sorted(files.items()):
        dest = os.path.join(app_dir, *rel.split("/"))
        if rel.startswith("data/") and os.path.exists(dest):
            continue        # the user's (possibly newer, rebuilt) data files win; new data files are added
        if os.path.exists(dest):
            b = os.path.join(backup, *rel.split("/"))
            os.makedirs(os.path.dirname(b), exist_ok=True)
            shutil.copy2(dest, b)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".new"
        with open(tmp, "wb") as f:
            f.write(z.read(n))
        os.replace(tmp, dest)
        written.append(rel)
    return written
