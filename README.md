# Yeeterson Forge

**A WoW: Forever addon updater for Linux that you can read in one sitting.**

One Python file. Standard library only. No telemetry, no account, no Overwolf, no Electron. It keeps your addons current in the background and gets out of the way.

```
$ yforge status
DungeonJournal             ForeverDungeonJournal1.6.0   up to date
Questie                    v12.0.4+v1.0.5               up to date
AtlasLoot Classic Forever  1.2.1-071026                 up to date
What's Training?           11.0.0-beta15                up to date (beta build, no release yet)
Godslayer                  v1.0.0                       up to date
```

## Why this exists

The official CurseForge app does not run on Linux. The good alternatives are much bigger programs. This one does a single job, for a single game, and every decision it makes is written down below.

If you want a full-featured manager for every WoW flavor, use [instawow](https://github.com/layday/instawow). It is excellent. Use this if you play Forever on Linux and want something small enough to audit yourself.

## What it does

- Installs and updates addons from **CurseForge** and from **GitHub releases**.
- Only ever takes files tagged for **WoW: Forever**, so a retail build never lands in your Forever folder.
- Prefers stable releases. If an addon has no release for Forever yet, it takes the newest beta and tells you.
- Installs required dependencies on its own.
- Checks every 10 minutes with a systemd user timer, and **never touches anything while the game is running**.
- Keeps the last three versions of every addon. `yforge rollback <addon>` puts the previous one back and holds it there.

## What it promises

| Promise | How |
|---|---|
| The file is the author's file | SHA-1 (CurseForge) or SHA-256 (GitHub) checked before anything is unpacked |
| A zip cannot write outside the addon folder | Absolute paths, `..`, drive letters and symlinks are refused |
| The addon is for Forever | Refused unless a `.toc` lists an interface version from 16000 to 19999 |
| An update cannot leave you half-installed | If any step fails, every folder is put back exactly as it was |
| Your own addons are safe | Folders the forge did not install are never modified or removed |
| It only follows secure links | Anything that is not `https` is refused |
| It does not phone home | It talks to `api.curseforge.com` and `api.github.com`, plus the download links they return. Nothing else. A test enforces this |
| Your API key stays yours | Read at run time, never written to a log or state file. A test enforces this too |

## Install

One line, no sudo, nothing outside your home folder:

```sh
curl -fsSL https://github.com/studyalwaysbro/yeeterson-forge/releases/latest/download/install.sh | sh -s -- github:studyalwaysbro/godslayer
```

That downloads the newest release, checks it against its published checksum, finds your game, starts managing [Godslayer](https://github.com/studyalwaysbro/godslayer) and turns on the background timer. Leave off everything after `sh` to install the forge alone. [install.sh](install.sh) is about 80 lines, so read it first if you like.

If it cannot find the game, it tells you. Point it at the `World of Warcraft` folder inside your launcher's `drive_c` and switch the timer on:

```sh
yforge init "/path/to/World of Warcraft" --flavor _classic_beta_
yforge add github:studyalwaysbro/godslayer
yforge timer on
```

Prefer to do it by hand? Clone the repository, link `forge.py` as `~/.local/bin/yforge`, run `yforge init`. A git checkout is never changed by the forge; update it with `git pull`.

### Which Linux

Any distro with Python 3.9 or newer, and it never needs root. That includes the image-based ones where the system is read-only: **Bazzite**, SteamOS on the Steam Deck, Fedora Silverblue and Kinoite. Everything lives in your home folder.

`yforge init` looks for the game in the places launchers put it: Lutris, Heroic, Faugus, Bottles (native and Flatpak), plain Wine, and Steam (native, Flatpak and Snap), including Steam libraries on another drive or an SD card.

The background timer is a systemd user timer. On a distro without systemd, `yforge timer on` prints a crontab line to use instead.

### The forge updates itself

Installed as above, the forge checks its own GitHub releases about once an hour and replaces its one file when there is a newer one. The new file must match the SHA-256 that GitHub recorded for the release, be valid Python and carry the version the release claims, and the old file is kept next to it as `forge.py.previous`. Releases of this repository are immutable: once published, a release and its files cannot be swapped.

This means you trust this repository's releases the same way you trust the addons it installs. If you would rather decide yourself, set `"self_update": false` in `forge.json` and run `yforge self-update` when you choose.

### The CurseForge key

CurseForge requires every third-party tool to use an API key, and the honest way to do that is for you to use your own. It is free and takes a minute:

1. Sign in at <https://console.curseforge.com> and copy the key under **API keys**.
2. Save it as the only line of `~/.config/yeeterson-forge/curseforge.key`, then `chmod 600` that file.

Addons that ship as GitHub releases need no key at all.

## Use

```sh
yforge add questie                         # the slug is the last part of the addon's CurseForge URL
yforge add whats-training beta             # accept beta builds for this one
yforge add github:studyalwaysbro/godslayer # an addon published as GitHub releases
yforge status                              # what is installed, what is newer
yforge update                              # do it now
yforge timer on                            # or let it check every 10 minutes
yforge rollback questie                    # that update was bad, go back
yforge remove questie
yforge doctor                              # paths, key, and whether the game is running
yforge self-update                         # update the forge itself right now
```

## Addons that block third-party downloads

Some authors switch off downloads for anything except the official CurseForge app. Leatrix Plus and Auctionator are two. Other tools get around that by guessing the download link. **The forge does not.** It is the author's call.

What it does instead is almost as convenient. `yforge status` gives you the link:

```
Leatrix Plus: open https://www.curseforge.com/wow/addons/leatrix-plus/files/9088634
  download Leatrix_Plus-1.60.13-forever.zip and leave it in your Downloads folder.
```

Download it in your browser and walk away. On its next run the forge finds the file, confirms its hash matches what the author published, and installs it with the same checks and backups as everything else. A file that does not match is ignored. You can also hand it a zip directly with `yforge import file.zip`.

## Where things live

Everything is in one folder, `~/.config/yeeterson-forge/` (or `$YFORGE_HOME`):

| File | What it is |
|---|---|
| `forge.json` | Your addon list and game folders. Plain JSON, edit it freely |
| `curseforge.key` | Your API key |
| `state.json` | What is installed |
| `backups/` | The last three versions of each addon |
| `logs/forge.log` | Everything it did and why |

Addon settings live in the game's `WTF` folder. The forge never goes near it.

When the launch client installs into a new folder next to `_classic_beta_`, the forge notices and tells you. Add that folder's `Interface/AddOns` path to `addon_dirs` in `forge.json` and both stay in sync.

## Tests

```sh
python3 -m unittest discover -s tests
```

82 tests, no network, under a second. CurseForge and GitHub are faked in memory, so the suite covers the cases you hope never to meet: tampered downloads, zips that try to escape their folder, a disk that fills up halfway through an install, a self-update that is not what it claims to be.

## Scope

Linux, WoW: Forever, CurseForge and GitHub. No GUI. Not affiliated with Blizzard, CurseForge or Overwolf. MIT licensed.
