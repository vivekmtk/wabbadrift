# Wabbadrift

Using a Wabbajack modlist as your baseline? Wabbadrift shows what drifted (mods, plugins, configs)
and recovers the FOMOD installer choices each mod was installed with, so you can see which options
the list picked and where yours differ. Update or restore mods with backup and undo.

It compares your Mod Organizer 2 (MO2) instance with a clean install of the same list, file by
file.

It works with any MO2-managed game and any Wabbajack modlist.

## What you need

- Windows (MO2 and Wabbajack are Windows tools).
- [Python 3.10 or newer](https://www.python.org/downloads/). Tick *Add Python to PATH* when
  installing.
- Your MO2 instance, the one you play and customise.
- A **baseline**: a clean install of the same list made by Wabbajack, in its own folder. It's
  the reference Wabbadrift compares against, so keep it untouched.
- The list's `.wabbajack` file. Wabbajack keeps it under
  `<Wabbajack folder>\<version>\downloaded_mod_lists\`.
- Optional, for reading FOMOD installer choices: 7-Zip. Wabbadrift finds a 7-Zip install or the
  copy bundled with Wabbajack on its own.

## Starting it

Double-click `wabbadrift.bat`. The first start sets up a private Python environment in the
`.venv` folder and installs two packages (this needs an internet connection and takes a few
seconds). After that, it starts the local server and opens Wabbadrift in your browser. Keep the
console window open while you use it; closing it stops Wabbadrift.

Wabbadrift only listens on your own computer (`localhost`). To use another port, copy
`.env.example` to `.env` and change `WABBADRIFT_PORT`.

## Setting up a comparison

The first start opens the setup form. Later, use **New** to add another comparison and
**Details** → **Edit** to change one. You can keep as many comparisons as you like.

1. Give it a name (letters, digits, `-` and `_`).
2. **Your instance:** the MO2 folder (the one with `ModOrganizer.exe` and `ModOrganizer.ini`),
   then pick the profile.
3. **Baseline instance:** the same for the clean install.
4. **Wabbajack list:** the `.wabbajack` file. The form shows the list's name and version once it
   can read it.
5. Optional settings: game folders and the downloads folder (taken from `ModOrganizer.ini` when
   left empty), the LOOT userlist, and where to keep backups.

Every path is checked before the comparison is saved.

## What it shows

| Tab | Shows |
|---|---|
| Mods | Mods only in the baseline, only in your instance, at a different version, or with changed files. Open a mod to compare its files and diff its text files. |
| Plugins | Plugins added, removed, moved or enabled differently, and LOOT group differences. |
| Game root | Files in the game folder that differ from what the list installs. |
| Configs | Profile INIs, MO2 settings, executables and config files inside mods. |
| Leaks | Paths in your instance that still point into the baseline, into Wabbajack's folders, or to places that don't exist. |
| FOMOD | The installer options the list chose for each FOMOD mod, and where your copy differs. |
| Updates | What a new version of the list changes, and whether each change is safe to apply to your instance. |
| History | Every set of changes you applied, with Undo. |

Some differences are expected: generated outputs, files tied to the game version, settings that
depend on the machine, and separators you mark as your own. They are hidden by default; **Show
expected drift** brings them back. The rules for this live in
`data/pairs/<name>.rules.yaml`, which you can edit.

**Details** shows what a comparison compares, including each instance's game version and the
game version the list was built for.

## Adding and resetting mods

In the Mods tab, tick mods in the **Change** column:

- **Add** copies a mod you don't have from the baseline into your instance, places it in your
  modlist next to its neighbours from the baseline, and enables its plugins.
- **Reset** makes your copy of a mod identical to the baseline's. Files you added to it are
  removed (they're backed up first).

**Review changes** shows exactly what will happen before anything is written: every file, where
each mod and plugin goes, and the edits to `modlist.txt`, `plugins.txt` and `loadorder.txt`.
**Apply** then:

- refuses while MO2 is running for your instance,
- refuses if anything changed since you reviewed it,
- backs up every file it overwrites or removes, and
- records the changes in History, where **Undo** restores your instance exactly. Undo refuses,
  and lists the files, if you've edited any of them since.

## What gets written

- **Your instance:** only when you apply changes, and only its mods folder and profile.
- **The baseline and the `.wabbajack`:** never.
- **Wabbadrift's own `data` folder:** comparisons, rules, caches, snapshots of list versions,
  and backups (unless you chose another backup folder).

## For developers

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest
.venv\Scripts\python -m wabbadrift.server --no-browser
```

The tests build small synthetic MO2 instances and `.wabbajack` files; they never touch a real
install. `DESIGN.md` explains how it works and why.
