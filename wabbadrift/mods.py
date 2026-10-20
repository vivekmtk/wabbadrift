"""Mod-level comparison between the custom instance and the baseline.

Baseline truth for a mod's files is the .wabbajack directives (path, size, hash). The installed
baseline is only stat'ed, to skip hashing custom files whose size and mtime match it. Mod order,
enabled state and separators come from the installed baseline profile.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Instance
from .hashing import HashCache
from .mo2ini import IniFile, read_ini
from .order import out_of_order
from .profile import Profile, read_profile
from .wabbajack import FileDirective, Modlist

ONLY_BASELINE = "only_baseline"
ONLY_CUSTOM = "only_custom"
DIFFERENT_VERSION = "different_version"
CHANGED_FILES = "changed_files"
IDENTICAL = "identical"

# file statuses
ADDED = "added"  # only in custom
REMOVED = "removed"  # only in baseline
CHANGED = "changed"
SAME = "same"
PENDING = "pending"  # needs a hash to decide

META_INI = "meta.ini"
_INSTALLED_KEY = re.compile(r"^(\d+)\\(modid|fileid)$", re.IGNORECASE)


def _int(value: str | None) -> int | None:
    try:
        n = int(value or "")
    except ValueError:
        return None
    return n if n > 0 else None


@dataclass
class NexusInfo:
    mod_id: int | None = None
    files: frozenset[tuple[int, int]] = frozenset()  # (mod id, file id)
    version: str = ""
    installation_file: str = ""

    @property
    def mod_ids(self) -> set[int]:
        ids = {m for m, _ in self.files}
        if self.mod_id:
            ids.add(self.mod_id)
        return ids


def nexus_info(ini: IniFile | None) -> NexusInfo:
    if ini is None:
        return NexusInfo()
    entries: dict[str, dict[str, int | None]] = {}
    for key, value in ini.sections.get("installedFiles", {}).items():
        m = _INSTALLED_KEY.match(key)
        if m:
            entries.setdefault(m.group(1), {})[m.group(2).lower()] = _int(value)
    files = frozenset(
        (e["modid"], e["fileid"])
        for e in entries.values()
        if e.get("modid") and e.get("fileid")
    )
    return NexusInfo(
        mod_id=_int(ini.get("General", "modid")),
        files=files,
        version=ini.get("General", "version", "") or "",
        installation_file=Path(ini.get("General", "installationFile", "") or "").name,
    )


@dataclass
class ModSide:
    name: str
    in_modlist: bool
    enabled: bool | None
    priority: int | None
    separator: str | None
    nexus: NexusInfo
    folder: Path | None


@dataclass
class FileDiff:
    path: str
    status: str
    custom_size: int | None = None
    baseline_size: int | None = None
    custom_hash: str | None = None
    baseline_hash: str | None = None
    reason: str = ""
    expected: str = ""  # expected-drift category, see rules.py
    abs_path: str | None = field(default=None, repr=False)
    mtime_ns: int | None = field(default=None, repr=False)


@dataclass
class ModComparison:
    status: str
    custom: ModSide | None
    baseline: ModSide | None
    match: str = ""  # nexus / name / modid
    version_note: str = ""
    files: list[FileDiff] = field(default_factory=list)  # differences only
    file_counts: dict[str, int] = field(default_factory=dict)
    order_moved: bool = False
    separator_changed: bool = False
    enabled_changed: bool = False
    expected: str = ""  # expected-drift category, see rules.py

    @property
    def name(self) -> str:
        return (self.baseline or self.custom).name


@dataclass
class ScanResult:
    mods: list[ModComparison]
    hashed_files: int = 0
    timings: dict[str, float] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for m in self.mods:
            out[m.status] = out.get(m.status, 0) + 1
        return out


# --- collecting both sides ----------------------------------------------------------------


def walk_files(root: Path) -> dict[str, tuple[str, int, int]]:
    """Lower-cased relative path -> (absolute path, size, mtime_ns), recursively."""
    out: dict[str, tuple[str, int, int]] = {}
    if not root.is_dir():
        return out
    prefix = len(str(root)) + 1
    stack = [str(root)]
    while stack:
        d = stack.pop()
        with os.scandir(d) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                else:
                    st = e.stat()
                    out[e.path[prefix:].lower()] = (e.path, st.st_size, st.st_mtime_ns)
    return out


def _sides(
    profile: Profile,
    mods_dir: Path,
    metas: dict[str, IniFile],
    extra_names: list[str] = (),
) -> list[ModSide]:
    sides = []
    seen = set()
    for entry in profile.mods:
        if entry.is_separator or entry.unmanaged:
            continue
        seen.add(entry.name.lower())
        sep = entry.separator[: -len("_separator")] if entry.separator else None
        folder = mods_dir / entry.name
        sides.append(
            ModSide(
                name=entry.name,
                in_modlist=True,
                enabled=entry.enabled,
                priority=entry.priority,
                separator=sep,
                nexus=nexus_info(metas.get(entry.name)),
                folder=folder if folder.is_dir() else None,
            )
        )
    for name in extra_names:
        if name.lower() in seen or name.endswith("_separator"):
            continue
        folder = mods_dir / name
        sides.append(
            ModSide(
                name=name,
                in_modlist=False,
                enabled=None,
                priority=None,
                separator=None,
                nexus=nexus_info(metas.get(name)),
                folder=folder if folder.is_dir() else None,
            )
        )
    return sides


def custom_sides(inst: Instance, profile: Profile) -> list[ModSide]:
    metas = {}
    for entry in profile.mods:
        meta = inst.mods_dir / entry.name / META_INI
        if meta.is_file():
            metas[entry.name] = read_ini(meta)
    return _sides(profile, inst.mods_dir, metas)


def baseline_sides(inst: Instance, profile: Profile, ml: Modlist) -> list[ModSide]:
    names = [e.name for e in profile.mods] + list(ml.mods)
    metas = ml.mod_metas([n for n in dict.fromkeys(names) if n in ml.mods])
    return _sides(profile, inst.mods_dir, metas, extra_names=list(ml.mods))


# --- matching -----------------------------------------------------------------------------


def match_mods(
    custom: list[ModSide], baseline: list[ModSide]
) -> list[tuple[ModSide | None, ModSide | None, str]]:
    """Pair mods by Nexus mod+file ID, then by name, then by Nexus mod ID alone."""
    pairs: list[tuple[ModSide | None, ModSide | None, str]] = []
    free_c = {id(c): c for c in custom}
    free_b = {id(b): b for b in baseline}

    def take(c: ModSide, b: ModSide, how: str) -> None:
        del free_c[id(c)]
        del free_b[id(b)]
        pairs.append((c, b, how))

    def best(c: ModSide, candidates: list[ModSide]) -> ModSide | None:
        if not candidates:
            return None
        same_name = [b for b in candidates if b.name.lower() == c.name.lower()]
        return (same_name or candidates)[0]

    for c in custom:
        if not c.nexus.files:
            continue
        cands = [b for b in free_b.values() if c.nexus.files & b.nexus.files]
        b = best(c, cands)
        if b:
            take(c, b, "nexus")
    by_name = {b.name.lower(): b for b in free_b.values()}
    for c in list(free_c.values()):
        b = by_name.get(c.name.lower())
        if b and id(b) in free_b:
            take(c, b, "name")
    for c in list(free_c.values()):
        ids = c.nexus.mod_ids
        if not ids:
            continue
        b = best(c, [b for b in free_b.values() if ids & b.nexus.mod_ids])
        if b:
            take(c, b, "modid")
    pairs += [(c, None, "") for c in free_c.values()]
    pairs += [(None, b, "") for b in free_b.values()]
    return pairs


def version_difference(c: ModSide, b: ModSide) -> str:
    """Empty when the two look like the same version/file, else a short reason."""
    cn, bn = c.nexus, b.nexus
    if cn.files and bn.files:
        if cn.files != bn.files:
            return "different Nexus file"
        return ""
    if cn.installation_file and bn.installation_file:
        if cn.installation_file.lower() != bn.installation_file.lower():
            return "different installation file"
        return ""
    if cn.version and bn.version and cn.version != bn.version:
        return "different version"
    return ""


# --- files --------------------------------------------------------------------------------


def compare_files(
    custom_files: dict[str, tuple[str, int, int]],
    baseline_install: dict[str, tuple[str, int, int]],
    directives: list[FileDirective],
    full: bool = False,
) -> list[FileDiff]:
    """Compare one mod's custom files with its directives, by metadata.

    Files whose content can only be decided by a hash come back ``PENDING``. With ``full``,
    every file present on both sides is hashed.
    """
    out = []
    known = set()
    for d in directives:
        key = d.path.lower()
        if key == META_INI:
            continue
        known.add(key)
        c = custom_files.get(key)
        if c is None:
            out.append(
                FileDiff(d.path, REMOVED, baseline_size=d.size, baseline_hash=d.hash)
            )
            continue
        cpath, csize, cmtime = c
        diff = FileDiff(
            d.path,
            PENDING,
            custom_size=csize,
            baseline_size=d.size,
            baseline_hash=d.hash,
            abs_path=cpath,
            mtime_ns=cmtime,
        )
        size_known = d.kind != "CreateBSA"
        b = baseline_install.get(key)
        if size_known and csize != d.size:
            diff.status, diff.reason = CHANGED, "size"
        elif not full and b and b[1] == csize and b[2] == cmtime:
            diff.status, diff.reason = SAME, "size+mtime"
        elif not size_known and b and b[1] != csize:
            diff.status, diff.reason = CHANGED, "size"
        out.append(diff)
    for key, (cpath, csize, _) in custom_files.items():
        if key == META_INI or key in known:
            continue
        rel = cpath[-len(key) :]
        out.append(FileDiff(rel, ADDED, custom_size=csize))
    return out


def resolve_pending(diffs: list[FileDiff], hashes: dict[str, str]) -> None:
    for d in diffs:
        if d.status != PENDING:
            continue
        d.custom_hash = hashes.get(d.abs_path)
        if d.custom_hash == d.baseline_hash:
            d.status, d.reason = SAME, "hash"
        else:
            d.status, d.reason = CHANGED, "hash"


def pending_files(diffs: list[FileDiff]) -> list[tuple[str, int, int]]:
    return [
        (d.abs_path, d.custom_size, d.mtime_ns) for d in diffs if d.status == PENDING
    ]


def file_counts(diffs: list[FileDiff]) -> dict[str, int]:
    out = {ADDED: 0, REMOVED: 0, CHANGED: 0, SAME: 0}
    for d in diffs:
        out[d.status] = out.get(d.status, 0) + 1
    return out


# --- order --------------------------------------------------------------------------------


def moved_mods(pairs: list[tuple[ModSide, ModSide]]) -> set[int]:
    """Indexes (into ``pairs``) of mods whose relative order differs.

    Pairs are taken in custom priority order; the longest run that is also increasing in
    baseline priority stays put, everything else counts as moved.
    """
    idx = [
        i
        for i in range(len(pairs))
        if pairs[i][0].priority is not None and pairs[i][1].priority is not None
    ]
    idx.sort(key=lambda i: pairs[i][0].priority)
    return {idx[k] for k in out_of_order([pairs[i][1].priority for i in idx])}


# --- scan ---------------------------------------------------------------------------------


def scan(
    custom: Instance,
    baseline: Instance,
    ml: Modlist,
    cache: HashCache,
) -> ScanResult:
    """Metadata scan of every mod; hashes only same-size files whose mtime differs."""
    t0 = time.perf_counter()
    timings = {}
    cprof = read_profile(custom.profile_dir)
    bprof = read_profile(baseline.profile_dir)
    cs = custom_sides(custom, cprof)
    bs = baseline_sides(baseline, bprof, ml)
    pairs = match_mods(cs, bs)
    timings["read+match"] = time.perf_counter() - t0

    results: list[ModComparison] = []
    to_hash: list[tuple[str, int, int]] = []
    for c, b, how in pairs:
        if b is None:
            results.append(ModComparison(ONLY_CUSTOM, c, None))
            continue
        if c is None:
            results.append(ModComparison(ONLY_BASELINE, None, b))
            continue
        cfiles = walk_files(c.folder) if c.folder else {}
        bfiles = walk_files(b.folder) if b.folder else {}
        diffs = compare_files(cfiles, bfiles, ml.mods.get(b.name, []))
        to_hash += pending_files(diffs)
        results.append(
            ModComparison(
                "",
                c,
                b,
                match=how,
                version_note=version_difference(c, b),
                files=diffs,
                separator_changed=(c.separator or "").lower()
                != (b.separator or "").lower(),
                enabled_changed=c.in_modlist
                and b.in_modlist
                and c.enabled != b.enabled,
            )
        )
    timings["walk"] = time.perf_counter() - t0 - timings["read+match"]

    t1 = time.perf_counter()
    hashes = cache.hash_many(to_hash)
    timings["hash"] = time.perf_counter() - t1

    matched = [r for r in results if r.custom and r.baseline]
    moved = moved_mods([(r.custom, r.baseline) for r in matched])
    for i, r in enumerate(matched):
        r.order_moved = i in moved
        resolve_pending(r.files, hashes)
        r.file_counts = file_counts(r.files)
        r.files = [d for d in r.files if d.status != SAME]
        if r.version_note:
            r.status = DIFFERENT_VERSION
        elif r.files:
            r.status = CHANGED_FILES
        else:
            r.status = IDENTICAL
    timings["total"] = time.perf_counter() - t0
    return ScanResult(mods=results, hashed_files=len(to_hash), timings=timings)


def mod_file_diff(
    custom_folder: Path | None,
    baseline_folder: Path | None,
    directives: list[FileDirective],
    cache: HashCache,
) -> list[FileDiff]:
    """Full per-file comparison of one mod (every file present on both sides is hashed)."""
    cfiles = walk_files(custom_folder) if custom_folder else {}
    bfiles = walk_files(baseline_folder) if baseline_folder else {}
    diffs = compare_files(cfiles, bfiles, directives, full=True)
    resolve_pending(diffs, cache.hash_many(pending_files(diffs)))
    return diffs
