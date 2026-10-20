"""Expected-drift rules.

Expected drift is still reported, but tagged with a category so the UI can hide it by default
and group it separately. Defaults live in ``defaults/rules.yaml`` (common + one section per
game); each pair gets its own editable copy on first use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

from .configs import KeyDiff
from .leaks import WHOLE_PATH
from .mods import CHANGED, ONLY_BASELINE, ONLY_CUSTOM, ModComparison
from .plugins import ONLY_CUSTOM as PLUGIN_ONLY_CUSTOM
from .plugins import PluginReport
from .profile import ModEntry

DEFAULTS_FILE = Path(__file__).parent / "defaults" / "rules.yaml"

GENERATOR = "generator_output"
GAME_VERSION = "game_version"
MACHINE = "machine_specific"
INTENTIONAL = "intentional_addition"

# MO2 gameName and Wabbajack GameType -> defaults section
GAME_KEYS = {
    "skyrim special edition": "skyrimse",
    "skyrimspecialedition": "skyrimse",
    "skyrim vr": "skyrimvr",
    "skyrimvr": "skyrimvr",
    "skyrim": "skyrim",
    "fallout 4": "fallout4",
    "fallout4": "fallout4",
    "fallout 4 vr": "fallout4vr",
    "fallout4vr": "fallout4vr",
    "new vegas": "falloutnv",
    "falloutnewvegas": "falloutnv",
    "fallout 3": "fallout3",
    "fallout3": "fallout3",
    "oblivion": "oblivion",
    "starfield": "starfield",
}


@lru_cache(maxsize=None)
def _compile(pattern: str) -> re.Pattern:
    """Glob with only ``*`` and ``?`` special; case-insensitive; ``\\`` and ``/`` equal."""
    out = []
    for ch in pattern.replace("\\", "/"):
        out.append(".*" if ch == "*" else "." if ch == "?" else re.escape(ch))
    return re.compile("".join(out), re.IGNORECASE | re.DOTALL)


def matches(patterns: list[str], text: str) -> bool:
    text = text.replace("\\", "/")
    return any(_compile(p).fullmatch(text) for p in patterns)


@dataclass
class Rules:
    game: str
    generator_mods: list[str] = field(default_factory=list)
    generator_files: list[str] = field(default_factory=list)
    generator_plugins: list[str] = field(default_factory=list)
    generator_game_root: list[str] = field(default_factory=list)
    overwrite_expected: bool = True
    version_mods: list[str] = field(default_factory=list)
    version_files: list[str] = field(default_factory=list)
    version_plugin_dlls: list[str] = field(default_factory=list)
    version_game_root: list[str] = field(default_factory=list)
    machine_keys: list[str] = field(default_factory=list)
    machine_absolute_paths: bool = True
    intentional_separators: list[str] = field(default_factory=list)
    known_tools: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Rules":
        gen = d.get("generator_outputs") or {}
        ver = d.get("game_version_files") or {}
        mach = d.get("machine_specific_ini") or {}
        intent = d.get("intentional_additions") or {}
        return cls(
            game=d.get("game", "generic"),
            generator_mods=gen.get("mods") or [],
            generator_files=gen.get("files") or [],
            generator_plugins=gen.get("plugins") or [],
            generator_game_root=gen.get("game_root") or [],
            overwrite_expected=bool(gen.get("overwrite", True)),
            version_mods=ver.get("mods") or [],
            version_files=ver.get("files") or [],
            version_plugin_dlls=ver.get("plugin_dlls") or [],
            version_game_root=ver.get("game_root") or [],
            machine_keys=mach.get("keys") or [],
            machine_absolute_paths=bool(mach.get("absolute_paths", True)),
            intentional_separators=intent.get("separators") or [],
            known_tools=d.get("known_tools") or [],
        )


def game_key(*names: str) -> str:
    for name in names:
        key = GAME_KEYS.get((name or "").strip().lower())
        if key:
            return key
    return "generic"


def _merge(base: dict, extra: dict) -> dict:
    """Dicts merge recursively, lists concatenate, scalars from ``extra`` win."""
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = out[k] + v
        else:
            out[k] = v
    return out


def default_rules_dict(game: str) -> dict:
    defaults = yaml.safe_load(DEFAULTS_FILE.read_text(encoding="utf-8"))
    games = defaults.get("games", {})
    merged = _merge(
        defaults.get("common", {}), games.get(game, games.get("generic", {}))
    )
    return {"game": game, **merged}


def load_rules(path: Path, game: str) -> Rules:
    """The pair's rules file; created from the defaults for ``game`` if it doesn't exist."""
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# Expected-drift rules for this pair. Edit freely; see wabbadrift/defaults/rules.yaml"
            "\n# for what each section means.\n"
        )
        body = yaml.safe_dump(
            default_rules_dict(game), sort_keys=False, allow_unicode=True
        )
        path.write_text(header + body, encoding="utf-8")
    return Rules.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})


# --- applying ------------------------------------------------------------------------------


def _mod_category(rules: Rules, *names: str) -> str:
    for name in names:
        if name and matches(rules.generator_mods, name):
            return GENERATOR
    for name in names:
        if name and matches(rules.version_mods, name):
            return GAME_VERSION
    return ""


def _intentional(rules: Rules, separator: str | None) -> bool:
    return bool(separator) and matches(rules.intentional_separators, separator)


def game_versions_differ(game_root_files, rules: Rules) -> bool:
    """True when a game executable listed as a game-version file differs between the roots."""
    return any(
        f.status == CHANGED
        and f.path.lower().endswith(".exe")
        and matches(rules.version_game_root, f.path)
        for f in game_root_files
    )


def apply_to_mods(
    mods: list[ModComparison], rules: Rules, game_versions_differ: bool = False
) -> None:
    """Tag mods and their files. With ``game_versions_differ``, extender plugin DLLs count as
    game-version files too."""
    version_files = rules.version_files + (
        rules.version_plugin_dlls if game_versions_differ else []
    )
    for m in mods:
        names = [s.name for s in (m.baseline, m.custom) if s]
        m.expected = _mod_category(rules, *names)
        for d in m.files:
            if m.expected:
                d.expected = m.expected
            elif matches(rules.generator_files, d.path):
                d.expected = GENERATOR
            elif matches(version_files, d.path):
                d.expected = GAME_VERSION
        if m.expected:
            continue
        if m.status == ONLY_CUSTOM and _intentional(rules, m.custom.separator):
            m.expected = INTENTIONAL
        elif m.status != ONLY_BASELINE and m.files and all(d.expected for d in m.files):
            m.expected = m.files[0].expected


def apply_to_plugins(
    report: PluginReport,
    rules: Rules,
    custom_owners: dict[str, ModEntry] | None = None,
    baseline_owners: dict[str, ModEntry] | None = None,
) -> None:
    """Owners map plugin -> providing mod (see ``plugins.plugin_owners``)."""
    custom_owners = custom_owners or {}
    baseline_owners = baseline_owners or {}
    for p in report.plugins:
        key = p.name.lower()
        owners = [o for o in (custom_owners.get(key), baseline_owners.get(key)) if o]
        if matches(rules.generator_plugins, p.name):
            p.expected = GENERATOR
        elif cat := _mod_category(rules, *(o.name for o in owners)):
            p.expected = cat
        elif p.status == PLUGIN_ONLY_CUSTOM and key in custom_owners:
            sep = custom_owners[key].separator
            sep = sep[: -len("_separator")] if sep else None
            if _intentional(rules, sep):
                p.expected = INTENTIONAL


def apply_to_game_root(files, rules: Rules) -> None:
    for f in files:
        if matches(rules.generator_game_root, f.path):
            f.expected = GENERATOR
        elif matches(rules.version_game_root, f.path):
            f.expected = GAME_VERSION


def _is_abs_path(value) -> bool:
    return isinstance(value, str) and bool(WHOLE_PATH.match(value))


def apply_to_keys(keys: list[KeyDiff], rules: Rules) -> None:
    for k in keys:
        if matches(rules.machine_keys, f"{k.section}/{k.key}"):
            k.expected = MACHINE
        elif (
            rules.machine_absolute_paths
            and _is_abs_path(k.baseline)
            and _is_abs_path(k.custom)
        ):
            k.expected = MACHINE


def rules_for_pair(pair, ml) -> Rules:
    """The pair's rules, created from the defaults for its game on first use."""
    return load_rules(
        pair.rules_file, game_key(ml.game_type, pair.custom.settings.game_name)
    )
