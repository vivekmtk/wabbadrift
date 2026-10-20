"""Baseline snapshots and update drift between two versions of a .wabbajack.

A snapshot keeps what a later comparison needs from one .wabbajack: the ``modlist`` JSON, the
delivered profile files, each mod's meta.ini and small inlined configs. Snapshots are gzipped,
named by content, and never written twice.

Update drift lists what changed between two versions (mods, game-root files, plugin and mod
order, profile INIs) and how each change relates to the custom instance: already matches,
safe to pull (custom still has the old version), conflicts (custom changed it), or not in
custom.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import Instance
from .configs import ini_diff, is_config, parse_game_ini
from .hashing import HashCache
from .mods import (
    ADDED,
    META_INI,
    REMOVED,
    SAME,
    ModSide,
    compare_files,
    custom_sides,
    match_mods,
    nexus_info,
    pending_files,
    resolve_pending,
    walk_files,
)
from .order import out_of_order
from .profile import is_backup, parse_modlist, parse_plugins, read_profile
from .wabbajack import FileDirective, Modlist, parse_modlist as parse_wabbajack

# change kinds
UPDATED = "updated"
MOVED = "moved"
ENABLED = "enabled"
DISABLED = "disabled"
CHANGED = "changed"
SEPARATOR = "separator"

# relation to the custom instance
MATCHES = "already_matches"
SAFE = "safe_to_pull"
CONFLICT = "conflict"
NOT_IN_CUSTOM = "not_in_custom"

INLINE_CONFIG_LIMIT = 1 << 20  # inlined configs kept in a snapshot, per file

PROFILE_FILES = {"modlist.txt", "plugins.txt", "loadorder.txt"}


# --- snapshots -----------------------------------------------------------------------------


@dataclass
class SnapshotInfo:
    file: Path
    sha: str
    name: str
    version: str
    wabbajack_version: str
    source: str
    taken: float
    directives: int

    @property
    def id(self) -> str:
        return self.file.name.removesuffix(".json.gz")


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "list"


def _kept_inline(path: str, size: int) -> bool:
    """Inlined files a snapshot keeps: delivered profile files, meta.ini, small configs."""
    head, _, rest = path.partition("\\")
    if head == "profiles":
        _, _, inner = rest.partition("\\")
        return "\\" not in inner and not is_backup(inner)
    if head == "mods":
        name = rest.rsplit("\\", 1)[-1].lower()
        return name == META_INI or (is_config(name) and size <= INLINE_CONFIG_LIMIT)
    return False


def take_snapshot(wabbajack_file: Path, snapshots_dir: Path) -> SnapshotInfo:
    """Snapshot a .wabbajack; returns the existing snapshot when the content is known."""
    with zipfile.ZipFile(wabbajack_file) as z:
        raw = z.read("modlist")
        sha = hashlib.sha256(raw).hexdigest()
        for info in list_snapshots(snapshots_dir):
            if info.sha == sha:
                return info
        data = json.loads(raw)
        inline = {
            d["SourceDataID"]: base64.b64encode(z.read(d["SourceDataID"])).decode()
            for d in data.get("Directives", [])
            if d.get("SourceDataID") and _kept_inline(d["To"], d.get("Size", 0))
        }
    name, version = data.get("Name", ""), str(data.get("Version", ""))
    stem = f"{_safe(name)}_{_safe(version)}_{sha[:12]}"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    file = snapshots_dir / f"{stem}.json.gz"
    with gzip.open(file, "wt", encoding="utf-8") as fh:
        json.dump({"modlist": data, "inline": inline}, fh)
    info = SnapshotInfo(
        file,
        sha,
        name,
        version,
        str(data.get("WabbajackVersion", "")),
        str(wabbajack_file),
        time.time(),
        len(data.get("Directives", [])),
    )
    meta = {k: v for k, v in info.__dict__.items() if k != "file"}
    (snapshots_dir / f"{stem}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return info


def list_snapshots(snapshots_dir: Path) -> list[SnapshotInfo]:
    """Oldest first."""
    out = []
    if snapshots_dir.is_dir():
        for meta in snapshots_dir.glob("*.meta.json"):
            file = meta.with_name(meta.name.removesuffix(".meta.json") + ".json.gz")
            if file.is_file():
                out.append(
                    SnapshotInfo(file, **json.loads(meta.read_text(encoding="utf-8")))
                )
    return sorted(out, key=lambda i: i.taken)


def load_snapshot(info: SnapshotInfo) -> Modlist:
    with gzip.open(info.file, "rt", encoding="utf-8") as fh:
        snap = json.load(fh)
    ml = parse_wabbajack(snap["modlist"], info.file)
    ml.inline_data = {k: base64.b64decode(v) for k, v in snap["inline"].items()}
    return ml


# --- reading a version ---------------------------------------------------------------------


def profile_name(ml: Modlist, preferred: str = "") -> str:
    """The delivered profile: the preferred one if the list has it, else the first."""
    names = {p.lower(): p for p in ml.profiles}
    return names.get(preferred.lower()) or next(iter(ml.profiles), "")


def profile_texts(ml: Modlist, profile: str) -> dict[str, str]:
    """Delivered top-level profile files (lower-case name -> text)."""
    files = {
        f.path.lower(): f.source_data_id
        for f in ml.profiles.get(profile, [])
        if f.source_data_id and "\\" not in f.path and not is_backup(f.path)
    }
    data = ml.read_inlines(list(files.values()))
    return {
        name: data[sid].decode("utf-8-sig", errors="replace")
        for name, sid in files.items()
        if sid in data
    }


def version_sides(ml: Modlist, texts: dict[str, str]) -> list[ModSide]:
    """The version's mods in its delivered modlist.txt order (plus unlisted folders)."""
    entries = parse_modlist(texts.get("modlist.txt", "").splitlines())
    names = [e.name for e in entries if not e.is_separator and e.name in ml.mods]
    metas = ml.mod_metas(list(dict.fromkeys(names + list(ml.mods))))
    sides, seen = [], set()
    for e in entries:
        # every installed mod has at least its meta.ini directive
        if e.is_separator or e.unmanaged or e.name not in ml.mods:
            continue
        seen.add(e.name.lower())
        sep = e.separator[: -len("_separator")] if e.separator else None
        sides.append(
            ModSide(
                e.name,
                True,
                e.enabled,
                e.priority,
                sep,
                nexus_info(metas.get(e.name)),
                None,
            )
        )
    for name in ml.mods:
        if name.lower() not in seen and not name.endswith("_separator"):
            sides.append(
                ModSide(
                    name, False, None, None, None, nexus_info(metas.get(name)), None
                )
            )
    return sides


def _files(directives: list[FileDirective]) -> dict[str, tuple[str, int]]:
    """Lower-case path -> (hash, size), meta.ini left out."""
    return {
        d.path.lower(): (d.hash, d.size)
        for d in directives
        if d.path.lower() != META_INI
    }


# --- report --------------------------------------------------------------------------------


@dataclass
class FileChange:
    path: str
    change: str  # added / removed / changed
    old_hash: str | None = None
    new_hash: str | None = None
    old_size: int | None = None
    new_size: int | None = None
    relation: str = ""  # game-root files only


@dataclass
class ModChange:
    name: str  # name in the new version (old name when removed)
    change: str  # added / removed / updated
    old_name: str = ""
    old_version: str = ""
    new_version: str = ""
    files: list[FileChange] = field(default_factory=list)
    relation: str = ""
    custom_name: str = ""
    relation_detail: str = ""


@dataclass
class OrderChange:
    kind: str  # plugin / mod
    name: str
    change: str  # added / removed / moved / enabled / disabled / separator
    old: str = ""
    new: str = ""
    relation: str = ""


@dataclass
class IniChange:
    file: str
    section: str
    key: str
    change: str
    old: str | None
    new: str | None
    relation: str = ""


@dataclass
class UpdateReport:
    old: str  # "<name> <version>"
    new: str
    identical: bool = False
    mods: list[ModChange] = field(default_factory=list)
    game_root: list[FileChange] = field(default_factory=list)
    order: list[OrderChange] = field(default_factory=list)
    inis: list[IniChange] = field(default_factory=list)
    profile: str = ""

    def counts(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for group, items in (
            ("mods", self.mods),
            ("game_root", self.game_root),
            ("order", self.order),
            ("inis", self.inis),
        ):
            c: dict[str, int] = {}
            for i in items:
                c[i.relation or "-"] = c.get(i.relation or "-", 0) + 1
            out[group] = c
        return out


def _file_changes(old: dict, new: dict) -> list[FileChange]:
    out = []
    for path in sorted(set(old) | set(new)):
        o, n = old.get(path), new.get(path)
        if o == n:
            continue
        change = ADDED if o is None else REMOVED if n is None else CHANGED
        out.append(
            FileChange(
                path,
                change,
                o and o[0],
                n and n[0],
                o and o[1],
                n and n[1],
            )
        )
    return out


def _version(side: ModSide) -> str:
    n = side.nexus
    return n.version or n.installation_file or ""


def _by_priority(sides: dict[str, ModSide]) -> list[str]:
    return sorted(sides, key=lambda k: sides[k].priority)


def _load_order(texts: dict[str, str]) -> list[str]:
    lines = [ln.strip().lower() for ln in texts.get("loadorder.txt", "").splitlines()]
    return [ln for ln in lines if ln and not ln.startswith("#")]


def _mod_order(texts: dict[str, str]) -> list[str]:
    entries = parse_modlist(texts.get("modlist.txt", "").splitlines())
    return [
        e.name.lower()
        for e in sorted(entries, key=lambda e: e.priority)
        if not e.is_separator
    ]


def _moved(old_order: list[str], new_order: list[str]) -> set[str]:
    """Names (lower-case) in both orders whose relative position changed."""
    pos = {n: i for i, n in enumerate(old_order)}
    common = [n for n in new_order if n in pos]
    return {common[i] for i in out_of_order([pos[n] for n in common])}


def compare_versions(
    old: Modlist, new: Modlist, preferred_profile: str = ""
) -> UpdateReport:
    rep = UpdateReport(
        f"{old.name} {old.version}".strip(), f"{new.name} {new.version}".strip()
    )
    op, np_ = profile_name(old, preferred_profile), profile_name(new, preferred_profile)
    rep.profile = np_
    ot, nt = profile_texts(old, op), profile_texts(new, np_)

    # mods: old and new matched like custom and baseline (Nexus file, name, Nexus mod)
    o_sides, n_sides = version_sides(old, ot), version_sides(new, nt)
    for o, n, _ in match_mods(o_sides, n_sides):
        if o is None:
            rep.mods.append(ModChange(n.name, ADDED, new_version=_version(n)))
        elif n is None:
            rep.mods.append(ModChange(o.name, REMOVED, old_version=_version(o)))
        else:
            files = _file_changes(
                _files(old.mods.get(o.name, [])), _files(new.mods.get(n.name, []))
            )
            ov, nv = _version(o), _version(n)
            if files or ov != nv or o.name != n.name:
                rep.mods.append(ModChange(n.name, UPDATED, o.name, ov, nv, files))

    def root(ml):
        return {d.path.lower(): (d.hash, d.size) for d in ml.game_root}

    rep.game_root = _file_changes(root(old), root(new))

    # mod order and separators (delivered modlist.txt)
    o_mods = {s.name.lower(): s for s in o_sides if s.in_modlist}
    n_mods = {s.name.lower(): s for s in n_sides if s.in_modlist}
    for name in _moved(_by_priority(o_mods), _by_priority(n_mods)):
        rep.order.append(OrderChange("mod", n_mods[name].name, MOVED))
    for name in o_mods.keys() & n_mods.keys():
        o, n = o_mods[name], n_mods[name]
        if (o.separator or "") != (n.separator or ""):
            rep.order.append(
                OrderChange(
                    "mod", n.name, SEPARATOR, o.separator or "", n.separator or ""
                )
            )
        if o.enabled != n.enabled:
            rep.order.append(
                OrderChange("mod", n.name, ENABLED if n.enabled else DISABLED)
            )

    # plugins: plugins.txt (enabled) and loadorder.txt (order)
    def plugins(texts):
        enabled = {
            p.name.lower(): p
            for p in parse_plugins(texts.get("plugins.txt", "").splitlines())
        }
        order = [
            ln.strip()
            for ln in texts.get("loadorder.txt", "").splitlines()
            if ln.strip() and not ln.startswith("#")
        ] or [p.name for p in enabled.values()]
        return enabled, order

    (oe, oo), (ne, no) = plugins(ot), plugins(nt)
    o_names = {p.lower(): p for p in oo} | {k: v.name for k, v in oe.items()}
    n_names = {p.lower(): p for p in no} | {k: v.name for k, v in ne.items()}
    for key in sorted(n_names.keys() - o_names.keys()):
        rep.order.append(OrderChange("plugin", n_names[key], ADDED))
    for key in sorted(o_names.keys() - n_names.keys()):
        rep.order.append(OrderChange("plugin", o_names[key], REMOVED))
    for key in sorted(_moved([p.lower() for p in oo], [p.lower() for p in no])):
        rep.order.append(OrderChange("plugin", n_names[key], MOVED))
    for key in sorted(oe.keys() & ne.keys()):
        if oe[key].enabled != ne[key].enabled:
            rep.order.append(
                OrderChange(
                    "plugin", n_names[key], ENABLED if ne[key].enabled else DISABLED
                )
            )

    # profile INIs
    for name in sorted(
        k for k in ot.keys() | nt.keys() if k.endswith(".ini") and k != "settings.ini"
    ):
        for k in ini_diff(ot.get(name, ""), nt.get(name, "")):
            rep.inis.append(
                IniChange(name, k.section, k.key, k.status, k.baseline, k.custom)
            )

    rep.identical = not (rep.mods or rep.game_root or rep.order or rep.inis)
    return rep


# --- relation to the custom instance -------------------------------------------------------


def _order_relation(
    name: str, old: list[str], new: list[str], custom: list[str]
) -> str:
    """For an item that moved: does custom order it like the new version, against every item
    the update put on its other side, or like the old version?"""
    o, n, c = ({x: i for i, x in enumerate(seq)} for seq in (old, new, custom))
    if name not in c:
        return NOT_IN_CUSTOM
    flipped = [
        y
        for y in n
        if y != name and y in o and y in c and (o[name] < o[y]) != (n[name] < n[y])
    ]
    as_new = all((c[name] < c[y]) == (n[name] < n[y]) for y in flipped)
    as_old = all((c[name] < c[y]) == (o[name] < o[y]) for y in flipped)
    return _relation(as_new, as_old)


def _relation(matches_new: bool, matches_old: bool) -> str:
    if matches_new:
        return MATCHES
    return SAFE if matches_old else CONFLICT


def relate(
    rep: UpdateReport,
    old: Modlist,
    new: Modlist,
    custom: Instance,
    cache: HashCache,
    game_root_cache: HashCache | None = None,
) -> None:
    """Tag every change with how it relates to the custom instance."""
    cprof = read_profile(custom.profile_dir)
    ot = profile_texts(old, profile_name(old, rep.profile))
    nt = profile_texts(new, rep.profile)
    o_sides, n_sides = version_sides(old, ot), version_sides(new, nt)
    c_sides = custom_sides(custom, cprof)
    to_new = {b.name: c for c, b, _ in match_mods(c_sides, n_sides) if c and b}
    to_old = {b.name: c for c, b, _ in match_mods(c_sides, o_sides) if c and b}

    # mods: compare the custom folder with the old and the new directives
    pending = []
    work = []
    for m in rep.mods:
        c = to_new.get(m.name) if m.change != REMOVED else None
        c = c or to_old.get(m.old_name or m.name)
        if c is None or c.folder is None:
            m.relation = MATCHES if m.change == REMOVED else NOT_IN_CUSTOM
            if m.change == ADDED:
                m.relation, m.relation_detail = SAFE, "not in custom; copy it"
            continue
        m.custom_name = c.name
        files = walk_files(c.folder)
        d_old = (
            compare_files(files, {}, old.mods.get(m.old_name or m.name, []))
            if m.change != ADDED
            else None
        )
        d_new = (
            compare_files(files, {}, new.mods.get(m.name, []))
            if m.change != REMOVED
            else None
        )
        for d in (d_old, d_new):
            if d is not None:
                pending += pending_files(d)
        work.append((m, d_old, d_new))
    hashes = cache.hash_many(pending)
    for m, d_old, d_new in work:
        differ = {}
        for label, d in (("old", d_old), ("new", d_new)):
            if d is not None:
                resolve_pending(d, hashes)
                differ[label] = [x for x in d if x.status != SAME]
        same_new = d_new is not None and not differ["new"]
        same_old = d_old is not None and not differ["old"]
        if m.change == REMOVED:
            m.relation = SAFE if same_old else CONFLICT
            m.relation_detail = (
                "custom still has it, unchanged"
                if same_old
                else f"custom has it with {len(differ['old'])} changed files"
            )
        elif m.change == ADDED:
            m.relation = MATCHES if same_new else CONFLICT
            if not same_new:
                n = len(differ["new"])
                m.relation_detail = (
                    f"custom has '{m.custom_name}' with {n} differing files"
                )
        else:
            m.relation = _relation(same_new, same_old)
            if m.relation == CONFLICT:
                changed = differ["old"]
                m.relation_detail = (
                    f"custom differs from the old version in {len(changed)} files: "
                    + ", ".join(x.path for x in changed[:3])
                )

    # game root: hash the custom file
    root_cache = game_root_cache or cache
    want = []
    for f in rep.game_root:
        p = custom.game_root / f.path
        if p.is_file():
            st = p.stat()
            want.append((str(p), st.st_size, st.st_mtime_ns))
    hashes = root_cache.hash_many(want)
    for f in rep.game_root:
        p = custom.game_root / f.path
        h = hashes.get(str(p)) if p.is_file() else None
        f.relation = _relation(h == f.new_hash, h == f.old_hash)

    # order: plugins.txt / loadorder.txt and modlist.txt of the custom profile
    c_plugins = {p.name.lower(): p for p in cprof.plugins}
    c_porder = [p.lower() for p in (cprof.loadorder or [p.name for p in cprof.plugins])]
    c_mods = {e.name.lower(): e for e in cprof.mods if not e.is_separator}
    c_morder = [
        e.name.lower() for e in sorted(c_mods.values(), key=lambda e: e.priority)
    ]

    lists = {
        "plugin": (_load_order(ot), _load_order(nt), c_porder),
        "mod": (_mod_order(ot), _mod_order(nt), c_morder),
    }
    for ch in rep.order:
        key = ch.name.lower()
        present = (
            key in (c_plugins if ch.kind == "plugin" else c_mods)
            or key in lists[ch.kind][2]
        )
        if ch.change == ADDED:
            ch.relation = MATCHES if present else SAFE
        elif ch.change == REMOVED:
            ch.relation = SAFE if present else MATCHES
        elif not present:
            ch.relation = NOT_IN_CUSTOM
        elif ch.change == MOVED:
            ch.relation = _order_relation(key, *lists[ch.kind])
        elif ch.change in (ENABLED, DISABLED):
            want_on = ch.change == ENABLED
            c_on = (
                c_plugins[key].enabled
                if ch.kind == "plugin" and key in c_plugins
                else c_mods[key].enabled if key in c_mods else None
            )
            ch.relation = _relation(c_on == want_on, c_on == (not want_on))
        elif ch.change == SEPARATOR:
            c_sep = c_mods[key].separator or ""
            c_sep = (
                c_sep[: -len("_separator")] if c_sep.endswith("_separator") else c_sep
            )
            ch.relation = _relation(c_sep == ch.new, c_sep == ch.old)

    # profile INIs: the custom profile's value
    c_inis = {}
    for f in rep.inis:
        if f.file not in c_inis:
            p = next((x for x in cprof.ini_files if x.name.lower() == f.file), None)
            c_inis[f.file] = (
                parse_game_ini(p.read_text(encoding="utf-8-sig", errors="replace"))
                if p
                else {}
            )
        cur = c_inis[f.file].get((f.section.lower(), f.key.lower()))
        value = cur[2] if cur else None
        f.relation = _relation(value == f.new, value == f.old)
