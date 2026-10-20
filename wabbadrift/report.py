"""Everything the UI shows for a pair, as JSON-ready data.

The server keeps one ``PairState`` per pair: the parsed .wabbajack, the rules and the last scan,
so opening a mod, a FOMOD report or an update comparison doesn't rescan. All work on a pair runs
under its lock (the hash cache is shared and not thread-safe).
"""

from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path

from . import actions, configs, drift, fomod, gameroot, leaks, mods, plugins, rules
from .config import DATA_DIR, Pair
from .hashing import HashCache
from .profile import read_profile
from .wabbajack import load_modlist


def plain(obj):
    """Dataclasses (without their repr=False fields), paths and sets as JSON-ready values."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            f.name: plain(getattr(obj, f.name))
            for f in dataclasses.fields(obj)
            if f.repr
        }
    if isinstance(obj, dict):
        return {str(k): plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [plain(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


def _side(s: mods.ModSide | None) -> dict | None:
    if s is None:
        return None
    return {
        "name": s.name,
        "in_modlist": s.in_modlist,
        "enabled": s.enabled,
        "priority": s.priority,
        "separator": s.separator or "",
        "version": s.nexus.version,
        "file": s.nexus.installation_file,
        "mod_id": s.nexus.mod_id,
        "has_folder": s.folder is not None,
    }


def mod_row(m: mods.ModComparison) -> dict:
    return {
        "name": m.name,
        "status": m.status,
        "match": m.match,
        "version_note": m.version_note,
        "expected": m.expected,
        "moved": m.order_moved,
        "separator_changed": m.separator_changed,
        "enabled_changed": m.enabled_changed,
        "files": {
            k: m.file_counts.get(k, 0) for k in (mods.ADDED, mods.REMOVED, mods.CHANGED)
        },
        "expected_files": sum(bool(d.expected) for d in m.files),
        "custom": _side(m.custom),
        "baseline": _side(m.baseline),
    }


class PairState:
    def __init__(self, pair: Pair):
        self.pair = pair
        self.lock = threading.Lock()
        self.cache = HashCache(DATA_DIR / "cache" / f"{pair.name}.sqlite")
        self.ml = None
        self.rules = None
        self.scan: mods.ScanResult | None = None
        self.versions_differ = False
        self.report: dict | None = None
        self.fomods: list[dict] | None = None

    @property
    def snapshots_dir(self) -> Path:
        return DATA_DIR / "snapshots" / self.pair.name

    # --- the scan --------------------------------------------------------------------------

    def run_scan(self) -> dict:
        """Metadata scan of everything; replaces the previous one."""
        t0 = time.perf_counter()
        p = self.pair
        self.ml = ml = load_modlist(p.wabbajack_file)
        self.rules = r = rules.rules_for_pair(p, ml)
        cprof, bprof = read_profile(p.custom.profile_dir), read_profile(
            p.baseline.profile_dir
        )

        roots = gameroot.compare_game_roots(
            p.custom.game_root,
            p.baseline.game_root,
            ml,
            self.cache,
            gameroot.root_builder_files(cprof, p.custom.mods_dir),
            gameroot.root_builder_files(bprof, p.baseline.mods_dir),
        )
        rules.apply_to_game_root(roots, r)
        self.versions_differ = rules.game_versions_differ(roots, r)

        self.scan = scan = mods.scan(p.custom, p.baseline, ml, self.cache)
        rules.apply_to_mods(scan.mods, r, self.versions_differ)

        plug = plugins.compare_plugins(
            cprof, bprof, plugins.read_loot_userlist(p.loot_userlist)
        )
        rules.apply_to_plugins(
            plug,
            r,
            plugins.plugin_owners(cprof, p.custom.mods_dir),
            plugins.plugin_owners(bprof, p.baseline.mods_dir),
        )

        inis = configs.profile_ini_diffs(cprof, bprof)
        mo2 = configs.mo2_settings_diff(p.custom, p.baseline)
        overwrites = configs.custom_overwrites_diff(cprof, bprof)
        for keys in [f.keys for f in inis] + [mo2, overwrites]:
            rules.apply_to_keys(keys, r)

        found = leaks.scan_leaks(p, leaks.load_known_tools(r.known_tools))
        self.fomods = None
        self.report = {
            "pair": p.name,
            "scanned": time.time(),
            "seconds": round(time.perf_counter() - t0, 1),
            "hashed_files": scan.hashed_files,
            "game": p.custom.settings.game_name or ml.game_type,
            "list": {
                "name": ml.name,
                "version": ml.version,
                "game": ml.game_type,
                "game_version": ml.game_version,
                "file": str(p.wabbajack_file),
            },
            "custom": {
                "profile": p.custom.profile_name,
                "mo2": str(p.custom.mo2_dir),
                "game_root": str(p.custom.game_root),
                "game_version": gameroot.game_version(
                    p.custom.game_root, r.version_game_root
                ),
            },
            "baseline": {
                "profile": p.baseline.profile_name,
                "mo2": str(p.baseline.mo2_dir),
                "game_root": str(p.baseline.game_root),
                "game_version": gameroot.game_version(
                    p.baseline.game_root, r.version_game_root
                ),
            },
            "paths": {
                "downloads_dir": str(p.downloads_dir),
                "backup_dir": str(p.backup_dir),
                "loot_userlist": str(p.loot_userlist) if p.loot_userlist else "",
            },
            "mods": [mod_row(m) for m in scan.mods],
            "plugins": plain(plug),
            "game_root": [plain(f) for f in roots if f.status != mods.SAME],
            "game_versions_differ": self.versions_differ,
            "configs": {
                "profile_inis": plain(inis),
                "mo2_settings": plain(mo2),
                "executables": plain(configs.executable_diffs(p.custom, p.baseline)),
                "custom_overwrites": plain(overwrites),
                "mod_configs": [
                    {
                        "mod": c.mod,
                        "path": c.path,
                        "status": c.status,
                        "expected": c.expected,
                    }
                    for c in configs.mod_config_changes(scan.mods)
                ],
            },
            "leaks": plain(found),
        }
        return self.report

    def ensure_scan(self) -> dict:
        return self.report if self.report is not None else self.run_scan()

    def _comparison(self, name: str) -> mods.ModComparison:
        self.ensure_scan()
        for m in self.scan.mods:
            if m.name == name:
                return m
        raise KeyError(name)

    # --- one mod ---------------------------------------------------------------------------

    def mod_files(self, name: str) -> dict:
        """Full per-file comparison of one mod (every file present on both sides hashed)."""
        m = self._comparison(name)
        c, b = m.custom, m.baseline
        diffs = mods.mod_file_diff(
            c.folder if c else None,
            b.folder if b else None,
            self.ml.mods.get(b.name, []) if b else [],
            self.cache,
        )
        tagged = mods.ModComparison(m.status, c, b, files=diffs)
        rules.apply_to_mods([tagged], self.rules, self.versions_differ)
        same = sum(d.status == mods.SAME for d in diffs)
        files = [
            {**plain(d), "text": configs.is_text(d.path)}
            for d in diffs
            if d.status != mods.SAME
        ]
        return {"mod": mod_row(m), "same": same, "files": files}

    def file_diff(self, name: str, path: str) -> dict:
        """Key or line diff of one text file inside a mod (baseline -> custom)."""
        m = self._comparison(name)
        c, b = m.custom, m.baseline
        rel = Path(*path.replace("\\", "/").split("/"))
        if rel.is_absolute() or ".." in rel.parts:
            raise KeyError(path)
        cf = c.folder / rel if c and c.folder else None
        bf = b.folder / rel if b and b.folder else None
        out: dict = {"path": path, "keys": None, "lines": None}
        if cf and bf and cf.is_file() and bf.is_file() and configs.is_config(path):
            keys = configs.structured_diff(bf, cf)
            if keys is not None:
                rules.apply_to_keys(keys, self.rules)
                out["keys"] = plain(keys)
                return out
        if not (cf or bf):
            return out
        absent = (cf or bf).with_name(
            (cf or bf).name + ".absent"
        )  # a side without the mod
        out["lines"] = configs.text_diff(bf or absent, cf or absent)
        return out

    # --- FOMOD -----------------------------------------------------------------------------

    def _fomod(self, m: mods.ModComparison) -> fomod.FomodReport:
        c = m.custom
        return fomod.fomod_report(
            self.pair,
            self.ml,
            m.baseline.name,
            c.folder if c else None,
            c.nexus.installation_file if c else "",
            DATA_DIR / "cache" / "fomod",
        )

    def fomod(self, name: str) -> dict:
        m = self._comparison(name)
        if m.baseline is None:
            return {"mod": name, "baseline": None, "notes": ["not in the baseline"]}
        return plain(self._fomod(m))

    def all_fomods(self) -> list[dict]:
        """Every baseline mod installed through a FOMOD installer (archive listings cached)."""
        self.ensure_scan()
        if self.fomods is None:
            out = []
            for m in self.scan.mods:
                if m.baseline is None or not self.ml.mods.get(m.baseline.name):
                    continue
                rep = self._fomod(m)
                if rep.baseline is not None:
                    out.append(plain(rep))
            self.fomods = out
        return self.fomods

    # --- update drift ----------------------------------------------------------------------

    def snapshots(self) -> list[dict]:
        return [
            {**plain(i), "id": i.id, "file": None}
            for i in drift.list_snapshots(self.snapshots_dir)
        ]

    def take_snapshot(self, wabbajack_file: Path | None = None) -> dict:
        info = drift.take_snapshot(
            wabbajack_file or self.pair.wabbajack_file, self.snapshots_dir
        )
        return {**plain(info), "id": info.id, "file": None}

    def update_drift(self, old_id: str, new_id: str) -> dict:
        infos = {i.id: i for i in drift.list_snapshots(self.snapshots_dir)}
        if old_id not in infos or new_id not in infos:
            raise KeyError("unknown snapshot")
        old, new = drift.load_snapshot(infos[old_id]), drift.load_snapshot(
            infos[new_id]
        )
        rep = drift.compare_versions(old, new, self.pair.baseline.profile_name)
        if not rep.identical:
            drift.relate(rep, old, new, self.pair.custom, self.cache)
        return {**plain(rep), "counts": rep.counts()}

    # --- copy / replace / undo ---------------------------------------------------------

    def plan_actions(self, copy: list[str], replace: list[str]) -> dict:
        self.ensure_scan()
        p = actions.plan(self.pair, self.scan.mods, copy, replace, self.cache)
        return {**plain(p), "backup_dir": str(self.pair.backup_dir)}

    def apply_actions(
        self, copy: list[str], replace: list[str], fingerprint: str
    ) -> dict:
        self.ensure_scan()
        try:
            return actions.apply(
                self.pair, self.scan.mods, copy, replace, fingerprint, self.cache
            )
        finally:
            self.report = None  # the custom instance changed (or may have): scan again

    def runs(self) -> list[dict]:
        return actions.list_runs(self.pair)

    def undo(self, run_id: str) -> dict:
        try:
            return actions.undo(self.pair, run_id)
        finally:
            self.report = None
