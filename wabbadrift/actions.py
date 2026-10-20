"""Copy and replace mods from the baseline into the custom instance, with backup and undo.

Only the custom instance's mods folder and profile are written (plus the pair's backup folder);
files are read from the installed baseline. Every run:

1. is planned first (the dry-run preview the user confirms; ``plan``),
2. refuses while the custom instance's ModOrganizer.exe runs,
3. re-plans and refuses if anything changed since the preview (fingerprint),
4. backs up every file it will overwrite or delete, records a journal, then writes.

``undo`` restores a run exactly, and refuses (listing them) if a file the run wrote has changed
since. One run, one mod or many, is one undo entry.
"""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import mods
from .config import Instance, Pair
from .hashing import HashCache, hash_file
from .plugins import PLUGIN_EXTENSIONS
from .profile import ModEntry, read_profile

COPY = "copy"
REPLACE = "replace"

# file operations, in the order they run
MKDIR = "mkdir"
CREATE = "create"
OVERWRITE = "overwrite"
DELETE = "delete"
RMDIR = "rmdir"

MODS = "mods"
PROFILE = "profile"
PROFILE_FILES = ("modlist.txt", "plugins.txt", "loadorder.txt")
JOURNAL = "journal.json"


class ActionError(Exception):
    """A run or undo that must not go ahead; ``files`` lists what blocks it."""

    def __init__(self, message: str, files: list[str] | None = None):
        super().__init__(message)
        self.files = files or []


# --- is MO2 running? -----------------------------------------------------------------------


def running_executables() -> list[str]:
    """Full image paths of running processes (Windows; empty elsewhere)."""
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    psapi = ctypes.WinDLL("psapi")
    k32 = ctypes.WinDLL("kernel32")
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    count = 4096
    while True:
        pids = (wintypes.DWORD * count)()
        needed = wintypes.DWORD()
        if not psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
            return []
        if needed.value < ctypes.sizeof(pids):
            break
        count *= 2
    out = []
    for pid in pids[: needed.value // ctypes.sizeof(wintypes.DWORD)]:
        handle = k32.OpenProcess(
            0x1000, False, pid
        )  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            continue
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                out.append(buf.value)
        finally:
            k32.CloseHandle(handle)
    return out


def mo2_running(inst: Instance) -> bool:
    """The instance's own ModOrganizer.exe (portable); any ModOrganizer.exe if it has none."""
    exe = inst.mo2_dir / "ModOrganizer.exe"
    procs = running_executables()
    if exe.is_file():
        want = os.path.normcase(os.path.abspath(exe))
        return any(os.path.normcase(p) == want for p in procs)
    return any(os.path.basename(p).lower() == "modorganizer.exe" for p in procs)


# --- small helpers -------------------------------------------------------------------------


def _inside(path: Path, root: Path) -> bool:
    path, root = Path(os.path.abspath(path)), Path(os.path.abspath(root))
    return path == root or root in path.parents


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


class TextFile:
    """A profile text file edited line by line, keeping its encoding, BOM and line endings."""

    def __init__(self, path: Path):
        self.path = path
        self.original = path.read_bytes() if path.is_file() else None
        data = self.original or b""
        self.bom = data.startswith(codecs.BOM_UTF8)
        text = data[len(codecs.BOM_UTF8) if self.bom else 0 :].decode(
            "utf-8", errors="surrogateescape"
        )
        self.nl = "\n" if text and "\r\n" not in text else "\r\n"
        self.lines = text.splitlines(keepends=True)
        if self.lines and not self.lines[-1].endswith(("\n", "\r")):
            self.lines[-1] += self.nl

    @property
    def exists(self) -> bool:
        return self.original is not None

    def find(self, name: str, strip: str = "") -> int | None:
        """Index of the line naming ``name`` (case-insensitive), after removing ``strip`` chars."""
        key = name.lower()
        for i, line in enumerate(self.lines):
            text = line.rstrip("\r\n")
            if text.startswith("#"):
                continue
            if strip and text[:1] in strip:  # one flag character: "+", "-", "*"
                text = text[1:]
            if text.lower() == key:
                return i
        return None

    def insert(self, index: int, line: str) -> None:
        self.lines.insert(index, line + self.nl)

    def append(self, line: str) -> None:
        self.lines.append(line + self.nl)

    def content(self) -> bytes:
        body = "".join(self.lines).encode("utf-8", errors="surrogateescape")
        return (codecs.BOM_UTF8 if self.bom else b"") + body

    @property
    def changed(self) -> bool:
        return self.exists and self.content() != self.original


def _first_entry_line(modlist: TextFile) -> int:
    for i, line in enumerate(modlist.lines):
        if not line.startswith("#"):
            return i
    return len(modlist.lines)


def _modlist_line(modlist: TextFile, name: str) -> int | None:
    return modlist.find(name, strip="+-*")


# --- planning ------------------------------------------------------------------------------


@dataclass
class Op:
    kind: str
    area: str
    rel: str
    source: Path | None = None
    size: int = 0


@dataclass
class ModPlan:
    action: str
    mod: str  # baseline name
    target: str = ""  # custom folder name
    ok: bool = True
    reason: str = ""
    notes: list[str] = field(default_factory=list)
    placement: str = ""
    plugins: list[dict] = field(default_factory=list)  # {name, placement}
    files: list[dict] = field(default_factory=list)  # {rel, kind, size}
    bytes: int = 0
    ops: list[Op] = field(default_factory=list, repr=False)


@dataclass
class Plan:
    pair: str
    mods: list[ModPlan] = field(default_factory=list)
    profile_diffs: list[dict] = field(default_factory=list)  # {file, lines}
    mo2_running: bool = False
    fingerprint: str = ""
    profile: dict[str, TextFile] = field(default_factory=dict, repr=False)

    @property
    def ok_mods(self) -> list[ModPlan]:
        return [m for m in self.mods if m.ok]


def _copy_ops(bfolder: Path, target: str) -> list[Op]:
    """Every folder and file of the baseline mod, into a new custom folder."""
    ops = [Op(MKDIR, MODS, target)]
    for dirpath, dirnames, filenames in os.walk(bfolder):
        dirnames.sort()
        here = Path(dirpath)
        rel = Path(target) / here.relative_to(bfolder)
        for d in dirnames:
            ops.append(Op(MKDIR, MODS, (rel / d).as_posix()))
        for f in sorted(filenames):
            src = here / f
            ops.append(Op(CREATE, MODS, (rel / f).as_posix(), src, src.stat().st_size))
    return ops


def _tree(folder: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    """Lower-cased relative dirs and files of a folder -> their paths."""
    dirs, files = {}, {}
    for dirpath, dirnames, filenames in os.walk(folder):
        here = Path(dirpath)
        for d in dirnames:
            p = here / d
            dirs[_rel(p, folder).lower()] = p
        for f in filenames:
            p = here / f
            files[_rel(p, folder).lower()] = p
    return dirs, files


def _replace_ops(
    bfolder: Path, cfolder: Path, mods_dir: Path, cache: HashCache
) -> list[Op]:
    """What makes the custom folder an exact copy of the baseline folder (content by hash)."""
    bdirs, bfiles = _tree(bfolder)
    cdirs, cfiles = _tree(cfolder)
    target = cfolder.name
    ops = []
    for key in sorted(bdirs.keys() - cdirs.keys(), key=lambda k: k.count("/")):
        ops.append(Op(MKDIR, MODS, f"{target}/{_rel(bdirs[key], bfolder)}"))
    both = [k for k in bfiles if k in cfiles]
    same_size = [
        k for k in both if bfiles[k].stat().st_size == cfiles[k].stat().st_size
    ]
    todo = []
    for k in same_size:
        for p in (bfiles[k], cfiles[k]):
            st = p.stat()
            todo.append((str(p), st.st_size, st.st_mtime_ns))
    hashes = cache.hash_many(todo)
    for key in sorted(bfiles):
        src = bfiles[key]
        size = src.stat().st_size
        if key not in cfiles:
            ops.append(Op(CREATE, MODS, f"{target}/{_rel(src, bfolder)}", src, size))
        elif key not in same_size or hashes[str(src)] != hashes[str(cfiles[key])]:
            ops.append(Op(OVERWRITE, MODS, _rel(cfiles[key], mods_dir), src, size))
    for key in sorted(cfiles.keys() - bfiles.keys()):
        p = cfiles[key]
        ops.append(Op(DELETE, MODS, _rel(p, mods_dir), size=p.stat().st_size))
    for key in sorted(cdirs.keys() - bdirs.keys(), key=lambda k: -k.count("/")):
        ops.append(Op(RMDIR, MODS, _rel(cdirs[key], mods_dir)))
    return ops


def _root_plugins(folder: Path) -> list[str]:
    if not folder or not folder.is_dir():
        return []
    return sorted(
        p.name
        for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in PLUGIN_EXTENSIONS
    )


class _Placer:
    """Places new modlist.txt and plugin entries in the custom profile, in memory."""

    def __init__(self, profile_dir: Path, bentries: list[ModEntry], border: list[str]):
        self.modlist = TextFile(profile_dir / "modlist.txt")
        self.plugins = TextFile(profile_dir / "plugins.txt")
        self.loadorder = TextFile(profile_dir / "loadorder.txt")
        self.bentries = [e for e in bentries if not e.unmanaged]
        self.border = border
        self.present = set()
        for f in (self.plugins, self.loadorder):
            for ln in f.lines:
                n = ln.rstrip("\r\n")
                if n and not n.startswith("#"):
                    self.present.add((n[1:] if n.startswith("*") else n).lower())

    def mod(self, bname: str, flag: str, custom_of) -> str:
        """Insert ``flag + bname`` next to its nearest baseline neighbour that custom has.

        modlist.txt lists the highest priority first, so "after X" in MO2's order is the line
        above X's.
        """
        names = [e.name for e in self.bentries]
        pos = names.index(bname)
        sep = self.bentries[pos].separator
        line = flag + bname

        def custom_line(e: ModEntry) -> tuple[int | None, str]:
            c = e.name if e.is_separator else custom_of(e.name)
            return (_modlist_line(self.modlist, c) if c else None), (c or "")

        for e in reversed(self.bentries[:pos]):  # lower priority, same separator
            if e.is_separator:
                break
            i, c = custom_line(e)
            if i is not None:
                self.modlist.insert(i, line)
                return f"after {c}"
        for e in self.bentries[pos + 1 :]:  # higher priority, same separator
            if e.is_separator:
                break
            i, c = custom_line(e)
            if i is not None:
                self.modlist.insert(i + 1, line)
                return f"before {c}"
        if sep:
            i = _modlist_line(self.modlist, sep)
            if i is not None:
                self.modlist.insert(i, line)
                return f"first under separator {sep[: -len('_separator')]}"
        for e in reversed(self.bentries[:pos]):  # any lower-priority neighbour
            if e.is_separator:
                continue
            i, c = custom_line(e)
            if i is not None:
                self.modlist.insert(i, line)
                note = (
                    f" (separator {sep[: -len('_separator')]} isn't in custom)"
                    if sep
                    else ""
                )
                return f"after {c}{note}"
        self.modlist.insert(_first_entry_line(self.modlist), line)
        return "at the end of the modlist (no baseline neighbour in custom)"

    def plugin(self, name: str) -> str | None:
        """Insert an enabled plugin after its nearest baseline neighbour plugin custom has."""
        key = name.lower()
        if key in self.present:
            return None
        order = [p.lower() for p in self.border]
        prev = None
        if key in order:
            for p in reversed(self.border[: order.index(key)]):
                if p.lower() in self.present:
                    prev = p
                    break
        for f, prefix in ((self.plugins, "*"), (self.loadorder, "")):
            if f is self.loadorder and not f.exists:
                continue
            i = f.find(prev, strip="*") if prev else None
            if i is None:
                f.append(prefix + name)
            else:
                f.insert(i + 1, prefix + name)
        self.present.add(key)
        return f"after {prev}" if prev else "at the end"


def _target_ok(pair: Pair, rel: str) -> bool:
    return _inside(pair.custom.mods_dir / rel, pair.custom.mods_dir)


def plan(
    pair: Pair,
    comparisons: list[mods.ModComparison],
    copy: list[str],
    replace: list[str],
    cache: HashCache,
) -> Plan:
    """The dry run: what copying and replacing these baseline mods would do."""
    cust, base = pair.custom, pair.baseline
    if _inside(cust.mods_dir, base.mo2_dir) or _inside(cust.profile_dir, base.mo2_dir):
        raise ActionError(
            "The custom instance is inside the baseline instance; refusing to write."
        )
    by_name = {m.name: m for m in comparisons}
    out = Plan(pair.name, mo2_running=mo2_running(cust))
    bprof = read_profile(base.profile_dir)
    border = bprof.loadorder or [p.name for p in bprof.plugins]
    placer = _Placer(cust.profile_dir, bprof.mods, border)
    copied: dict[str, str] = {}
    matched = {
        m.baseline.name: m.custom.name for m in comparisons if m.baseline and m.custom
    }

    def custom_of(bname: str) -> str | None:
        return copied.get(bname) or matched.get(bname)

    bprio = {e.name: e.priority for e in bprof.mods}
    for name in sorted(dict.fromkeys(copy), key=lambda n: bprio.get(n, -1)):
        mp = ModPlan(COPY, name, name)
        out.mods.append(mp)
        m = by_name.get(name)
        b = m.baseline if m else None
        entry = bprof.mod(name)
        if b is None or b.folder is None:
            mp.ok, mp.reason = False, "not in the baseline"
        elif m.custom is not None:
            mp.ok, mp.reason = (
                False,
                f"your instance already has it as '{m.custom.name}'; use Reset",
            )
        elif entry is None or entry.unmanaged:
            mp.ok, mp.reason = False, "not in the baseline modlist"
        elif (cust.mods_dir / name).exists():
            mp.ok, mp.reason = (
                False,
                "a custom folder with this name exists but isn't matched",
            )
        elif not _target_ok(pair, name):
            mp.ok, mp.reason = False, "unsafe folder name"
        if not mp.ok:
            continue
        mp.ops = _copy_ops(b.folder, name)
        mp.placement = placer.mod(name, "+" if entry.enabled else "-", custom_of)
        copied[name] = name
        mp.notes.append(
            f"{'enabled' if entry.enabled else 'disabled'}, as in the baseline"
        )

    for name in dict.fromkeys(replace):
        mp = ModPlan(REPLACE, name)
        out.mods.append(mp)
        m = by_name.get(name)
        if not m or not m.baseline or not m.baseline.folder:
            mp.ok, mp.reason = False, "not in the baseline"
            continue
        if not m.custom or not m.custom.folder:
            mp.ok, mp.reason = False, "your instance has no folder for it; use Add"
            continue
        b, c = m.baseline, m.custom
        mp.target = c.name
        if not _target_ok(pair, c.name):
            mp.ok, mp.reason = False, "unsafe folder name"
            continue
        if c.name != b.name:
            mp.notes.append(
                f"keeps the custom folder name '{c.name}' (baseline: '{b.name}') and its place"
            )
        mp.ops = _replace_ops(b.folder, c.folder, cust.mods_dir, cache)
        if not mp.ops:
            mp.ok, mp.reason = False, "already identical"
            continue
        mp.placement = "keeps its place and enabled state"

    for mp in out.ok_mods:
        bfolder = base.mods_dir / mp.mod
        for p in sorted(_root_plugins(bfolder), key=lambda n: _order_key(border, n)):
            where = placer.plugin(p)
            if where:
                mp.plugins.append({"name": p, "placement": where})
        mp.files = [
            {"rel": o.rel, "kind": o.kind, "size": o.size}
            for o in mp.ops
            if o.kind in FILE_KINDS
        ]
        mp.bytes = sum(o.size for o in mp.ops if o.kind in (CREATE, OVERWRITE))

    for f in (placer.modlist, placer.plugins, placer.loadorder):
        if f.changed:
            out.profile[f.path.name] = f
            out.profile_diffs.append({"file": f.path.name, "lines": _diff(f)})
    out.fingerprint = _fingerprint(pair, out)
    return out


FILE_KINDS = (CREATE, OVERWRITE, DELETE)


def _order_key(order: list[str], name: str) -> int:
    lower = [p.lower() for p in order]
    return lower.index(name.lower()) if name.lower() in lower else len(lower)


def _diff(f: TextFile) -> list[str]:
    import difflib

    old = (f.original or b"").decode("utf-8", errors="replace").splitlines()
    new = f.content().decode("utf-8", errors="replace").splitlines()
    return list(
        difflib.unified_diff(old, new, f"custom/{f.path.name}", "after", lineterm="")
    )


def _stat(p: Path) -> list | None:
    try:
        st = p.stat()
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


def _fingerprint(pair: Pair, p: Plan) -> str:
    """Changes when anything the plan reads or writes changes on disk."""
    roots = {MODS: pair.custom.mods_dir, PROFILE: pair.custom.profile_dir}
    items = []
    for mp in p.ok_mods:
        items.append([mp.action, mp.mod, mp.target])
        for o in mp.ops:
            src = _stat(o.source) if o.source else None
            items.append(
                [
                    o.kind,
                    o.area,
                    o.rel,
                    str(o.source or ""),
                    src,
                    _stat(roots[o.area] / o.rel),
                ]
            )
    for name, f in sorted(p.profile.items()):
        items.append(
            [
                name,
                hashlib.sha256(f.original or b"").hexdigest(),
                hashlib.sha256(f.content()).hexdigest(),
            ]
        )
    return hashlib.sha256(json.dumps(items, default=str).encode()).hexdigest()


# --- applying ------------------------------------------------------------------------------


def _roots(pair: Pair) -> dict[str, Path]:
    return {MODS: pair.custom.mods_dir, PROFILE: pair.custom.profile_dir}


def _write_journal(run_dir: Path, journal: dict) -> None:
    tmp = run_dir / (JOURNAL + ".tmp")
    tmp.write_text(json.dumps(journal, indent=1), encoding="utf-8")
    os.replace(tmp, run_dir / JOURNAL)


def apply(
    pair: Pair,
    comparisons: list[mods.ModComparison],
    copy: list[str],
    replace: list[str],
    fingerprint: str,
    cache: HashCache,
) -> dict:
    """Run a previewed plan. Returns the journal."""
    if mo2_running(pair.custom):
        raise ActionError(
            "Mod Organizer is running for the custom instance. Close it first."
        )
    p = plan(pair, comparisons, copy, replace, cache)
    if p.fingerprint != fingerprint:
        raise ActionError("Something changed since the preview. Preview again.")
    if not p.ok_mods:
        raise ActionError("Nothing to do.")
    roots = _roots(pair)
    for root in roots.values():
        if _inside(pair.backup_dir, root):
            raise ActionError(
                "The backup folder is inside the custom instance; change backup_dir."
            )

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    run_dir = pair.backup_dir / run_id
    backup = run_dir / "backup"
    run_dir.mkdir(parents=True)
    ops = [o for mp in p.ok_mods for o in mp.ops]
    order = [MKDIR, CREATE, OVERWRITE, DELETE, RMDIR]
    ops.sort(key=lambda o: order.index(o.kind))  # stable: keeps each kind's own order
    entries, dirs_created, dirs_removed = [], [], []

    # back up everything that exists and will change
    for o in ops:
        target = roots[o.area] / o.rel
        if not _inside(target, roots[o.area]):
            raise ActionError(
                f"Refusing to write outside the custom instance: {target}"
            )
        if o.kind in (OVERWRITE, DELETE):
            dest = backup / o.area / o.rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, dest)
            entries.append(
                {
                    "area": o.area,
                    "rel": o.rel,
                    "kind": o.kind,
                    "before": hash_file(target),
                }
            )
        elif o.kind == CREATE:
            entries.append(
                {"area": o.area, "rel": o.rel, "kind": o.kind, "before": None}
            )
    for name in p.profile:
        target = roots[PROFILE] / name
        dest = backup / PROFILE / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, dest)
        entries.append(
            {
                "area": PROFILE,
                "rel": name,
                "kind": OVERWRITE,
                "before": hash_file(target),
            }
        )

    journal = {
        "id": run_id,
        "pair": pair.name,
        "created": time.time(),
        "status": "applying",
        "roots": {k: str(v) for k, v in roots.items()},
        "mods": [
            {
                "action": mp.action,
                "mod": mp.mod,
                "target": mp.target,
                "notes": mp.notes,
                "placement": mp.placement,
                "plugins": mp.plugins,
            }
            for mp in p.ok_mods
        ],
        "entries": entries,
        "dirs_created": dirs_created,
        "dirs_removed": dirs_removed,
    }
    _write_journal(run_dir, journal)

    try:
        for o in ops:
            target = roots[o.area] / o.rel
            if o.kind == MKDIR:
                if not target.exists():
                    target.mkdir()
                    dirs_created.append([o.area, o.rel])
            elif o.kind in (CREATE, OVERWRITE):
                shutil.copy2(o.source, target)
            elif o.kind == DELETE:
                target.unlink()
            elif o.kind == RMDIR:
                target.rmdir()
                dirs_removed.append([o.area, o.rel])
        for name, f in p.profile.items():
            (roots[PROFILE] / name).write_bytes(f.content())
        for e in entries:
            if e["kind"] != DELETE:
                e["after"] = hash_file(roots[e["area"]] / e["rel"])
        journal["status"] = "done"
    except OSError as err:
        journal["status"] = "failed"
        journal["error"] = str(err)
        _write_journal(run_dir, journal)
        raise ActionError(
            f"Writing failed ({err}). Undo this run to restore what was written."
        )
    _write_journal(run_dir, journal)
    return journal


# --- undo log ------------------------------------------------------------------------------


def list_runs(pair: Pair) -> list[dict]:
    """Newest first."""
    out = []
    if pair.backup_dir.is_dir():
        for d in pair.backup_dir.iterdir():
            f = d / JOURNAL
            if f.is_file():
                j = json.loads(f.read_text(encoding="utf-8"))
                out.append(
                    {
                        k: v
                        for k, v in j.items()
                        if k not in ("entries", "dirs_created", "dirs_removed")
                    }
                    | {"files": len(j["entries"])}
                )
    return sorted(out, key=lambda j: j["created"], reverse=True)


def _load_run(pair: Pair, run_id: str) -> tuple[Path, dict]:
    runs = {r["id"] for r in list_runs(pair)}
    if run_id not in runs:
        raise ActionError("Unknown run.")
    run_dir = pair.backup_dir / run_id
    return run_dir, json.loads((run_dir / JOURNAL).read_text(encoding="utf-8"))


def undo(pair: Pair, run_id: str) -> dict:
    if mo2_running(pair.custom):
        raise ActionError(
            "Mod Organizer is running for the custom instance. Close it first."
        )
    run_dir, j = _load_run(pair, run_id)
    if j["status"] == "undone":
        raise ActionError("This run was already undone.")
    roots = _roots(pair)
    if j["roots"] != {k: str(v) for k, v in roots.items()}:
        raise ActionError(
            "This run belongs to other instance folders; refusing to undo it here."
        )
    for e in j["entries"]:
        if not _inside(roots[e["area"]] / e["rel"], roots[e["area"]]):
            raise ActionError(
                "The undo log points outside the custom instance; refusing."
            )

    # refuse if anything the run wrote changed since
    changed = []
    for e in j["entries"]:
        target = roots[e["area"]] / e["rel"]
        if e["kind"] == DELETE:
            if target.exists():
                changed.append(f"{e['rel']} (was deleted, exists again)")
        elif "after" in e:
            if not target.is_file():
                changed.append(f"{e['rel']} (missing)")
            elif hash_file(target) != e["after"]:
                changed.append(f"{e['rel']} (changed)")
    if changed:
        raise ActionError(
            "Files this run wrote have changed since; undo refused.", changed
        )

    backup = run_dir / "backup"
    for area, rel in j["dirs_removed"][::-1]:  # parents first
        (roots[area] / rel).mkdir(exist_ok=True)
    for e in j["entries"]:
        target = roots[e["area"]] / e["rel"]
        if e["kind"] == CREATE:
            if target.exists():
                target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup / e["area"] / e["rel"], target)
    for area, rel in j["dirs_created"][::-1]:  # children first
        d = roots[area] / rel
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    j["status"] = "undone"
    j["undone"] = time.time()
    _write_journal(run_dir, j)
    return j
