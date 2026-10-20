# Wabbadrift — Design

## What it does

Wabbadrift is a local tool that shows how a customised Mod Organizer 2 (MO2) instance has drifted
from the Wabbajack modlist it was built from, and lets the user add missing mods from that
baseline or reset changed ones to it.

It works for any MO2-managed game and any Wabbajack modlist. Nothing game-, list- or
machine-specific lives in the code.

## Concepts

- **Pair**: what the user configures. One pair is:
  - a **custom instance**: MO2 folder + profile + game root (the one the user changed);
  - a **baseline instance**: the MO2 folder Wabbajack installed + its profile + game root;
  - the **.wabbajack file** and the **downloads folder** that holds the source archives.

  Game roots and the downloads folder are read from each instance's `ModOrganizer.ini`
  (`gamePath`, `download_directory`); the user can override them. Other profiles in either
  instance are ignored.
- **Baseline truth**:
  - The **installed baseline** is the target. Mod order, enabled state and separators come from
    the installed baseline profile. Copy and replace take whatever is in the baseline mods
    folder. The baseline is assumed pristine; the tool doesn't second-guess it.
  - The **.wabbajack** supplies Nexus mod/file IDs, archive info, per-file hashes and sizes (so
    the baseline install never needs hashing), FOMOD evidence, and the history used for
    update drift.

## Stack

- Python 3.10+, server bound to `127.0.0.1` only (stdlib `http.server`), serving a static
  HTML/JS UI with no framework and no build step. No network access.
- Runtime dependencies (`requirements.txt`): `xxhash` (Wabbajack's file hash; a pure-Python
  xxHash64 is far too slow for mod folders) and `PyYAML` (rules files, YAML config diffs). Dev
  (`requirements-dev.txt`): those plus `pytest`. Everything else is stdlib
  (`zipfile`, `json`, `sqlite3`, `difflib`, `tomllib` where available). INI files use a small
  own parser, because MO2 and game INIs don't fit `configparser`.
- `7z.exe` (external, path configurable per pair, auto-detected from a 7-Zip install or
  Wabbajack's bundled `Extractors\windows-x64\7z.exe`) is used read-only to list archives and
  stream `fomod/ModuleConfig.xml` to memory (`-so`). Nothing is extracted to disk.
- UI: a Nordic dark/light theme built from CSS tokens. Fonts come from Google Fonts and fall
  back to system fonts offline.

## User data (git-ignored, under `data/`)

| Path | Contents |
|---|---|
| `data/pairs/<pair>.json` | Pair config |
| `data/pairs/<pair>.rules.yaml` | Expected-drift rules for the pair |
| `data/cache/<pair>.sqlite` | Content-hash cache (path, size, mtime → xxHash64) |
| `data/cache/fomod/` | Archive listings + ModuleConfig.xml, per archive (path, size, mtime) |
| `data/snapshots/<pair>/` | Baseline snapshots (gzipped `modlist` JSON per .wabbajack version) |
| `data/backups/<pair>/` | Default backup + undo-log location (configurable per pair) |

## Components

| Module | Responsibility |
|---|---|
| `config` | Load, check and save pair configs (the setup form); auto-detect game roots, downloads, 7z |
| `mo2ini` | Parse `ModOrganizer.ini` (both `k=v` and `k = v`; `@ByteArray`, `@Variant`, quoting/escapes; `customExecutables`) |
| `profile` | Read `modlist.txt`, `plugins.txt`, `loadorder.txt`, `plugingroups.txt`, `lockedorder.txt`, `archives.txt`, profile INIs; ignore timestamped backups |
| `wabbajack` | Read the `modlist` entry from the .wabbajack zip in memory; index archives and directives by mod / game root / profile / other |
| `hashing` | Wabbajack-compatible xxHash64 (base64 of little-endian digest), cached |
| `mods` | Match mods (Nexus mod/file ID, then name), status, priority/separator/enabled, per-file diff |
| `order` | Minimal "moved" set between two orders (longest increasing subsequence) |
| `plugins` | plugins.txt, loadorder.txt, LOOT userlist groups and MO2 plugin groups (read-only) |
| `gameroot` | Game root vs game root, and vs the .wabbajack `Game Root\` directives |
| `configs` | Key-by-key diff of profile INIs, ModOrganizer.ini, INI/TOML/JSON/YAML inside mods; text diffs |
| `leaks` | Path-leak report (flag only, never fix) |
| `rules` | Expected-drift rules: defaults per game + generic fallback, editable per pair |
| `fomod` | Parse ModuleConfig.xml and infer the choices from the directives |
| `drift` | Baseline snapshots and update drift between two .wabbajack versions |
| `actions` | Copy / replace mods with preview, backup and undo; MO2-running check |
| `report` | One pair's scan, per-mod hashing, file diffs, FOMOD and update drift as JSON-ready data |
| `server` | JSON API + static UI (`web/`) |

## Key decisions

### Mod matching and status

Custom mods are matched to baseline mods by Nexus mod/file ID (custom `meta.ini` vs .wabbajack
archive state), then by folder name. Each mod is one of: only in baseline, only in custom,
different version or file, same version with changed files, or identical. Priority, separator and
enabled state are compared separately.

### Scanning cost

The first scan uses metadata only: file list, size, mtime and `meta.ini` `installedFiles`, compared
against the directive `To`/`Size` values. Content hashes are computed only when the user opens a
mod, or when sizes match but mtimes differ. Hashes are compared with the directive hashes, never
against a hashed baseline install. Hashes are cached by path + size + mtime.

### .wabbajack handling

The `modlist` JSON is read in memory with `zipfile`. The zip is never extracted (it can be
several GB unpacked). `CreateBSA` source files (`TEMP_BSA_FILES\<TempID>\…`) are BSA contents,
not loose files, and are kept apart.

### Plugin groups

Wabbajack lists ship their groups in MO2's `plugingroups.txt`; LOOT's userlist usually lives
outside the instance (per game, not per instance) and is often shared, so a baseline rarely has
its own. The comparison is read-only: the custom LOOT userlist groups and the custom
`plugingroups.txt`, against the baseline's delivered `plugingroups.txt`.

### Game root

Every path in either game root is compared against its .wabbajack `Game Root\` directive when one
exists (hash and size), otherwise against the installed baseline game root (both sides hashed
when sizes match but mtimes differ). Each file is labelled by source: vanilla game file, mod
archive, inline, built BSA, or install-only. Files that an enabled mod's RootBuilder `root/`
folder provides are annotated, because RootBuilder only copies them in while the game runs.

### Comparing values across instances

ModOrganizer.ini values and executables are compared after replacing each instance's own paths
(game root, mods, profiles, overwrite, downloads, MO2 folder) with placeholders such as
`%GAME_ROOT%`. A value that is "the same place in its own instance" is then not drift, and a
value that still points at the other instance stands out. UI-state sections (geometry, widgets,
recent directories, download-server stats) are ignored.

### Expected drift

Each pair has a rules file (defaults per game, plus a generic fallback) for generator outputs,
game-version files, machine-specific INI keys, and intentional additions (separators matching a
user pattern). Expected drift is hidden by default and grouped separately behind one toggle.
Intentional additions are listed as such, not as drift.

- Defaults: `wabbadrift/defaults/rules.yaml` has a `common` section plus one per game (Skyrim
  SE/VR/LE, Fallout 4/VR, New Vegas, Fallout 3, Oblivion, Starfield, generic). The game comes
  from the .wabbajack `GameType`, else MO2's `gameName`. On first use, `common` + the game are
  merged (lists concatenate) into `data/pairs/<pair>.rules.yaml`, which is the file users edit.
- Patterns are case-insensitive globs where only `*` and `?` are special, so a separator pattern
  like `[Custom]*` means what it says.
- Results are tagged, not removed: mods, files inside mods, plugins, game-root files and INI keys
  get an `expected` category (generator output / game version / machine-specific / intentional
  addition). A mod whose every differing file is expected takes that category.
- Script-extender plugin DLLs (and their `.pdb`) count as game-version files only when the two
  game versions differ (a game executable listed as a game-version file changed). Otherwise a
  changed DLL is real drift.
- Plugins inherit the category of the mod that provides them (enabled, highest priority).

### Path leaks

The leak scan checks executables (binary, working directory, arguments), custom_overwrites,
profile INI values, a list of known tool config locations that can be extended, and a generic scan
for absolute paths in configs. It flags values that resolve into the baseline instance or its game
root, into a Wabbajack temp/install folder (including `__temp__`), or to paths that don't exist.
It reports where each value was found, what it points to, and a suggested custom-instance
equivalent. It never fixes anything.

- Check order: a Wabbajack temp segment (`__temp__`) wins, then baseline game root, baseline
  instance, the Wabbajack install folder (derived from the .wabbajack's location), and finally
  "doesn't exist".
- Suggestions map the baseline's game root, mods, profiles, overwrite, downloads and MO2 folder to
  the custom ones (longest prefix first) and say whether the result exists.
- An executable whose binary is in the baseline but whose working directory is in the custom
  game root gets a note: it runs the baseline's copy against the custom game.
- Known tool configs live in `wabbadrift/defaults/known_tools.yaml` (globs with `%MO2%`, `%MODS%`,
  `%OVERWRITE%`, `%PROFILE%` or environment variables); a pair's rules file can add more.
- The generic scan covers INI/JSON/YAML/TOML/XML/CFG files in custom mods, overwrite and the
  profile. To keep it signal, not noise: JSON/YAML/TOML strings count only when the whole value
  is a path (not prose), `meta.ini` is skipped (download history), and a missing path counts
  only if its drive exists on this machine (otherwise it is a mod author's leftover).

### FOMOD

For baseline mods whose main source archive (the one most of the mod's files come from) contains
`fomod/ModuleConfig.xml`, the tool infers each choice (Step → Group → Option), including
conditional installs and flag dependencies. Each choice has a confidence (exact / likely /
ambiguous) and its evidence. Installers are never re-run.

- Files are identified by content: size + CRC from the 7z listing. Every option, required
  install and conditional install expands to (mod-folder path, content) pairs; within one
  install list the highest priority, then the later entry, wins a path.
- What is installed: for the baseline, the directives name each file's archive path; a file
  Wabbajack took from another archive counts when its size matches exactly one candidate. For
  the custom mod, files are matched by size, then CRC32. `.mohidden` files count as installed.
- Rules, repeated until nothing changes:
  - an option none of whose files are in the mod, or most of them missing, wasn't selected;
  - an option is selected when it alone explains an installed file;
  - group types (exactly one, at most one, at least one, all) decide the rest of a group, but
    "needs a selection" only applies to a step that was shown;
  - files only conditional installs explain imply the flags those installs all need; a
    conditional install that didn't run (file missing, or replaced by content no other
    conditional install provides; they run last) rules out the one unknown flag it needed;
  - a hidden step rules out its options; a selected option implies its step's flags.
- Last resort, for a shown group that needs a selection: try each option, play the rules
  forward, and keep the one with the fewest files unexplained or missing, then the fewest of its
  files overwritten, then the fewest options selected. A tie stays ambiguous.
- Only a fixed `<type>` (Required / NotUsable) counts; a `dependencyType` default doesn't.
- The custom mod is read against its own installer when its `installationFile` is in the
  downloads folder and has one, else against the baseline's. Choices both sides decide
  differently are listed as differences, with the custom evidence.

### Update drift and snapshots

- A snapshot (`data/snapshots/<pair>/`) is the gzipped `modlist` JSON plus the inlined files a
  comparison needs: the delivered profile's top-level files (not timestamped backups), every
  mod's meta.ini and inlined configs up to 1 MB. It is named by the SHA-256 of `modlist`, so the
  same list saved by several Wabbajack versions is stored once; a small `.meta.json` beside it
  holds name, version and source so listing doesn't unpack anything.
- Two versions compare:
  - mods: old and new matched like custom and baseline (Nexus file, name, Nexus mod). Added,
    removed, or updated (files by path + hash, meta.ini aside; or a new Nexus version/name).
    A modlist.txt entry without directives isn't in that version;
  - game-root files by path + hash;
  - mod order, separators and enabled state from the delivered modlist.txt; plugins added,
    removed, enabled/disabled (plugins.txt) and moved (loadorder.txt), "moved" being the
    minimal set from the longest increasing subsequence;
  - profile INIs key by key.
- Each change is related to the custom instance:
  - already matches / safe to apply (custom still equals the old version) / conflict (custom
    differs from both) / not in custom;
  - mods compare the custom folder with the old and new directives (hashes cached);
    game-root files hash the custom file; INI keys read the custom profile;
  - a moved item is judged on the items the update put on its other side: custom agreeing with
    the new order on all of them already matches, with the old order is safe, else conflict.

### Server and UI

- `wabbadrift.bat` starts it (creating `.venv` from `requirements.txt`, runtime only, on first
  run, so the packages never touch the system Python); it runs
  `python -m wabbadrift.server`, which serves `web/` and a JSON API on `127.0.0.1` (port from
  `WABBADRIFT_PORT`, default 8765). Hardening: non-localhost `Host` headers are
  refused (DNS rebinding); only an allowlist of files is served; POSTs need an `X-Wabbadrift`
  header plus JSON content type, which forces a CORS preflight that is never granted; pair names
  must be configured pairs; file-diff paths can't leave the mod folder.
- Comparisons (pairs) are set up in the UI, never by editing JSON: a name, each instance's MO2
  folder and profile, the .wabbajack, and optional game folders, downloads folder, LOOT userlist
  and backup folder. Each MO2 folder is checked as it is entered (it must hold
  `ModOrganizer.ini`; its profiles fill a dropdown) and the .wabbajack is opened to show the
  list's name and version. Saving checks everything again server-side: the profile exists, the
  two instances are separate folders (neither inside the other), the backup folder is outside
  both, and the whole config loads. The profile is always stored explicitly. Editing keeps keys
  the form doesn't show (rules file, 7-Zip path); the name can't change, because caches,
  snapshots and the undo history are kept under it. Removing a comparison deletes only its
  JSON. The first start with no comparisons opens the form.
- The header only picks the comparison (Details, New, Rescan) and the view. Details opens the
  comparison read-only in the side panel, with Edit there: game, each instance's profile, game
  version and folders, the list and the game version it was built for, and the other folders.
  An instance whose game version differs from the list's is flagged. The game version is the
  file version of the game's main executable (the first exact `.exe` among the rules' game-version
  files, e.g. `SkyrimSE.exe`); the list's is the most common `GameVersion` of its game-file
  archives.
- Scan results live in memory only; reopening a comparison rescans (seconds once the hash cache is
  warm). Saved results could be stale while still feeding mod details and copy/replace plans.
- One state per pair keeps the parsed .wabbajack, the rules and the last scan in memory, so opening
  a mod, a FOMOD report or an update comparison doesn't rescan. Work on a pair runs under its lock;
  the hash cache's SQLite connection is shared across request threads one at a time.
- The scan is metadata only. Opening a mod hashes every file present on both sides; text files get
  a key diff (INI/TOML/JSON/YAML) or a line diff on request. FOMOD reports are read per mod or for
  every mod at once (the FOMOD tab).
- The UI escapes everything read from the instances and a Content-Security-Policy forbids inline
  and third-party scripts. Expected drift is hidden by default; one toggle shows it, grouped
  separately. Every status has a word, not only a colour.

### Writes

- The UI calls copy **Add** and replace **Reset** (a mod reset to the baseline's version), ticked in
  a "Change" column and applied after "Review changes"; the code and journal keep copy/replace.
- Direction is baseline → custom only. Nothing ever writes to the baseline instance or the
  .wabbajack.
- **Copy** brings the mod folder + `meta.ini`, a `modlist.txt` entry right after its nearest
  baseline neighbour that also exists in custom (same separator), and its plugins, enabled, in
  `plugins.txt` and `loadorder.txt` right after their nearest baseline neighbour plugin that
  exists in custom (at the end if none).
- **Replace** shows the full difference first. After one confirmation, the custom mod folder
  becomes an exact copy of the baseline mod folder. Files only in custom are deleted (backed up
  first). If the folder names differ (matched by Nexus ID), the custom folder name and its
  modlist position are kept and the rename is logged.
- The LOOT userlist is never written (it is often shared between instances).
- Copy and replace work on one mod or many. The selection list starts empty, has "select all",
  and shows each mod's custom enabled state. The tool never decides for the user.
- Before every write:
  - refuse while the custom instance's `ModOrganizer.exe` is running;
  - show a dry-run preview;
  - back up every file and folder to be touched into a timestamped folder; and
  - record an undo-log entry (one per run). Undo restores exactly. It refuses and lists the
    files if anything the run wrote has changed since.

How it is built (`actions.py`):

- Copy brings the modlist entry with the baseline's enabled state. Placement in MO2 order:
  right after the nearest lower-priority baseline neighbour in the same separator that custom
  has, else right before the nearest higher-priority one, else first under the separator, else
  after any lower-priority neighbour, else at the top priority. Mods copied in the same run count
  as neighbours, so a block of new mods keeps its order.
- Replace also adds plugins that the baseline version ships and custom doesn't list yet, with
  the same placement rule as copy (otherwise MO2 would add them disabled at the end).
- Replace compares the two folders by content (xxHash64, cached): files only in the baseline
  are created, different ones overwritten, custom-only files deleted, folders created or
  removed so the tree matches. Identical files aren't touched.
- Profile files (`modlist.txt`, `plugins.txt`, `loadorder.txt`) are edited line by line,
  keeping their encoding, BOM and line endings.
- The preview is a plan computed without writing. Apply re-plans and refuses unless the plan's
  fingerprint (every source and target's size and mtime, the profile files' content) matches
  the preview's, so nothing changed in between.
- "MO2 is running" means the instance's own `ModOrganizer.exe` is a running process (process
  image paths via the Win32 API); for a non-portable instance, any `ModOrganizer.exe`.
- A run lives in `<backup_dir>/<timestamp>-<id>/`: `backup/` holds the files it overwrote or
  deleted (copied with their timestamps), and `journal.json` holds every file's hash before
  and after, plus the folders it created or removed. The journal is written before the first
  write (status `applying`), so an interrupted run can still be undone.
- Undo checks MO2, that the journal belongs to this pair's folders, and that every file the
  run wrote still has its after-hash (and deleted files are still gone). Then it recreates
  removed folders, restores backups, deletes created files and removes the folders it created.
- Only the custom mods folder and profile are written; every target is checked to be inside
  them, and refused if the custom instance lies inside the baseline.
