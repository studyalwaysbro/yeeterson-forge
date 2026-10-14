"""Tests for Yeeterson Forge. Run from the project root: python3 -m unittest discover -s tests -v

Nothing here touches the network or a real game folder: CurseForge, GitHub and the downloads
are faked in memory, and every test works in its own temporary directory.
"""
import contextlib
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import forge  # noqa: E402

FOREVER_TOC = "## Interface: 16001\n## Title: {name}\n{name}.lua\n"
RETAIL_TOC = "## Interface: 120100\n## Title: {name}\n"


def make_zip(folders, toc=FOREVER_TOC, extra=None, marker="v1"):
    """A zip with one or more addon folders. marker makes two builds differ in content."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for folder in folders:
            zf.writestr(f"{folder}/{folder}.toc", toc.format(name=folder))
            zf.writestr(f"{folder}/{folder}.lua", f"-- {folder} {marker}\n")
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buf.getvalue()


def sha1(data):
    return hashlib.sha1(data).hexdigest()


class FakeWorld:
    """In-memory CurseForge and GitHub."""

    def __init__(self):
        self.mods = {}        # slug -> {id, name, slug, links, files: [...]}
        self.repos = {}       # "owner/repo" -> [release, ...]
        self.blobs = {}       # url -> bytes
        self.calls = []       # every URL requested
        self.headers = []     # headers of every JSON request
        self.offline = False
        self.next_id = 1000

    def add_mod(self, slug, name=None, distributable=True):
        self.next_id += 1
        self.mods[slug] = {"id": self.next_id, "slug": slug, "name": name or slug.title(),
                           "links": {"websiteUrl": f"https://www.curseforge.com/wow/addons/{slug}"},
                           "files": [], "distributable": distributable}
        return self.mods[slug]

    def add_file(self, slug, data, version, release_type=1, date=None, deps=(), bad_hash=False, with_url=None):
        mod = self.mods[slug]
        self.next_id += 1
        fid = self.next_id
        name = f"{slug}-{version}.zip"
        url = f"https://edge.forgecdn.net/files/{fid}/{name}"
        allowed = mod["distributable"] if with_url is None else with_url
        self.blobs[url] = data
        record = {"id": fid, "fileName": name, "displayName": version, "releaseType": release_type,
                  "fileDate": date or f"2026-10-{len(mod['files']) + 1:02d}T00:00:00Z", "isAvailable": True,
                  "downloadUrl": url if allowed else None,
                  "hashes": [{"algo": 1, "value": "0" * 40 if bad_hash else sha1(data)}],
                  "dependencies": [{"modId": self.mods[d]["id"], "relationType": 3} for d in deps]}
        mod["files"].append(record)
        return record

    def add_release(self, repo, tag, data, prerelease=False, digest=True, date=None, asset_name=None):
        self.next_id += 1
        name = asset_name or f"{repo.split('/')[-1]}-{tag}.zip"
        url = f"https://github.com/{repo}/releases/download/{tag}/{name}"
        self.blobs[url] = data
        self.repos.setdefault(repo, []).append({
            "tag_name": tag, "prerelease": prerelease, "draft": False,
            "published_at": date or f"2026-10-{len(self.repos.get(repo, [])) + 1:02d}T00:00:00Z",
            "assets": [{"id": self.next_id, "name": name, "browser_download_url": url,
                        "digest": ("sha256:" + hashlib.sha256(data).hexdigest()) if digest else None}],
        })

    def etag(self, repo):
        return '"' + sha1(json.dumps(self.repos[repo], sort_keys=True).encode()) + '"'

    def http_json(self, url, headers):
        self.calls.append(url)
        self.headers.append(headers)
        if self.offline:
            raise forge.ForgeError("could not reach the network")
        if url.startswith(forge.API):
            if headers.get("x-api-key") != "good-key":
                raise forge.ForgeError("CurseForge refused the API key (check it with: yforge doctor)")
            path = url[len(forge.API):]
            if path.startswith("/mods/search"):
                slug = path.split("slug=")[1]
                return {"data": [self.public(m) for m in self.mods.values() if m["slug"].startswith(slug)]}, {}
            if path.startswith("/games/"):
                return {"data": {"id": 1}}, {}
            parts = path.split("?")[0].strip("/").split("/")
            mod = next(m for m in self.mods.values() if str(m["id"]) == parts[1])
            if len(parts) == 3 and parts[2] == "files":
                assert "gameVersionTypeId=88568" in path, "must only ask for WoW Forever files"
                return {"data": list(mod["files"])}, {}
            return {"data": self.public(mod)}, {}
        if url.startswith(forge.GITHUB):
            repo = url[len(forge.GITHUB) + len("/repos/"):].split("/releases")[0]
            if repo not in self.repos:
                raise forge.ForgeError("api.github.com answered 404")
            if headers.get("if-none-match") == self.etag(repo):
                return None, {"ETag": self.etag(repo)}
            releases = json.loads(json.dumps(self.repos[repo]))
            return (releases[-1] if url.endswith("/releases/latest") else releases), {"ETag": self.etag(repo)}
        raise AssertionError(f"the forge contacted an unexpected host: {url}")

    @staticmethod
    def public(mod):
        return {k: v for k, v in mod.items() if k not in ("files", "distributable")}

    def http_download(self, url, dest):
        self.calls.append(url)
        if self.offline or url not in self.blobs:
            raise forge.ForgeError("download failed")
        Path(dest).write_bytes(self.blobs[url])


class ForgeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="yforge-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = self.tmp / "home"
        self.game = self.tmp / "World of Warcraft"
        self.addons = self.game / "_classic_beta_" / "Interface" / "AddOns"
        self.addons.mkdir(parents=True)
        self.downloads = self.tmp / "Downloads"
        self.downloads.mkdir()
        self.world = FakeWorld()
        self.notes = []
        self.running = False
        saved = {name: getattr(forge, name) for name in ("HOME", "http_json", "http_download", "game_running", "notify", "QUIET")}
        self.addCleanup(lambda: [setattr(forge, k, v) for k, v in saved.items()])
        forge.HOME = self.home
        forge.http_json = self.world.http_json
        forge.http_download = self.world.http_download
        forge.game_running = lambda: self.running
        forge.notify = self.notes.append
        forge.QUIET = True
        env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(env)))
        os.environ["CURSEFORGE_API_KEY"] = "good-key"
        os.environ.pop("GITHUB_TOKEN", None)
        os.environ.pop("YFORGE_HOME", None)
        self.write_config([])

    def write_config(self, addons, **extra):
        cfg = {**forge.DEFAULT_CONFIG, "wow_root": str(self.game), "addon_dirs": [str(self.addons)],
               "import_dirs": [str(self.downloads)], "self_update": False, "addons": addons, **extra}
        forge.write_json(self.home / "forge.json", cfg)

    def run_forge(self, *args):
        self.out = io.StringIO()
        with contextlib.redirect_stdout(self.out):
            return forge.main([*args, "--quiet"])

    def state(self):
        return forge.read_json(self.home / "state.json", {})

    def lua(self, folder, root=None):
        return ((root or self.addons) / folder / f"{folder}.lua").read_text()

    def log_text(self):
        path = self.home / "logs" / "forge.log"
        return path.read_text() if path.exists() else ""


class InstallAndUpdate(ForgeCase):
    def test_installs_a_curseforge_addon_and_records_it(self):
        self.world.add_mod("questie", "Questie")
        data = make_zip(["Questie", "QuestieDB"])
        self.world.add_file("questie", data, "v12")
        self.write_config([{"slug": "questie"}])
        self.assertEqual(self.run_forge("update"), 0)
        self.assertEqual(self.lua("Questie"), "-- Questie v1\n")
        self.assertTrue((self.addons / "QuestieDB").is_dir())
        entry = self.state()["questie"]
        self.assertEqual((entry["version"], entry["folders"], entry["sha1"]), ("v12", ["Questie", "QuestieDB"], sha1(data)))
        self.assertEqual(entry["website"], "https://www.curseforge.com/wow/addons/questie")
        self.assertFalse(list((self.home / "cache").glob("*")), "scratch files are cleaned up")
        self.assertIn("Updated: Questie", self.notes)

    def test_update_replaces_backs_up_and_drops_folders_the_new_version_lost(self):
        self.world.add_mod("questie")
        self.world.add_file("questie", make_zip(["Questie", "QuestieOld"], marker="v1"), "v1")
        self.write_config([{"slug": "questie"}])
        self.run_forge("update")
        self.world.add_file("questie", make_zip(["Questie"], marker="v2"), "v2")
        self.assertEqual(self.run_forge("update"), 0)
        self.assertEqual(self.lua("Questie"), "-- Questie v2\n")
        self.assertFalse((self.addons / "QuestieOld").exists())
        backup = next((self.home / "backups" / "questie").iterdir()) / "_classic_beta_"
        self.assertEqual(self.lua("Questie", backup), "-- Questie v1\n")
        self.assertTrue((backup / "QuestieOld").is_dir())
        self.assertEqual(self.state()["questie"]["folders"], ["Questie"])

    def test_second_run_is_a_no_op(self):
        self.world.add_mod("questie")
        self.world.add_file("questie", make_zip(["Questie"]), "v1")
        self.write_config([{"slug": "questie"}])
        self.run_forge("update")
        stamp = (self.addons / "Questie" / "Questie.lua").stat().st_mtime_ns
        downloads = len([c for c in self.world.calls if "forgecdn" in c])
        self.notes.clear()
        self.assertEqual(self.run_forge("update"), 0)
        self.assertEqual((self.addons / "Questie" / "Questie.lua").stat().st_mtime_ns, stamp)
        self.assertEqual(len([c for c in self.world.calls if "forgecdn" in c]), downloads)
        self.assertEqual(self.notes, [])

    def test_keeps_only_the_configured_number_of_backups(self):
        self.world.add_mod("questie")
        self.write_config([{"slug": "questie"}], keep_backups=2)
        import time as real_time
        stamps = iter(f"20261007-00000{i}" for i in range(9))
        original = forge.time.strftime
        forge.time.strftime = lambda fmt, *a: next(stamps) if fmt == "%Y%m%d-%H%M%S" else original(fmt, *a)
        self.addCleanup(setattr, forge.time, "strftime", original)
        for n in range(1, 6):
            self.world.add_file("questie", make_zip(["Questie"], marker=f"v{n}"), f"v{n}")
            self.run_forge("update")
        versions = sorted(p.name for p in (self.home / "backups" / "questie").iterdir())
        self.assertEqual(len(versions), 2)
        newest = self.home / "backups" / "questie" / versions[-1] / "_classic_beta_"
        self.assertEqual(self.lua("Questie", newest), "-- Questie v4\n")
        del real_time

    def test_status_changes_nothing(self):
        self.world.add_mod("questie")
        self.world.add_file("questie", make_zip(["Questie"]), "v1")
        self.write_config([{"slug": "questie"}])
        self.assertEqual(self.run_forge("status"), 0)
        self.assertFalse((self.addons / "Questie").exists())
        self.assertNotIn("folders", self.state().get("questie", {}))

    def test_game_running_blocks_changes_unless_forced(self):
        self.world.add_mod("questie")
        self.world.add_file("questie", make_zip(["Questie"]), "v1")
        self.write_config([{"slug": "questie"}])
        self.running = True
        self.assertEqual(self.run_forge("update"), 0)
        self.assertFalse((self.addons / "Questie").exists())
        self.assertEqual(self.world.calls, [], "it does not even ask the network")
        self.assertEqual(self.run_forge("update", "--force"), 0)
        self.assertTrue((self.addons / "Questie").exists())

    def test_installs_into_every_game_folder(self):
        second = self.game / "_forever_" / "Interface" / "AddOns"
        second.mkdir(parents=True)
        self.world.add_mod("questie")
        self.world.add_file("questie", make_zip(["Questie"]), "v1")
        self.write_config([{"slug": "questie"}], addon_dirs=[str(self.addons), str(second), str(self.tmp / "gone")])
        self.assertEqual(self.run_forge("update"), 0)
        self.assertTrue((self.addons / "Questie").is_dir() and (second / "Questie").is_dir())

    def test_required_dependencies_are_installed(self):
        self.world.add_mod("syndicator")
        self.world.add_file("syndicator", make_zip(["Syndicator"]), "s1")
        self.world.add_mod("baganator")
        self.world.add_file("baganator", make_zip(["Baganator"]), "b1", deps=["syndicator"])
        self.write_config([{"slug": "baganator"}])
        self.assertEqual(self.run_forge("update"), 0)
        self.assertTrue((self.addons / "Syndicator").is_dir())
        self.assertEqual(self.state()["syndicator"]["dependency_of"], "baganator")

    def test_addons_the_forge_did_not_install_are_never_touched(self):
        mine = self.addons / "TorchBinds"
        mine.mkdir()
        (mine / "TorchBinds.lua").write_text("mine")
        self.world.add_mod("questie")
        self.world.add_file("questie", make_zip(["Questie"]), "v1")
        self.write_config([{"slug": "questie"}])
        self.run_forge("update")
        self.run_forge("remove", "questie")
        self.assertEqual((mine / "TorchBinds.lua").read_text(), "mine")
        self.assertFalse((self.addons / "Questie").exists())


class Stability(ForgeCase):
    def setUp(self):
        super().setUp()
        self.world.add_mod("trainer")

    def test_release_is_preferred_over_a_newer_beta(self):
        self.world.add_file("trainer", make_zip(["Trainer"], marker="release"), "1.0", release_type=1, date="2026-10-01T00:00:00Z")
        self.world.add_file("trainer", make_zip(["Trainer"], marker="beta"), "1.1-beta", release_type=2, date="2026-10-05T00:00:00Z")
        self.write_config([{"slug": "trainer"}])
        self.run_forge("update")
        self.assertEqual(self.lua("Trainer"), "-- Trainer release\n")

    def test_beta_channel_takes_the_newest_of_release_or_beta(self):
        self.world.add_file("trainer", make_zip(["Trainer"], marker="release"), "1.0", release_type=1, date="2026-10-01T00:00:00Z")
        self.world.add_file("trainer", make_zip(["Trainer"], marker="beta"), "1.1-beta", release_type=2, date="2026-10-05T00:00:00Z")
        self.world.add_file("trainer", make_zip(["Trainer"], marker="alpha"), "1.2-alpha", release_type=3, date="2026-10-06T00:00:00Z")
        self.write_config([{"slug": "trainer", "channel": "beta"}])
        self.run_forge("update")
        self.assertEqual(self.lua("Trainer"), "-- Trainer beta\n")

    def test_falls_back_to_beta_when_there_is_no_release_and_says_so(self):
        self.world.add_file("trainer", make_zip(["Trainer"], marker="beta"), "1.1-beta", release_type=2)
        self.write_config([{"slug": "trainer"}])
        self.assertEqual(self.run_forge("update"), 0)
        self.assertEqual(self.lua("Trainer"), "-- Trainer beta\n")
        self.assertEqual(self.state()["trainer"]["note"], "beta build")

    def test_no_forever_file_is_a_problem_not_a_crash(self):
        self.write_config([{"slug": "trainer"}])
        self.assertEqual(self.run_forge("update"), 1)
        self.assertIn("no WoW Forever file on CurseForge", self.log_text())


class Safety(ForgeCase):
    def setUp(self):
        super().setUp()
        self.world.add_mod("evil")
        self.write_config([{"slug": "evil"}])

    def refused(self, data, reason):
        self.world.add_file("evil", data, "1")
        self.assertEqual(self.run_forge("update"), 1)
        self.assertIn(reason, self.log_text())
        self.assertEqual(sorted(p.name for p in self.addons.iterdir()), [])
        self.assertFalse(list((self.home / "cache").glob("unpack-*")), "scratch folder removed after a refusal")

    def test_hash_mismatch_is_refused(self):
        self.world.add_file("evil", make_zip(["Evil"]), "1", bad_hash=True)
        self.assertEqual(self.run_forge("update"), 1)
        self.assertIn("failed its hash check", self.log_text())
        self.assertFalse((self.addons / "Evil").exists())
        self.assertFalse(list((self.home / "cache").glob("*.zip")))

    def test_retail_only_addon_is_refused(self):
        self.refused(make_zip(["Evil"], toc=RETAIL_TOC), "no .toc in this file lists a WoW Forever interface version")

    def test_multi_flavor_toc_with_a_forever_version_is_accepted(self):
        self.world.add_file("evil", make_zip(["Evil"], toc="## Interface: 120100, 16001, 11508\n"), "1")
        self.assertEqual(self.run_forge("update"), 0)
        self.assertTrue((self.addons / "Evil").is_dir())

    def test_path_traversal_is_refused(self):
        self.refused(make_zip(["Evil"], extra={"../../escape.txt": "x"}), "unsafe path")
        self.assertFalse((self.tmp / "escape.txt").exists())
        self.assertFalse((self.home / "escape.txt").exists())

    def test_absolute_and_drive_paths_are_refused(self):
        self.refused(make_zip(["Evil"], extra={"/etc/evil": "x"}), "unsafe path")

    def test_drive_letter_path_is_refused(self):
        self.refused(make_zip(["Evil"], extra={"C:/Windows/evil": "x"}), "unsafe path")

    def test_symlink_entry_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Evil/Evil.toc", FOREVER_TOC.format(name="Evil"))
            link = zipfile.ZipInfo("Evil/link")
            link.external_attr = (0o120777 << 16)
            zf.writestr(link, "/etc/passwd")
        self.refused(buf.getvalue(), "symbolic link")

    def test_garbage_instead_of_a_zip_is_refused(self):
        self.refused(b"this is not a zip", "not a readable zip archive")

    def test_zip_without_folders_is_refused(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("loose.toc", FOREVER_TOC.format(name="loose"))
        self.refused(buf.getvalue(), "no addon folders")

    def test_oversized_archive_is_refused(self):
        original = forge.MAX_UNPACKED_BYTES
        forge.MAX_UNPACKED_BYTES = 10
        self.addCleanup(setattr, forge, "MAX_UNPACKED_BYTES", original)
        self.refused(make_zip(["Evil"]), "implausibly large")

    def test_windows_backslash_paths_are_unpacked_as_folders(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Evil\\Evil.toc", FOREVER_TOC.format(name="Evil"))
            zf.writestr("Evil\\sub\\file.lua", "x")
        self.world.add_file("evil", buf.getvalue(), "1")
        self.assertEqual(self.run_forge("update"), 0)
        self.assertTrue((self.addons / "Evil" / "sub" / "file.lua").is_file())

    def test_folder_owned_by_another_addon_is_not_overwritten(self):
        self.world.add_mod("good")
        self.world.add_file("good", make_zip(["Shared"], marker="good"), "1")
        self.world.add_file("evil", make_zip(["Shared", "Evil"], marker="evil"), "1")
        self.write_config([{"slug": "good"}, {"slug": "evil"}])
        self.assertEqual(self.run_forge("update"), 1)
        self.assertEqual(self.lua("Shared"), "-- Shared good\n")
        self.assertFalse((self.addons / "Evil").exists(), "a refused install leaves nothing behind")
        self.assertIn("folder Shared belongs to good", self.log_text())

    def test_failed_install_is_rolled_back_completely(self):
        self.world.add_file("evil", make_zip(["Evil", "EvilTwo"], marker="v1"), "1")
        self.run_forge("update")
        self.world.add_file("evil", make_zip(["Evil", "EvilTwo"], marker="v2"), "2")
        original, calls = shutil.copytree, []

        def flaky(src, dst, *a, **k):
            calls.append(dst)
            if len(calls) == 2:
                raise OSError("disk full")
            return original(src, dst, *a, **k)

        forge.shutil.copytree = flaky
        self.addCleanup(setattr, forge.shutil, "copytree", original)
        self.assertEqual(self.run_forge("update"), 1)
        forge.shutil.copytree = original
        self.assertEqual(self.lua("Evil"), "-- Evil v1\n")
        self.assertEqual(self.lua("EvilTwo"), "-- EvilTwo v1\n")
        self.assertEqual(self.state()["evil"]["version"], "1")
        self.assertEqual(list((self.home / "backups" / "evil").iterdir()), [], "the aborted backup is not left behind")
        self.assertEqual(self.run_forge("update"), 0)
        self.assertEqual(self.lua("EvilTwo"), "-- EvilTwo v2\n")


class BlockedDownloads(ForgeCase):
    """Authors can switch off third-party downloads. The forge respects that."""

    def setUp(self):
        super().setUp()
        self.world.add_mod("leatrix-plus", "Leatrix Plus", distributable=False)
        self.data = make_zip(["Leatrix_Plus"], marker="v1")
        self.file = self.world.add_file("leatrix-plus", self.data, "1.60.13")
        self.write_config([{"slug": "leatrix-plus"}])

    def test_never_downloads_and_never_guesses_a_cdn_url(self):
        self.assertEqual(self.run_forge("update"), 0)
        self.assertFalse((self.addons / "Leatrix_Plus").exists())
        self.assertFalse([c for c in self.world.calls if "forgecdn" in c or "edge." in c])
        self.assertIn("https://www.curseforge.com/wow/addons/leatrix-plus/files/", self.log_text())

    def test_fetch_itself_refuses_a_file_with_no_download_url(self):
        with self.assertRaises(forge.ForgeError):
            forge.fetch(self.file)
        self.assertFalse([c for c in self.world.calls if "forgecdn" in c])

    def test_user_is_told_once_per_version(self):
        self.run_forge("update")
        self.run_forge("update")
        self.assertEqual(len([n for n in self.notes if "Leatrix Plus needs a manual download" in n]), 1)
        self.world.add_file("leatrix-plus", make_zip(["Leatrix_Plus"], marker="v2"), "1.60.14")
        self.run_forge("update")
        self.assertEqual(len([n for n in self.notes if "Leatrix Plus needs a manual download" in n]), 2)

    def test_picks_up_the_exact_file_from_the_downloads_folder(self):
        (self.downloads / "leatrix-plus-1.60.13 (1).zip").write_bytes(self.data)
        self.assertEqual(self.run_forge("update"), 0)
        self.assertEqual(self.lua("Leatrix_Plus"), "-- Leatrix_Plus v1\n")
        self.assertEqual(self.state()["leatrix-plus"]["version"], "1.60.13")
        self.assertTrue((self.downloads / "leatrix-plus-1.60.13 (1).zip").exists(), "the user's download is left alone")

    def test_a_tampered_file_with_the_right_name_is_ignored(self):
        (self.downloads / "leatrix-plus-1.60.13.zip").write_bytes(make_zip(["Leatrix_Plus"], marker="tampered"))
        self.assertEqual(self.run_forge("update"), 0)
        self.assertFalse((self.addons / "Leatrix_Plus").exists())

    def test_import_command_installs_a_verified_zip_from_anywhere(self):
        elsewhere = self.tmp / "renamed.zip"
        elsewhere.write_bytes(self.data)
        self.assertEqual(self.run_forge("import", str(elsewhere)), 0)
        self.assertEqual(self.lua("Leatrix_Plus"), "-- Leatrix_Plus v1\n")
        self.assertEqual(self.state()["leatrix-plus"]["note"], "imported from a manual download")
        self.assertEqual(self.run_forge("status"), 0)

    def test_import_refuses_anything_it_cannot_verify(self):
        stranger = self.tmp / "stranger.zip"
        stranger.write_bytes(make_zip(["Malware"]))
        self.assertEqual(self.run_forge("import", str(stranger), str(self.tmp / "missing.zip")), 1)
        self.assertEqual(list(self.addons.iterdir()), [])
        self.assertIn("not a WoW Forever file of any addon you manage", self.log_text())

    def test_import_refuses_while_the_game_runs(self):
        self.running = True
        with self.assertRaises(SystemExit):
            self.run_forge("import", str(self.tmp / "x.zip"))

    def test_an_installed_blocked_addon_stays_installed_and_reports_the_update(self):
        (self.downloads / "leatrix-plus-1.60.13.zip").write_bytes(self.data)
        self.run_forge("update")
        self.world.add_file("leatrix-plus", make_zip(["Leatrix_Plus"], marker="v2"), "1.60.14")
        self.assertEqual(self.run_forge("update"), 0)
        self.assertEqual(self.lua("Leatrix_Plus"), "-- Leatrix_Plus v1\n")
        self.assertIn("1 waiting for a manual download", self.log_text())


class GitHubSource(ForgeCase):
    def setUp(self):
        super().setUp()
        os.environ.pop("CURSEFORGE_API_KEY", None)  # GitHub addons need no key
        self.repo = "studyalwaysbro/godslayer"
        self.write_config([])

    def test_add_installs_the_newest_release_without_any_key(self):
        self.world.add_release(self.repo, "v1.0.0", make_zip(["Godslayer"], marker="1.0.0"))
        self.assertEqual(self.run_forge("add", f"github:{self.repo}"), 0)
        self.assertEqual(self.lua("Godslayer"), "-- Godslayer 1.0.0\n")
        entry = self.state()["godslayer"]
        self.assertEqual((entry["name"], entry["source"], entry["version"]), ("Godslayer", f"github:{self.repo}", "v1.0.0"))
        cfg = forge.read_json(self.home / "forge.json", {})
        self.assertEqual(cfg["addons"], [{"slug": "godslayer", "github": self.repo}])
        self.assertFalse([c for c in self.world.calls if "curseforge" in c])

    def test_new_release_updates_and_unchanged_repo_uses_the_etag(self):
        self.world.add_release(self.repo, "v1.0.0", make_zip(["Godslayer"], marker="1.0.0"))
        self.run_forge("add", f"github:{self.repo}")
        self.run_forge("update")
        self.assertEqual(self.world.headers[-1].get("if-none-match"), self.world.etag(self.repo))
        self.assertEqual(self.lua("Godslayer"), "-- Godslayer 1.0.0\n")
        self.world.add_release(self.repo, "v1.1.0", make_zip(["Godslayer"], marker="1.1.0"))
        self.assertEqual(self.run_forge("update"), 0)
        self.assertEqual(self.lua("Godslayer"), "-- Godslayer 1.1.0\n")
        self.assertEqual(self.state()["godslayer"]["version"], "v1.1.0")

    def test_prerelease_is_skipped_while_a_release_exists(self):
        self.world.add_release(self.repo, "v1.0.0", make_zip(["Godslayer"], marker="stable"), date="2026-10-01T00:00:00Z")
        self.world.add_release(self.repo, "v2.0.0-rc1", make_zip(["Godslayer"], marker="rc"), prerelease=True, date="2026-10-05T00:00:00Z")
        self.run_forge("add", f"github:{self.repo}")
        self.assertEqual(self.lua("Godslayer"), "-- Godslayer stable\n")

    def test_beta_channel_takes_the_prerelease(self):
        self.world.add_release(self.repo, "v1.0.0", make_zip(["Godslayer"], marker="stable"), date="2026-10-01T00:00:00Z")
        self.world.add_release(self.repo, "v2.0.0-rc1", make_zip(["Godslayer"], marker="rc"), prerelease=True, date="2026-10-05T00:00:00Z")
        self.run_forge("add", f"github:{self.repo}", "beta")
        self.assertEqual(self.lua("Godslayer"), "-- Godslayer rc\n")

    def test_digest_mismatch_is_refused(self):
        self.world.add_release(self.repo, "v1.0.0", make_zip(["Godslayer"]))
        url = self.world.repos[self.repo][0]["assets"][0]["browser_download_url"]
        self.world.blobs[url] = make_zip(["Godslayer"], marker="swapped")
        self.assertEqual(self.run_forge("add", f"github:{self.repo}"), 1)
        self.assertFalse((self.addons / "Godslayer").exists())

    def test_release_without_a_zip_or_unknown_repo_is_a_problem(self):
        self.world.repos[self.repo] = [{"tag_name": "v1", "prerelease": False, "draft": False, "published_at": "x", "assets": []}]
        self.assertEqual(self.run_forge("add", f"github:{self.repo}"), 1)
        self.assertIn("no release zip on GitHub", self.log_text())
        self.assertEqual(self.run_forge("add", "github:nobody/nothing"), 1)

    def test_adopts_a_hand_installed_copy_and_keeps_it_as_a_backup(self):
        hand = self.addons / "Godslayer"
        hand.mkdir()
        (hand / "Godslayer.lua").write_text("hand installed")
        self.world.add_release(self.repo, "v1.0.0", make_zip(["Godslayer"], marker="1.0.0"))
        self.run_forge("add", f"github:{self.repo}")
        backup = next((self.home / "backups" / "godslayer").iterdir()) / "_classic_beta_" / "Godslayer" / "Godslayer.lua"
        self.assertEqual(backup.read_text(), "hand installed")

    def test_bad_specs_are_rejected(self):
        for spec in ("github:only-owner", "github:a/b/c", "Not A Slug", "../etc"):
            with self.assertRaises(SystemExit, msg=spec):
                self.run_forge("add", spec)

    def test_github_token_is_sent_only_to_github(self):
        os.environ["GITHUB_TOKEN"] = "tok"
        self.world.add_release(self.repo, "v1.0.0", make_zip(["Godslayer"]))
        self.run_forge("add", f"github:{self.repo}")
        self.assertEqual(self.world.headers[0].get("authorization"), "Bearer tok")


class RollbackAndRemove(ForgeCase):
    def setUp(self):
        super().setUp()
        self.world.add_mod("questie")
        self.world.add_file("questie", make_zip(["Questie"], marker="v1"), "v1")
        self.write_config([{"slug": "questie"}])
        self.run_forge("update")
        self.world.add_file("questie", make_zip(["Questie", "QuestieNew"], marker="v2"), "v2")
        self.run_forge("update")

    def test_rollback_restores_and_holds_until_add(self):
        self.assertEqual(self.run_forge("rollback", "questie"), 0)
        self.assertEqual(self.lua("Questie"), "-- Questie v1\n")
        self.assertFalse((self.addons / "QuestieNew").exists())
        self.assertEqual(self.run_forge("update"), 0)
        self.assertEqual(self.lua("Questie"), "-- Questie v1\n", "held: the timer must not undo a rollback")
        self.assertEqual(self.run_forge("add", "questie"), 0)
        self.assertEqual(self.lua("Questie"), "-- Questie v2\n")
        self.assertNotIn("hold", self.state()["questie"])

    def test_rollback_without_a_backup_fails_cleanly(self):
        self.run_forge("rollback", "questie")
        with self.assertRaises(SystemExit):
            self.run_forge("rollback", "questie")
        with self.assertRaises(SystemExit):
            self.run_forge("rollback", "never-installed")

    def test_remove_deletes_folders_and_forgets_the_addon(self):
        self.assertEqual(self.run_forge("remove", "questie"), 0)
        self.assertFalse((self.addons / "Questie").exists() or (self.addons / "QuestieNew").exists())
        self.assertNotIn("questie", self.state())
        self.assertEqual(forge.read_json(self.home / "forge.json", {})["addons"], [])

    def test_remove_and_rollback_refuse_while_the_game_runs(self):
        self.running = True
        for command in ("remove", "rollback"):
            with self.assertRaises(SystemExit):
                self.run_forge(command, "questie")
        self.assertEqual(self.lua("Questie"), "-- Questie v2\n")


class KeysAndNetwork(ForgeCase):
    def setUp(self):
        super().setUp()
        self.world.add_mod("questie")
        self.world.add_file("questie", make_zip(["Questie"]), "v1")
        self.write_config([{"slug": "questie"}])

    def test_key_lookup_order(self):
        cfg = forge.load_config()
        self.assertEqual(forge.curseforge_key(cfg), ("good-key", "environment variable CURSEFORGE_API_KEY"))
        del os.environ["CURSEFORGE_API_KEY"]
        self.assertEqual(forge.curseforge_key(cfg), (None, None))
        legacy = self.tmp / ".env"
        legacy.write_text("OTHER=1\n" + forge.KEY_NAME + "='legacy$value'\n")
        cfg["curseforge_env"] = str(legacy)
        self.assertEqual(forge.curseforge_key(cfg)[0], "legacy$value")
        (self.home / "curseforge.key").write_text("file-key\n")
        self.assertEqual(forge.curseforge_key(cfg), ("file-key", str(self.home / "curseforge.key")))

    def test_missing_key_explains_how_to_get_one_and_the_key_never_reaches_the_log(self):
        del os.environ["CURSEFORGE_API_KEY"]
        self.assertEqual(self.run_forge("update"), 1)
        self.assertIn("console.curseforge.com", self.log_text())
        os.environ["CURSEFORGE_API_KEY"] = "wrong-key"
        self.assertEqual(self.run_forge("update"), 1)
        self.assertIn("refused the API key", self.log_text())
        os.environ["CURSEFORGE_API_KEY"] = "good-key"
        self.run_forge("update")
        self.run_forge("doctor")
        for text in (self.log_text(), json.dumps(self.state())):
            self.assertNotIn("good-key", text)
            self.assertNotIn("wrong-key", text)

    def test_offline_is_a_problem_and_changes_nothing(self):
        self.run_forge("update")
        before = json.dumps(self.state()["questie"], sort_keys=True)
        self.world.offline = True
        self.assertEqual(self.run_forge("update"), 1)
        self.assertEqual(json.dumps(self.state()["questie"], sort_keys=True), before)
        self.assertTrue((self.addons / "Questie").is_dir())

    def test_a_standing_problem_is_announced_once_and_again_only_when_it_changes(self):
        self.world.offline = True
        self.run_forge("update")
        self.run_forge("update")
        self.assertEqual(len([n for n in self.notes if n.startswith("Needs a look")]), 1)
        self.world.offline = False
        self.run_forge("update")
        self.world.offline = True
        self.run_forge("update")
        self.assertEqual(len([n for n in self.notes if n.startswith("Needs a look")]), 2)

    def test_one_broken_addon_does_not_block_the_others(self):
        self.world.add_mod("broken")
        self.world.add_file("broken", b"junk", "1")
        self.write_config([{"slug": "broken"}, {"slug": "missing-entirely"}, {"slug": "questie"}])
        self.assertEqual(self.run_forge("update"), 1)
        self.assertTrue((self.addons / "Questie").is_dir())
        self.assertNotIn("missing-entirely", self.state(), "no empty records for addons that do not exist")

    def test_only_the_two_documented_hosts_are_contacted(self):
        self.world.add_release("o/r", "v1", make_zip(["R"]))
        self.write_config([{"slug": "questie"}, {"slug": "r", "github": "o/r"}])
        self.run_forge("update")
        from urllib.parse import urlsplit
        api_hosts = {urlsplit(c).netloc for c in self.world.calls if "/releases/download/" not in c and "forgecdn" not in c}
        self.assertEqual(api_hosts, {"api.curseforge.com", "api.github.com"})


class SetupCommands(ForgeCase):
    def setUp(self):
        super().setUp()
        (self.home / "forge.json").unlink()

    def test_init_with_a_path_writes_a_config_for_the_only_flavor(self):
        self.assertEqual(self.run_forge("init", str(self.game)), 0)
        cfg = forge.load_config()
        self.assertEqual(cfg["addon_dirs"], [str(self.addons.resolve())])
        self.assertEqual((cfg["addons"], cfg["interface_range"], cfg["forever_game_version_type"]), ([], [16000, 19999], 88568))

    def test_init_asks_which_flavor_when_there_are_several(self):
        (self.game / "_retail_").mkdir()
        self.assertEqual(self.run_forge("init", str(self.game)), 2)
        self.assertFalse((self.home / "forge.json").exists())
        self.assertEqual(self.run_forge("init", str(self.game), "--flavor", "_classic_beta_"), 0)
        self.assertTrue(forge.load_config()["addon_dirs"][0].endswith("_classic_beta_/Interface/AddOns"))

    def test_init_refuses_to_overwrite_and_rejects_bad_input(self):
        self.run_forge("init", str(self.game))
        with self.assertRaises(SystemExit):
            self.run_forge("init", str(self.game))
        (self.home / "forge.json").unlink()
        with self.assertRaises(SystemExit):
            self.run_forge("init", str(self.tmp / "nowhere"))
        with self.assertRaises(SystemExit):
            self.run_forge("init", str(self.game), "--flavor", "_nope_")

    def test_find_wow_searches_the_usual_launchers(self):
        base = self.tmp / "userhome"
        lutris = base / "Games" / "battlenet" / "drive_c" / "Program Files (x86)" / "World of Warcraft"
        steam = base / ".local/share/Steam/steamapps/compatdata/123/pfx/drive_c/Program Files (x86)/World of Warcraft"
        lutris.mkdir(parents=True)
        steam.mkdir(parents=True)
        self.assertEqual(forge.find_wow(base), sorted([lutris.resolve(), steam.resolve()]))
        self.assertEqual(forge.find_wow(self.tmp / "empty"), [])

    def test_find_wow_follows_steam_libraries_on_other_drives_and_other_launchers(self):
        base = self.tmp / "var" / "home" / "deck"
        tail = "drive_c/Program Files (x86)/World of Warcraft"
        card = self.tmp / "run" / "media" / "sdcard" / "SteamLibrary"
        steam = base / ".local/share/Steam"
        (steam / "steamapps").mkdir(parents=True)
        (steam / "steamapps" / "libraryfolders.vdf").write_text(
            f'"libraryfolders"\n{{\n\t"0"\n\t{{\n\t\t"path"\t\t"{steam}"\n\t}}\n\t"1"\n\t{{\n\t\t"path"\t\t"{card}"\n\t}}\n}}\n')
        (base / ".steam").mkdir()
        (base / ".steam" / "steam").symlink_to(steam)
        expected = [card / "steamapps/compatdata/3141592653/pfx" / tail,
                    base / "Games/Heroic/Prefixes/default/Battle.net/pfx" / tail,
                    base / "Faugus/battlenet" / tail,
                    base / ".var/app/com.usebottles.bottles/data/bottles/bottles/Gaming/drive_c/Program Files/World of Warcraft"]
        for folder in expected:
            folder.mkdir(parents=True)
        self.assertEqual(forge.find_wow(base), sorted(p.resolve() for p in expected), "each install is listed once")

    def test_commands_without_a_config_say_what_to_do(self):
        with self.assertRaises(SystemExit) as caught:
            self.run_forge("status")
        self.assertIn("yforge init", str(caught.exception))

    def test_unit_files_point_at_this_script_and_run_quietly(self):
        service, timer = forge.unit_files()
        self.assertIn(f'ExecStart="{sys.executable}" "{Path(forge.__file__).resolve()}" update --quiet', service)
        self.assertIn("OnUnitActiveSec=10min", timer)
        self.assertNotIn("YFORGE_HOME", service)
        os.environ["YFORGE_HOME"] = str(self.home)
        self.assertIn(f'Environment="YFORGE_HOME={self.home}"', forge.unit_files()[0])

    def test_unit_files_keep_a_path_with_spaces_or_a_percent_sign_in_one_piece(self):
        self.addCleanup(setattr, forge, "script_path", forge.script_path)
        forge.script_path = lambda: Path("/var/home/me/My Stuff/100%/forge.py")
        self.assertIn('"/var/home/me/My Stuff/100%%/forge.py" update --quiet', forge.unit_files()[0])

    def test_timer_without_systemd_offers_a_cron_line(self):
        saved = forge.shutil.which
        self.addCleanup(setattr, forge.shutil, "which", saved)
        forge.shutil.which = lambda name: None
        with self.assertRaises(SystemExit) as caught:
            self.run_forge("timer", "on")
        self.assertIn("*/10 * * * *", str(caught.exception))

    def test_version_and_help(self):
        self.assertEqual(self.run_forge("--version"), 0)
        self.assertEqual(self.run_forge("help"), 0)
        self.run_forge("init", str(self.game))
        self.assertEqual(self.run_forge("nonsense"), 2)


class SelfUpdate(ForgeCase):
    """The forge replaces its own file only with a release that proves itself."""

    def setUp(self):
        super().setUp()
        self.script = self.tmp / "share" / "forge.py"
        self.script.parent.mkdir()
        self.script.write_text("# the installed forge\n")
        saved = forge.script_path
        self.addCleanup(setattr, forge, "script_path", saved)
        forge.script_path = lambda: self.script
        self.write_config([], self_update=True)

    def publish(self, version, source=None, **kwargs):
        source = source if source is not None else f'VERSION = "{version}"\nprint("new forge")\n'
        self.world.add_release(forge.SELF_REPO, f"v{version}", source.encode(), asset_name="forge.py", **kwargs)
        return source

    def test_a_newer_release_replaces_the_file_and_keeps_the_old_one(self):
        source = self.publish("99.0.0")
        self.assertEqual(self.run_forge("update"), 0)
        self.assertEqual(self.script.read_text(), source)
        self.assertTrue(os.access(self.script, os.X_OK))
        self.assertEqual(self.script.with_name("forge.py.previous").read_text(), "# the installed forge\n")
        self.assertTrue(any("updated itself to 99.0.0" in n for n in self.notes))
        self.assertIn("updated itself", self.log_text())

    def test_it_updates_even_while_the_game_is_running(self):
        self.publish("99.0.0")
        self.running = True
        self.run_forge("update")
        self.assertIn("99.0.0", self.script.read_text())

    def test_the_same_or_an_older_release_changes_nothing(self):
        self.publish(forge.VERSION)
        self.publish("0.0.1")
        self.run_forge("update")
        self.assertEqual(self.script.read_text(), "# the installed forge\n")
        self.assertFalse(self.script.with_name("forge.py.previous").exists())

    def test_a_release_without_a_checksum_is_refused(self):
        self.publish("99.0.0", digest=False)
        self.run_forge("update")
        self.assertEqual(self.script.read_text(), "# the installed forge\n")
        self.assertIn("no checksummed forge.py", self.log_text())

    def test_a_tampered_download_is_refused_and_leaves_no_litter(self):
        self.publish("99.0.0")
        url = f"https://github.com/{forge.SELF_REPO}/releases/download/v99.0.0/forge.py"
        self.world.blobs[url] = b'VERSION = "99.0.0"\nimport os; os.system("evil")\n'
        self.run_forge("update")
        self.assertEqual(self.script.read_text(), "# the installed forge\n")
        self.assertIn("failed its hash check", self.log_text())
        self.assertEqual(sorted(p.name for p in self.script.parent.iterdir()), ["forge.py"])

    def test_a_file_that_is_not_python_or_claims_the_wrong_version_is_refused(self):
        self.publish("99.0.0", source='VERSION = "99.0.0"\nthis is not python (\n')
        self.run_forge("update")
        self.assertIn("not valid Python", self.log_text())
        self.publish("99.1.0", source='VERSION = "1.0.0"\n')
        with self.assertRaises(SystemExit) as caught:
            self.run_forge("self-update")
        self.assertIn("does not say it is v99.1.0", str(caught.exception))
        self.assertEqual(self.script.read_text(), "# the installed forge\n")

    def test_it_asks_at_most_once_an_hour_and_can_be_switched_off(self):
        self.run_forge("update")
        self.run_forge("update")
        self.assertEqual(len([c for c in self.world.calls if c.endswith("/releases/latest")]), 1)
        self.write_config([], self_update=False)
        (self.home / "state.json").unlink()
        self.world.calls.clear()
        self.publish("99.0.0")
        self.run_forge("update")
        self.assertEqual(self.world.calls, [])
        self.assertEqual(self.script.read_text(), "# the installed forge\n")

    def test_status_never_updates_the_forge(self):
        self.publish("99.0.0")
        self.run_forge("status")
        self.assertEqual(self.script.read_text(), "# the installed forge\n")

    def test_a_git_checkout_is_left_to_git(self):
        (self.script.parent / ".git").mkdir()
        self.publish("99.0.0")
        self.run_forge("update")
        self.assertEqual(self.world.calls, [])
        with self.assertRaises(SystemExit) as caught:
            self.run_forge("self-update")
        self.assertIn("git pull", str(caught.exception))

    def test_being_offline_is_reported_and_is_not_a_problem_for_the_addons(self):
        self.world.offline = True
        self.assertEqual(self.run_forge("update"), 0)
        self.assertIn("could not update the forge itself", self.log_text())

    def test_the_manual_command_reports_the_newest_release(self):
        self.publish("99.0.0")
        self.assertEqual(self.run_forge("self-update"), 0)
        self.assertIn("99.0.0", self.script.read_text())


class Hardening(ForgeCase):
    def test_a_slug_that_could_escape_a_folder_or_clash_with_bookkeeping_is_refused(self):
        self.write_config([{"slug": ".."}, {"slug": "_told"}, {"slug": "a/b"}])
        self.assertEqual(self.run_forge("update"), 1)
        self.assertEqual(self.world.calls, [])
        self.assertNotIn("..", self.state())
        for spec in ("github:owner/..", "github:owner/_self"):
            with self.assertRaises(SystemExit):
                self.run_forge("add", spec)

    def test_only_https_links_are_followed(self):
        for url in ("http://api.github.com/x", "file:///etc/passwd", "ftp://example.com/a.zip"):
            with self.assertRaises(forge.ForgeError):
                forge.require_https(url)
        forge.require_https("https://api.github.com/x")


class Helpers(unittest.TestCase):
    def test_game_process_pattern(self):
        running = [
            r"C:\Program Files (x86)\World of Warcraft\_classic_beta_\WowB.exe -launcherlogin -uid wow_classic_beta",
            r"Z:\games\World of Warcraft\_retail_\Wow.exe",
            "/home/u/Games/World of Warcraft/_classic_era_/WowClassic.exe ",
            "WowT.exe",
        ]
        idle = [
            r"C:\Program Files (x86)\World of Warcraft\World of Warcraft Launcher.exe",
            r"C:\Program Files (x86)\Battle.net\Battle.net.exe",
            "python3 forge.py update --quiet",
            "WowVoiceProxy.exe",
            "MeowWow.exe.bak",
        ]
        for line in running:
            self.assertTrue(forge.GAME_PROCESS.search(line), line)
        for line in idle:
            self.assertFalse(forge.GAME_PROCESS.search(line), line)

    def test_game_running_reads_proc_without_crashing(self):
        self.assertIn(forge.game_running(), (True, False))

    def test_choose_orders_by_stability_then_date(self):
        files = [{"id": 3, "releaseType": 3, "fileDate": "3"}, {"id": 2, "releaseType": 2, "fileDate": "2"}, {"id": 1, "releaseType": 1, "fileDate": "1"}]
        self.assertEqual(forge.choose(files, "release")[0]["id"], 1)
        self.assertEqual(forge.choose(files, "beta")[0]["id"], 2)
        self.assertEqual(forge.choose(files, "alpha")[0]["id"], 3)
        self.assertEqual(forge.choose(files[:1], "release"), (files[0], "alpha"))
        self.assertEqual(forge.choose([], "release"), (None, None))

    def test_home_resolution_order(self):
        saved = dict(os.environ)
        try:
            os.environ["YFORGE_HOME"] = "/tmp/somewhere"
            self.assertEqual(forge.find_home(), Path("/tmp/somewhere"))
            del os.environ["YFORGE_HOME"]
            here = Path(forge.__file__).resolve().parent
            expected = here if (here / "forge.json").exists() else Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "yeeterson-forge"
            self.assertEqual(forge.find_home(), expected)
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_glob_escape(self):
        self.assertEqual(forge.glob_escape("a[1]*?"), "a[[]1[]][*][?]")


if __name__ == "__main__":
    unittest.main()
