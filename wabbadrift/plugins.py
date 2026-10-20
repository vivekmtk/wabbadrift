"""Plugin comparison: plugins.txt, loadorder.txt, MO2 plugin groups and LOOT userlist groups.

Read-only. The LOOT userlist usually lives outside both instances, so its groups are shown for
the custom side next to the baseline's delivered ``plugingroups.txt``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .order import out_of_order
from .profile import ModEntry, Profile

ONLY_BASELINE = "only_baseline"
ONLY_CUSTOM = "only_custom"
BOTH = "both"


@dataclass
class LootUserlist:
    groups: list[dict] = field(default_factory=list)  # {"name", "after": [...]}
    plugin_groups: dict[str, str] = field(default_factory=dict)  # lower name -> group


def read_loot_userlist(path: Path | None) -> LootUserlist:
    if not path or not path.is_file():
        return LootUserlist()
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
    groups = [g for g in data.get("groups") or [] if isinstance(g, dict)]
    plugin_groups = {
        p["name"].lower(): p["group"]
        for p in data.get("plugins") or []
        if isinstance(p, dict) and p.get("name") and p.get("group")
    }
    return LootUserlist(groups=groups, plugin_groups=plugin_groups)


@dataclass
class PluginDiff:
    name: str
    status: str
    baseline_enabled: bool | None = None  # None: not in plugins.txt (implicitly loaded)
    custom_enabled: bool | None = None
    baseline_index: int | None = None  # position in loadorder.txt
    custom_index: int | None = None
    order_moved: bool = False
    baseline_group: str = ""  # baseline plugingroups.txt
    custom_group: str = ""  # custom plugingroups.txt
    loot_group: str = ""  # custom LOOT userlist
    expected: str = ""  # expected-drift category, see rules.py

    @property
    def enabled_changed(self) -> bool:
        return self.status == BOTH and self.baseline_enabled != self.custom_enabled

    @property
    def group_changed(self) -> bool:
        if self.status != BOTH or not self.baseline_group:
            return False
        base = self.baseline_group.lower()
        return any(
            g and g.lower() != base for g in (self.custom_group, self.loot_group)
        )


@dataclass
class PluginReport:
    plugins: list[PluginDiff]
    locked_order: list[tuple[str, str | None, str | None]]  # plugin, baseline, custom
    loot_groups: list[dict]


def _index(profile: Profile) -> tuple[dict[str, str], dict[str, int], dict[str, bool]]:
    names: dict[str, str] = {}
    order: dict[str, int] = {}
    for i, name in enumerate(profile.loadorder):
        names.setdefault(name.lower(), name)
        order.setdefault(name.lower(), i)
    enabled = {}
    for p in profile.plugins:
        names.setdefault(p.name.lower(), p.name)
        enabled[p.name.lower()] = p.enabled
    return names, order, enabled


def compare_plugins(
    custom: Profile, baseline: Profile, loot: LootUserlist | None = None
) -> PluginReport:
    loot = loot or LootUserlist()
    c_names, c_order, c_enabled = _index(custom)
    b_names, b_order, b_enabled = _index(baseline)
    c_groups = {k.lower(): v for k, v in custom.plugin_groups.items()}
    b_groups = {k.lower(): v for k, v in baseline.plugin_groups.items()}

    diffs: dict[str, PluginDiff] = {}
    for key in list(b_names) + [k for k in c_names if k not in b_names]:
        in_b, in_c = key in b_names, key in c_names
        diffs[key] = PluginDiff(
            name=b_names.get(key) or c_names[key],
            status=BOTH if in_b and in_c else ONLY_BASELINE if in_b else ONLY_CUSTOM,
            baseline_enabled=b_enabled.get(key),
            custom_enabled=c_enabled.get(key),
            baseline_index=b_order.get(key),
            custom_index=c_order.get(key),
            baseline_group=b_groups.get(key, ""),
            custom_group=c_groups.get(key, ""),
            loot_group=loot.plugin_groups.get(key, ""),
        )

    common = sorted(
        (
            d
            for d in diffs.values()
            if d.baseline_index is not None and d.custom_index is not None
        ),
        key=lambda d: d.custom_index,
    )
    for k in out_of_order([d.baseline_index for d in common]):
        common[k].order_moved = True

    locked = []
    c_lock = {k.lower(): (k, v) for k, v in custom.locked_order.items()}
    b_lock = {k.lower(): (k, v) for k, v in baseline.locked_order.items()}
    for key in dict.fromkeys(list(b_lock) + list(c_lock)):
        bv, cv = b_lock.get(key, (None, None))[1], c_lock.get(key, (None, None))[1]
        if bv != cv:
            locked.append(((b_lock.get(key) or c_lock[key])[0], bv, cv))

    return PluginReport(
        plugins=list(diffs.values()), locked_order=locked, loot_groups=loot.groups
    )


PLUGIN_EXTENSIONS = {".esp", ".esm", ".esl"}


def plugin_owners(profile: Profile, mods_dir: Path) -> dict[str, ModEntry]:
    """Lower-cased plugin name -> the mod that provides it (enabled, highest priority wins)."""
    out: dict[str, ModEntry] = {}
    for entry in profile.mods:  # lowest priority first
        if entry.is_separator:
            continue
        folder = mods_dir / entry.name
        if not folder.is_dir():
            continue
        for p in folder.iterdir():
            if p.suffix.lower() not in PLUGIN_EXTENSIONS or not p.is_file():
                continue
            key = p.name.lower()
            if entry.enabled or key not in out or not out[key].enabled:
                out[key] = entry
    return out
