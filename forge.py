#!/usr/bin/env python3
"""Yeeterson Forge: keeps World of Warcraft: Forever addons installed and current on Linux.

  yforge status                       what is installed and what is newer upstream
  yforge update [--force]             install missing addons, update outdated ones
  yforge add <slug> [beta|alpha]      manage a CurseForge addon (slug = last part of its URL)
  yforge add github:<owner>/<repo>    manage an addon published as GitHub releases
  yforge remove <slug>                stop managing it and delete its folders
  yforge rollback <slug>              put back the previous version and hold it there
  yforge import <file.zip> ...        install a zip you downloaded yourself, after verifying it
  yforge init [path] [--flavor NAME]  find the game and write a starting forge.json
  yforge timer on|off                 check for updates every 10 minutes (systemd user timer)
  yforge doctor                       show paths, the key source and whether the game is running

One file, standard library only, no telemetry, no account. It talks to api.curseforge.com and
api.github.com and to nothing else.

How it stays safe: every download is hash-checked, unpacked in a scratch folder, refused unless
a .toc lists a Forever interface version, and swapped in folder by folder with the old version
kept as a backup. A failed install is rolled back. Nothing is touched while the game is running
unless --force is given. Addon folders it did not install are never modified.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

VERSION = "1.0.0"
API = "https://api.curseforge.com/v1"
GITHUB = "https://api.github.com"
USER_AGENT = f"yeeterson-forge/{VERSION}"
KEY_NAME = "CURSEFORGE_API_KEY"
CHANNELS = {"release": 1, "beta": 2, "alpha": 3}
CHANNEL_NAME = {v: k for k, v in CHANNELS.items()}
MAX_UNPACKED_BYTES = 2 * 1024 ** 3
MAX_ARCHIVE_ENTRIES = 100_000
GAME_PROCESS = re.compile(r"(?:^|[\\/ ])Wow(?:B|T|Classic|ClassicB|ClassicT)?\.exe(?:\s|$)")
DEFAULT_CONFIG = {
    "forever_game_version_type": 88568,
    "interface_range": [16000, 19999],
    "wow_root": "",
    "addon_dirs": [],
    "import_dirs": ["~/Downloads"],
    "keep_backups": 3,
    "addons": [],
}
QUIET = False


class ForgeError(Exception):
    """A problem with one addon. It is reported and the other addons carry on."""


# ---------------------------------------------------------------------------
# Where things live
# ---------------------------------------------------------------------------

def find_home():
    """YFORGE_HOME, else the script's own folder when it holds a forge.json, else ~/.config."""
    env = os.environ.get("YFORGE_HOME")
    if env:
        return Path(env).expanduser()
    here = Path(__file__).resolve().parent
    if (here / "forge.json").exists():
        return here
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "yeeterson-forge"


HOME = find_home()


def paths():
    return {name: HOME / name for name in ("forge.json", "state.json", "cache", "backups", "logs", "curseforge.key")}


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        paths()["logs"].mkdir(parents=True, exist_ok=True)
        with open(paths()["logs"] / "forge.log", "a") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    if not QUIET:
        print(msg)


def read_json(path, fallback):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return fallback


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def load_config():
    cfg = read_json(paths()["forge.json"], None)
    if cfg is None:
        raise SystemExit(f"No forge.json in {HOME}. Run: yforge init")
    for key, value in DEFAULT_CONFIG.items():
        cfg.setdefault(key, value)
    return cfg


def addon_dirs(cfg):
    return [Path(d).expanduser() for d in cfg["addon_dirs"]]


# ---------------------------------------------------------------------------
# Network. Everything that leaves the machine goes through these two functions.
# ---------------------------------------------------------------------------

def http_json(url, headers):
    """GET a JSON document. Returns (data, response headers); data is None on 304 Not Modified."""
    req = urllib.request.Request(url, headers={"user-agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.load(res), dict(res.headers)
    except urllib.error.HTTPError as err:
        if err.code == 304:
            return None, dict(err.headers)
        if err.code in (401, 403) and "curseforge" in url:
            raise ForgeError("CurseForge refused the API key (check it with: yforge doctor)") from None
        if err.code in (403, 429) and "github" in url:
            raise ForgeError("GitHub rate limit reached, it will work again within the hour") from None
        raise ForgeError(f"{urllib.parse.urlsplit(url).netloc} answered {err.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        raise ForgeError(f"could not reach {urllib.parse.urlsplit(url).netloc}: {getattr(err, 'reason', err)}") from None


def http_download(url, dest):
    req = urllib.request.Request(url, headers={"user-agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=120) as res, open(dest, "wb") as out:
            shutil.copyfileobj(res, out)
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        Path(dest).unlink(missing_ok=True)
        raise ForgeError(f"download failed: {getattr(err, 'reason', err)}") from None


# ---------------------------------------------------------------------------
# CurseForge
# ---------------------------------------------------------------------------

def curseforge_key(cfg):
    """The user's own key: CURSEFORGE_API_KEY, then curseforge.key beside forge.json, then a .env file."""
    key = os.environ.get(KEY_NAME, "").strip()
    if key:
        return key, f"environment variable {KEY_NAME}"
    key_file = paths()["curseforge.key"]
    if key_file.exists():
        key = key_file.read_text().strip()
        if key:
            return key, str(key_file)
    env_file = cfg.get("curseforge_env")
    if env_file and Path(env_file).expanduser().exists():
        for line in Path(env_file).expanduser().read_text().splitlines():
            name, _, value = line.partition("=")
            value = value.strip().strip("'\"")
            if name.strip() == KEY_NAME and value:
                return value, str(env_file)
    return None, None


def cf(cfg, path):
    key, _ = curseforge_key(cfg)
    if not key:
        raise ForgeError(f"no CurseForge API key. Get a free one at https://console.curseforge.com and save it to {paths()['curseforge.key']}")
    data, _ = http_json(API + path, {"x-api-key": key, "accept": "application/json"})
    return data["data"]


def resolve_mod(cfg, slug):
    for mod in cf(cfg, f"/mods/search?gameId=1&classId=1&slug={urllib.parse.quote(slug)}"):
        if mod["slug"] == slug:
            return mod
    raise ForgeError(f"no CurseForge addon with slug {slug}")


def forever_files(cfg, mod_id):
    files = cf(cfg, f"/mods/{mod_id}/files?gameVersionTypeId={cfg['forever_game_version_type']}&pageSize=50")
    files = [f for f in files if f.get("isAvailable", True)]
    files.sort(key=lambda f: f["fileDate"], reverse=True)
    return files


def choose(files, channel):
    """Newest file at the wanted stability or better. Falls back to less stable and says which."""
    if not files:
        return None, None
    want = CHANNELS.get(channel, 1)
    for level in (1, 2, 3):
        if level < want:
            continue
        ok = [f for f in files if f["releaseType"] <= level]
        if ok:
            return ok[0], (None if level == want else CHANNEL_NAME[level])
    return files[0], CHANNEL_NAME.get(files[0]["releaseType"])


def sha1_values(file):
    return [h["value"].lower() for h in file.get("hashes", []) if h.get("algo") == 1]


# ---------------------------------------------------------------------------
# GitHub releases
# ---------------------------------------------------------------------------

def github_files(repo, entry):
    """Releases that carry a .zip, shaped like CurseForge file records. Uses the ETag so an
    unchanged repository costs nothing against GitHub's hourly limit."""
    headers = {"accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["authorization"] = f"Bearer {token}"
    if entry.get("gh_etag") and entry.get("gh_files") is not None:
        headers["if-none-match"] = entry["gh_etag"]
    releases, response = http_json(f"{GITHUB}/repos/{repo}/releases?per_page=20", headers)
    if releases is None:
        return entry["gh_files"]
    files = []
    for release in releases:
        if release.get("draft"):
            continue
        asset = next((a for a in release.get("assets", []) if a["name"].lower().endswith(".zip")), None)
        if not asset:
            continue
        digest = asset.get("digest") or ""
        files.append({
            "id": f"gh{asset['id']}", "fileName": asset["name"], "displayName": release["tag_name"],
            "releaseType": 2 if release.get("prerelease") else 1, "fileDate": release.get("published_at") or "",
            "downloadUrl": asset["browser_download_url"], "dependencies": [],
            "sha256": digest[7:].lower() if digest.startswith("sha256:") else None,
        })
    files.sort(key=lambda f: f["fileDate"], reverse=True)
    etag = next((v for k, v in response.items() if k.lower() == "etag"), None)
    entry["gh_etag"], entry["gh_files"] = etag, files
    return files


# ---------------------------------------------------------------------------
# Archives
# ---------------------------------------------------------------------------

def file_hashes(path):
    sha1, sha256 = hashlib.sha1(), hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            sha1.update(block)
            sha256.update(block)
    return sha1.hexdigest(), sha256.hexdigest()


def verify(path, file):
    """True when the file on disk is exactly the upstream file. A file with no published hash
    can be downloaded from its official URL but can never be imported from somewhere else."""
    sha1, sha256 = file_hashes(path)
    expected = sha1_values(file)
    if expected and sha1 not in expected:
        return False, sha1
    if file.get("sha256") and sha256 != file["sha256"]:
        return False, sha1
    return True, sha1


def fetch(file):
    """Download from the URL the author allows. Returns (zip path, sha1)."""
    url = file.get("downloadUrl")
    if not url:
        raise ForgeError("the author does not allow third-party downloads")
    cache = paths()["cache"]
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / f"{file['id']}.zip"
    http_download(url, dest)
    ok, sha1 = verify(dest, file)
    if not ok:
        dest.unlink(missing_ok=True)
        raise ForgeError("download failed its hash check")
    return dest, sha1


def unpack(zip_path, cfg):
    """Extract into a fresh scratch folder. Returns (scratch, [top level addon folders])."""
    scratch = paths()["cache"] / f"unpack-{Path(zip_path).stem}-{os.getpid()}"
    shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True)
    try:
        try:
            archive = zipfile.ZipFile(zip_path)
        except (zipfile.BadZipFile, OSError):
            raise ForgeError("not a readable zip archive") from None
        with archive as zf:
            infos = zf.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES or sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
                raise ForgeError("archive is implausibly large")
            for info in infos:
                # some authors zip on Windows with backslash separators; treat them as folders
                name = info.filename.replace("\\", "/")
                norm = os.path.normpath(name)
                if name.startswith("/") or norm == ".." or norm.startswith("../") or ":" in name or "\x00" in name:
                    raise ForgeError(f"archive has an unsafe path: {info.filename!r}")
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ForgeError(f"archive contains a symbolic link: {info.filename!r}")
                target = scratch / norm
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        folders = sorted(p.name for p in scratch.iterdir() if p.is_dir())
        if not folders:
            raise ForgeError("archive has no addon folders")
        lo, hi = cfg["interface_range"]
        forever = False
        for folder in folders:
            for toc in (scratch / folder).glob("*.toc"):
                for line in toc.read_text(errors="replace").splitlines():
                    m = re.match(r"\s*##\s*Interface[^:]*:\s*(.+)", line, re.I)
                    if m and any(lo <= int(n) <= hi for n in re.findall(r"\d+", m.group(1))):
                        forever = True
        if not forever:
            raise ForgeError("no .toc in this file lists a WoW Forever interface version")
        return scratch, folders
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise


def owners(state):
    return {folder: slug for slug, s in state.items() if isinstance(s, dict) for folder in s.get("folders", [])}


def install(cfg, state, slug, file, zip_path, sha1, note=""):
    """Swap the new version in. Either every game folder ends up on the new version, or none does."""
    scratch, folders = unpack(zip_path, cfg)
    owned = owners(state)
    old = state.get(slug, {})
    targets = [d for d in addon_dirs(cfg) if d.is_dir()]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved, created = [], []
    try:
        if not targets:
            raise ForgeError("none of the addon_dirs in forge.json exist")
        for addon_dir in targets:
            for folder in folders:
                if (addon_dir / folder).exists() and owned.get(folder, slug) != slug:
                    raise ForgeError(f"folder {folder} belongs to {owned[folder]}")
        for addon_dir in targets:
            backup = paths()["backups"] / slug / stamp / addon_dir.parent.parent.name
            for folder in sorted(set(folders) | set(old.get("folders", []))):
                target = addon_dir / folder
                if target.exists():
                    backup.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(backup / folder))
                    moved.append((backup / folder, target))
                if folder in folders:
                    created.append(target)
                    shutil.copytree(scratch / folder, target)
    except BaseException:
        for target in reversed(created):
            shutil.rmtree(target, ignore_errors=True)
        for backup, target in reversed(moved):
            if not target.exists():
                shutil.move(str(backup), str(target))
        shutil.rmtree(paths()["backups"] / slug / stamp, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    slug_backups = paths()["backups"] / slug
    versions = sorted(slug_backups.glob("*")) if slug_backups.exists() else []
    for stale in versions[: max(0, len(versions) - cfg.get("keep_backups", 3))]:
        shutil.rmtree(stale, ignore_errors=True)
    keep = ("mod_id", "name", "website", "dependency_of", "source", "gh_etag", "gh_files")
    state[slug] = {
        **{k: v for k, v in old.items() if k in keep},
        "file_id": file["id"], "file_name": file["fileName"], "version": file.get("displayName") or file["fileName"],
        "release_type": CHANNEL_NAME.get(file["releaseType"], "?"), "file_date": file.get("fileDate", ""),
        "sha1": sha1, "folders": folders, "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "previous_file_id": old.get("file_id"), "note": note or "",
    }


def folders_present(cfg, entry):
    if not entry.get("folders"):
        return False
    return all((d / f).is_dir() for d in addon_dirs(cfg) if d.is_dir() for f in entry["folders"])


def find_download(cfg, file):
    """A zip the user fetched by hand for exactly this upstream file: same hash, in an import folder."""
    if not sha1_values(file):
        return None
    stem = Path(file["fileName"]).stem
    for folder in cfg.get("import_dirs", []):
        folder = Path(folder).expanduser()
        if not folder.is_dir():
            continue
        # browsers rename a second download to "name (1).zip"
        for candidate in sorted(folder.glob(f"{glob_escape(stem)}*.zip"), key=lambda p: p.stat().st_mtime, reverse=True):
            if verify(candidate, file)[0]:
                return candidate
    return None


def glob_escape(text):
    return re.sub(r"([\[\]*?])", r"[\1]", text)


# ---------------------------------------------------------------------------
# The machine around it
# ---------------------------------------------------------------------------

def game_running():
    """True when a WoW client process exists. Reads /proc directly, so it needs no extra tools."""
    for pid in Path("/proc").glob("[0-9]*"):
        try:
            cmdline = (pid / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if GAME_PROCESS.search(cmdline):
            return True
    return False


def notify(text):
    try:
        subprocess.run(["notify-send", "-a", "Yeeterson Forge", "Yeeterson Forge", text], timeout=5, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def file_page(entry, file):
    site = entry.get("website")
    return f"{site.rstrip('/')}/files/{file['id']}" if site else "its CurseForge page"


def cmd_update(cfg, state, force=False, check_only=False):
    state_path = paths()["state.json"]
    if not check_only and game_running() and not force:
        log("WoW is running, so nothing was changed. Updates apply the next time it is closed.")
        return 0
    # A new client folder (the launch client, for example) is never guessed at: say so once per folder.
    known = {str(d.parent.parent) for d in addon_dirs(cfg)}
    root = Path(cfg.get("wow_root") or "").expanduser()
    for flavor in sorted(root.glob("_*_")) if cfg.get("wow_root") and root.is_dir() else []:
        if str(flavor) not in known and str(flavor) not in state.get("_announced", []):
            state.setdefault("_announced", []).append(str(flavor))
            log(f"New game folder {flavor.name} is not managed yet. Add its Interface/AddOns path to addon_dirs in forge.json.")
            if not check_only:
                notify(f"New WoW folder {flavor.name} found. Addons are not installed there until it is added to forge.json.")
    queue = [dict(a) for a in cfg["addons"]]
    seen, changed, problems, manual, rows = set(), [], [], [], []
    while queue:
        addon = queue.pop(0)
        slug = addon["slug"]
        if slug in seen:
            continue
        seen.add(slug)
        entry = state.setdefault(slug, {})
        name = entry.get("name") or slug
        try:
            repo = addon.get("github")
            channel = addon.get("channel", "release")
            if repo:
                entry.update(name=addon.get("name") or repo.split("/")[-1].capitalize(), source=f"github:{repo}")
                file, fallback = choose(github_files(repo, entry), channel)
            else:
                if "mod_id" not in entry or "website" not in entry:
                    mod = resolve_mod(cfg, slug)
                    entry.update(mod_id=mod["id"], name=mod["name"], website=(mod.get("links") or {}).get("websiteUrl") or "")
                file, fallback = choose(forever_files(cfg, entry["mod_id"]), channel)
            name = entry["name"]
            if addon.get("dependency_of"):
                entry["dependency_of"] = addon["dependency_of"]
            if not file:
                raise ForgeError("no release zip on GitHub" if repo else "no WoW Forever file on CurseForge")
            for dep in file.get("dependencies", []):
                if dep.get("relationType") == 3:
                    dep_mod = cf(cfg, f"/mods/{dep['modId']}")
                    queue.append({"slug": dep_mod["slug"], "dependency_of": slug, "channel": channel})
            installed = entry.get("version", "not installed") if entry.get("folders") else "not installed"
            current = entry.get("file_id") == file["id"] and folders_present(cfg, entry)
            held = bool(entry.get("hold")) and folders_present(cfg, entry)
            newer = file.get("displayName") or file["fileName"]
            suffix = f" ({fallback} build, no release yet)" if fallback else ""
            if current:
                rows.append((name, installed, "up to date" + suffix))
                continue
            if held:
                rows.append((name, installed, "held by rollback"))
                continue
            downloaded = None
            if not file.get("downloadUrl"):
                downloaded = find_download(cfg, file)
                if not downloaded:
                    rows.append((name, installed, f"{newer}: download it by hand, the author blocks other apps"))
                    manual.append((slug, name, file))
                    continue
            if check_only:
                rows.append((name, installed, f"update: {newer}{suffix}" + (" (found in your downloads)" if downloaded else "")))
                continue
            if downloaded:
                zip_path, sha1 = downloaded, verify(downloaded, file)[1]
            else:
                zip_path, sha1 = fetch(file)
            try:
                install(cfg, state, slug, file, zip_path, sha1, f"{fallback} build" if fallback else "")
            finally:
                if not downloaded:
                    Path(zip_path).unlink(missing_ok=True)
            state[slug].pop("hold", None)
            was = installed if installed != "not installed" else None
            changed.append(f"{name}: {was + ' -> ' if was else 'installed '}{state[slug]['version']}")
            rows.append((name, state[slug]["version"], "updated" + suffix))
            log(f"  {changed[-1]}")
        except ForgeError as err:
            problems.append(f"{name}: {err}")
            rows.append((name, entry.get("version", "not installed") if entry.get("folders") else "not installed", f"PROBLEM: {err}"))
            if not check_only:
                log(f"  PROBLEM {name}: {err}")
        except Exception as err:  # one bad addon never blocks the others
            problems.append(f"{name}: unexpected {type(err).__name__}: {err}")
            rows.append((name, "?", f"PROBLEM: {err}"))
            if not check_only:
                log(f"  PROBLEM {name}: unexpected {type(err).__name__}: {err}")
        finally:
            if not entry and slug in state and not state[slug]:
                del state[slug]
            write_json(state_path, state)
    if check_only:
        width = max((len(r[0]) for r in rows), default=10)
        for row_name, version, status in rows:
            print(f"{row_name:<{width}}  {version[:38]:<38}  {status}")
        for slug, manual_name, file in manual:
            print(f"\n{manual_name}: open {file_page(state[slug], file)}\n  download {file['fileName']} and leave it in your Downloads folder. The forge installs it on its next run.")
        return 1 if problems else 0
    log(f"update done: {len(changed)} changed, {len(rows) - len(changed) - len(problems) - len(manual)} unchanged, "
        f"{len(manual)} waiting for a manual download, {len(problems)} problems")
    if changed:
        notify("Updated: " + "; ".join(c.split(":")[0] for c in changed))
    # Tell the user about each manual download and each problem once, not every ten minutes.
    told = state.setdefault("_told", {})
    for slug, manual_name, file in manual:
        if told.get(slug) != file["id"]:
            told[slug] = file["id"]
            log(f"  {manual_name} {file.get('displayName') or file['fileName']} is only offered on CurseForge by its author: {file_page(state[slug], file)}")
            notify(f"{manual_name} needs a manual download. Get it from CurseForge and leave it in Downloads; the forge does the rest.")
    signature = sorted(problems)
    if signature != state.get("_problems", []):
        state["_problems"] = signature
        if problems:
            notify("Needs a look: " + "; ".join(problems)[:200])
    write_json(state_path, state)
    return 1 if problems else 0


def cmd_add(cfg, state, spec, channel=None, force=False):
    new = {"slug": spec}
    if spec.lower().startswith("github:"):
        repo = spec[7:].strip("/")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise SystemExit("Use github:<owner>/<repo>")
        new = {"slug": repo.split("/")[-1].lower(), "github": repo}
    elif not re.fullmatch(r"[a-z0-9-]+", spec):
        raise SystemExit("A slug is the last part of the addon's CurseForge URL, for example: questie")
    if channel:
        if channel not in CHANNELS:
            raise SystemExit("Channel is release, beta or alpha")
        new["channel"] = channel
    existing = next((a for a in cfg["addons"] if a["slug"] == new["slug"]), None)
    if existing:
        existing.update(new)
    else:
        cfg["addons"].append(new)
    write_json(paths()["forge.json"], cfg)
    state.get(new["slug"], {}).pop("hold", None)
    return cmd_update(cfg, state, force=force)


def cmd_remove(cfg, state, slug):
    if game_running():
        raise SystemExit("WoW is running. Close it first.")
    entry = state.pop(slug, None)
    if isinstance(entry, dict):
        for addon_dir in addon_dirs(cfg):
            for folder in entry.get("folders", []):
                shutil.rmtree(addon_dir / folder, ignore_errors=True)
    cfg["addons"] = [a for a in cfg["addons"] if a["slug"] != slug]
    state.get("_told", {}).pop(slug, None)
    write_json(paths()["forge.json"], cfg)
    write_json(paths()["state.json"], state)
    log(f"removed {slug}")
    return 0


def cmd_rollback(cfg, state, slug):
    if game_running():
        raise SystemExit("WoW is running. Close it first.")
    slug_backups = paths()["backups"] / slug
    versions = sorted(slug_backups.glob("*")) if slug_backups.exists() else []
    if not versions or not isinstance(state.get(slug), dict):
        raise SystemExit(f"no backup for {slug}")
    last = versions[-1]
    restored = None
    for addon_dir in addon_dirs(cfg):
        source = last / addon_dir.parent.parent.name
        if not source.is_dir():
            continue
        for folder in state[slug].get("folders", []):
            shutil.rmtree(addon_dir / folder, ignore_errors=True)
        for folder in source.iterdir():
            shutil.copytree(folder, addon_dir / folder.name)
        restored = sorted(p.name for p in source.iterdir())
    if restored is None:
        raise SystemExit(f"the newest backup of {slug} has nothing for these game folders")
    shutil.rmtree(last, ignore_errors=True)  # it is live again, so it is no longer "the previous version"
    state[slug].update(folders=restored, hold=True, file_id=state[slug].get("previous_file_id"),
                       version=f"rolled back from {state[slug].get('version', '')}")
    write_json(paths()["state.json"], state)
    log(f"rolled back {slug}. It stays on this version until you run: yforge add {state[slug].get('source') or slug}")
    return 0


def cmd_import(cfg, state, zips, force=False):
    """Install zips the user downloaded. Each must match, byte for byte, a file the addon's
    author published for WoW Forever; anything else is refused."""
    if game_running() and not force:
        raise SystemExit("WoW is running. Close it first, or the import waits for nothing.")
    candidates = []
    for addon in cfg["addons"]:
        entry = state.get(addon["slug"], {})
        try:
            if addon.get("github"):
                files = github_files(addon["github"], state.setdefault(addon["slug"], {}))
            elif "mod_id" in entry:
                files = forever_files(cfg, entry["mod_id"])
            else:
                mod = resolve_mod(cfg, addon["slug"])
                state.setdefault(addon["slug"], {}).update(mod_id=mod["id"], name=mod["name"], website=(mod.get("links") or {}).get("websiteUrl") or "")
                files = forever_files(cfg, mod["id"])
        except ForgeError as err:
            log(f"  could not list files for {addon['slug']}: {err}")
            continue
        candidates += [(addon["slug"], f) for f in files if sha1_values(f) or f.get("sha256")]
    failures = 0
    for zip_arg in zips:
        zip_path = Path(zip_arg).expanduser()
        if not zip_path.is_file():
            log(f"  {zip_arg}: no such file")
            failures += 1
            continue
        match = next(((slug, f) for slug, f in candidates if verify(zip_path, f)[0]), None)
        if not match:
            log(f"  {zip_path.name}: not a WoW Forever file of any addon you manage, so it was not installed")
            failures += 1
            continue
        slug, file = match
        try:
            install(cfg, state, slug, file, zip_path, verify(zip_path, file)[1], "imported from a manual download")
            state[slug].pop("hold", None)
            log(f"  {state[slug].get('name', slug)}: installed {state[slug]['version']} from {zip_path.name}")
        except ForgeError as err:
            log(f"  {zip_path.name}: {err}")
            failures += 1
        finally:
            write_json(paths()["state.json"], state)
    return 1 if failures else 0


WOW_SEARCH = (
    "Games/*/drive_c/Program Files (x86)/World of Warcraft",
    "Games/*/drive_c/Program Files/World of Warcraft",
    ".wine/drive_c/Program Files (x86)/World of Warcraft",
    ".var/app/com.usebottles.bottles/data/bottles/bottles/*/drive_c/Program Files (x86)/World of Warcraft",
    ".local/share/bottles/bottles/*/drive_c/Program Files (x86)/World of Warcraft",
    ".local/share/Steam/steamapps/compatdata/*/pfx/drive_c/Program Files (x86)/World of Warcraft",
    ".var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/compatdata/*/pfx/drive_c/Program Files (x86)/World of Warcraft",
    ".local/share/lutris/runners/wine/*/drive_c/Program Files (x86)/World of Warcraft",
)


def find_wow(base=None):
    base = Path(base) if base else Path.home()
    found = []
    for pattern in WOW_SEARCH:
        found += [p for p in base.glob(pattern) if p.is_dir()]
    return sorted(set(found))


def cmd_init(path=None, flavor=None):
    config_path = paths()["forge.json"]
    if config_path.exists():
        raise SystemExit(f"{config_path} already exists. Edit it, or delete it to start over.")
    roots = [Path(path).expanduser().resolve()] if path else find_wow()
    roots = [r for r in roots if r.is_dir()]
    if not roots:
        raise SystemExit('Could not find the game. Run: yforge init "/path/to/World of Warcraft"')
    if len(roots) > 1:
        print("Found more than one install. Pick one:")
        for root in roots:
            print(f'  yforge init "{root}"')
        return 2
    root = roots[0]
    flavors = sorted(p.name for p in root.glob("_*_") if p.is_dir())
    if flavor and flavor not in flavors:
        raise SystemExit(f"{root} has no folder {flavor}. It has: {', '.join(flavors) or 'none'}")
    if not flavor and len(flavors) != 1:
        print(f"{root} has these game folders: {', '.join(flavors) or 'none'}")
        print("Say which one is WoW Forever, for example: yforge init --flavor _classic_beta_")
        return 2
    target = root / (flavor or flavors[0]) / "Interface" / "AddOns"
    target.mkdir(parents=True, exist_ok=True)
    cfg = {**DEFAULT_CONFIG, "wow_root": str(root), "addon_dirs": [str(target)], "addons": []}
    write_json(config_path, cfg)
    print(f"Wrote {config_path}")
    print(f"Addons go to {target}")
    if not curseforge_key(cfg)[0]:
        print("\nCurseForge addons need your own free API key:")
        print("  1. Sign in at https://console.curseforge.com and copy the key under API keys.")
        print(f"  2. Save it as the only line of {paths()['curseforge.key']}")
        print("Addons from GitHub releases work without a key.")
    print("\nNext: yforge add questie     then: yforge timer on")
    return 0


UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "systemd" / "user"


def unit_files():
    script = Path(__file__).resolve()
    environment = f"Environment=YFORGE_HOME={HOME}\n" if os.environ.get("YFORGE_HOME") else ""
    service = (
        "[Unit]\nDescription=Yeeterson Forge: keep WoW Forever addons current\nAfter=network-online.target\n\n"
        f"[Service]\nType=oneshot\n{environment}ExecStart={sys.executable} {script} update --quiet\nNice=10\n"
    )
    timer = (
        "[Unit]\nDescription=Yeeterson Forge: check for WoW Forever addon updates every 10 minutes\n\n"
        "[Timer]\nOnBootSec=2min\nOnUnitActiveSec=10min\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def cmd_timer(mode):
    if mode not in ("on", "off"):
        raise SystemExit("Use: yforge timer on   or   yforge timer off")
    service_path, timer_path = UNIT_DIR / "yeeterson-forge.service", UNIT_DIR / "yeeterson-forge.timer"
    systemctl = lambda *args: subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    if mode == "off":
        systemctl("disable", "--now", "yeeterson-forge.timer")
        service_path.unlink(missing_ok=True)
        timer_path.unlink(missing_ok=True)
        systemctl("daemon-reload")
        print("Timer removed. Addons now update only when you run: yforge update")
        return 0
    service, timer = unit_files()
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    service_path.write_text(service)
    timer_path.write_text(timer)
    systemctl("daemon-reload")
    result = systemctl("enable", "--now", "yeeterson-forge.timer")
    if result.returncode != 0:
        raise SystemExit(f"systemd refused the timer: {result.stderr.strip()}")
    print("Timer on. The forge checks two minutes after login and every 10 minutes after that.")
    return 0


def cmd_doctor(cfg):
    key, source = curseforge_key(cfg)
    print(f"Yeeterson Forge {VERSION}")
    print(f"home         {HOME}")
    for d in addon_dirs(cfg):
        print(f"addons       {d}  {'ok' if d.is_dir() else 'MISSING'}")
    print(f"CurseForge   {'key from ' + source if key else 'no key (GitHub addons still work)'}")
    if key:
        try:
            cf(cfg, "/games/1")
            print("             the key works")
        except ForgeError as err:
            print(f"             {err}")
    print(f"game         {'running' if game_running() else 'not running'}")
    print(f"managing     {len(cfg['addons'])} addons")
    return 0


def main(argv=None):
    global QUIET
    argv = list(sys.argv[1:] if argv is None else argv)
    flags = {a for a in argv if a.startswith("--") and a not in ("--flavor",)}
    flavor = None
    if "--flavor" in argv:
        i = argv.index("--flavor")
        if i + 1 >= len(argv):
            raise SystemExit("--flavor needs a folder name, for example _classic_beta_")
        flavor = argv[i + 1]
        del argv[i:i + 2]
    args = [a for a in argv if not a.startswith("--")]
    QUIET = "--quiet" in flags
    force = "--force" in flags
    command = args[0] if args else "status"
    if "--version" in flags or command == "version":
        print(VERSION)
        return 0
    if "--help" in flags or command in ("help", "-h"):
        print(__doc__)
        return 0
    if command == "init":
        return cmd_init(args[1] if len(args) > 1 else None, flavor)
    if command == "timer":
        return cmd_timer(args[1] if len(args) > 1 else "")
    cfg = load_config()
    state = read_json(paths()["state.json"], {})
    if command == "update":
        return cmd_update(cfg, state, force=force)
    if command == "status":
        return cmd_update(cfg, state, check_only=True)
    if command == "add" and len(args) >= 2:
        return cmd_add(cfg, state, args[1], args[2] if len(args) > 2 else None, force=force)
    if command == "remove" and len(args) == 2:
        return cmd_remove(cfg, state, args[1])
    if command == "rollback" and len(args) == 2:
        return cmd_rollback(cfg, state, args[1])
    if command == "import" and len(args) >= 2:
        return cmd_import(cfg, state, args[1:], force=force)
    if command == "doctor":
        return cmd_doctor(cfg)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
