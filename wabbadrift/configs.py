"""INI and config comparisons: profile INIs, ModOrganizer.ini, and config files inside mods."""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import Instance
from .mo2ini import Mo2Settings
from .profile import Profile

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10: TOML falls back to a text diff
    tomllib = None

ADDED = "added"  # only in custom
REMOVED = "removed"  # only in baseline
CHANGED = "changed"

CONFIG_EXTENSIONS = {".ini", ".toml", ".json", ".yaml", ".yml"}
TEXT_EXTENSIONS = CONFIG_EXTENSIONS | {
    ".txt",
    ".cfg",
    ".conf",
    ".xml",
    ".csv",
    ".log",
    ".md",
    ".psc",
    ".lua",
    ".js",
    ".html",
}
TEXT_DIFF_LIMIT = 2 * 1024 * 1024

# ModOrganizer.ini sections that only hold UI state
MO2_NOISE_SECTIONS = {
    "Geometry",
    "Widgets",
    "recentDirectories",
    "PluginPersistance",
    "Servers",  # download server stats
}
MO2_NOISE_KEYS = re.compile(
    r"(geometry|_state|windowstate|splitter|lastused|separatorcolor)", re.IGNORECASE
)


@dataclass
class KeyDiff:
    section: str
    key: str
    status: str
    baseline: object = None
    custom: object = None
    expected: str = ""  # expected-drift category, see rules.py


# --- generic parsing -----------------------------------------------------------------------


def parse_game_ini(text: str) -> dict[tuple[str, str], tuple[str, str, str]]:
    """(section, key) lower-cased -> (section, key, value). Last duplicate wins."""
    out = {}
    section = ""
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in ";#":
            continue
        if s[0] == "[" and s.endswith("]"):
            section = s[1:-1].strip()
            continue
        if "=" not in s:
            continue
        key, value = s.split("=", 1)
        key = key.strip()
        out[(section.lower(), key.lower())] = (section, key, value.strip())
    return out


def ini_diff(baseline_text: str, custom_text: str) -> list[KeyDiff]:
    b, c = parse_game_ini(baseline_text), parse_game_ini(custom_text)
    out = []
    for k in dict.fromkeys(list(b) + list(c)):
        bv, cv = b.get(k), c.get(k)
        if bv and cv and bv[2] == cv[2]:
            continue
        sec, key = (bv or cv)[:2]
        status = CHANGED if bv and cv else REMOVED if bv else ADDED
        out.append(KeyDiff(sec, key, status, bv and bv[2], cv and cv[2]))
    return out


def flatten(obj, prefix: str = "") -> dict[str, object]:
    """Nested dicts/lists -> ``a.b[0].c`` paths with scalar values."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def _load_structured(path: Path, text: str):
    ext = path.suffix.lower()
    if ext == ".json":
        return json.loads(text)
    if ext in (".yaml", ".yml"):
        return yaml.safe_load(text)
    if ext == ".toml" and tomllib is not None:
        return tomllib.loads(text)
    raise ValueError(f"no structured parser for {ext}")


def read_text(path: Path) -> str | None:
    """File text, or None when it is missing, too large or binary."""
    if not path.is_file() or path.stat().st_size > TEXT_DIFF_LIMIT:
        return None
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return None
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def structured_diff(baseline: Path, custom: Path) -> list[KeyDiff] | None:
    """Key-by-key diff of two INI/TOML/JSON/YAML files; None if either can't be parsed."""
    bt, ct = read_text(baseline), read_text(custom)
    if bt is None or ct is None:
        return None
    if custom.suffix.lower() == ".ini":
        return ini_diff(bt, ct)
    try:
        b = flatten(_load_structured(baseline, bt))
        c = flatten(_load_structured(custom, ct))
    except (ValueError, yaml.YAMLError):
        return None
    out = []
    for k in dict.fromkeys(list(b) + list(c)):
        if k in b and k in c and b[k] == c[k]:
            continue
        status = CHANGED if k in b and k in c else REMOVED if k in b else ADDED
        out.append(KeyDiff("", k, status, b.get(k), c.get(k)))
    return out


def text_diff(baseline: Path, custom: Path, context: int = 3) -> list[str] | None:
    """Unified diff (baseline -> custom); None if either side isn't diffable text."""
    bt = read_text(baseline) if baseline.exists() else ""
    ct = read_text(custom) if custom.exists() else ""
    if bt is None or ct is None:
        return None
    return list(
        difflib.unified_diff(
            bt.splitlines(),
            ct.splitlines(),
            fromfile=f"baseline/{baseline.name}",
            tofile=f"custom/{custom.name}",
            n=context,
            lineterm="",
        )
    )


def is_config(path: str) -> bool:
    return Path(path).suffix.lower() in CONFIG_EXTENSIONS


def is_text(path: str) -> bool:
    return Path(path).suffix.lower() in TEXT_EXTENSIONS


# --- profile INIs --------------------------------------------------------------------------


@dataclass
class IniFileDiff:
    name: str
    status: str  # added / removed / changed
    keys: list[KeyDiff]


def profile_ini_diffs(custom: Profile, baseline: Profile) -> list[IniFileDiff]:
    c = {p.name.lower(): p for p in custom.ini_files}
    b = {p.name.lower(): p for p in baseline.ini_files}
    out = []
    for key in dict.fromkeys(list(b) + list(c)):
        bp, cp = b.get(key), c.get(key)
        if bp and cp:
            keys = ini_diff(read_text(bp) or "", read_text(cp) or "")
            if keys:
                out.append(IniFileDiff(bp.name, CHANGED, keys))
        else:
            out.append(IniFileDiff((bp or cp).name, REMOVED if bp else ADDED, []))
    return out


# --- ModOrganizer.ini ----------------------------------------------------------------------


def path_tokens(inst: Instance) -> list[tuple[str, str]]:
    """(placeholder, path) pairs, longest path first, used to compare across instances."""
    pairs = [
        ("%GAME_ROOT%", str(inst.game_root)),
        ("%MODS%", str(inst.mods_dir)),
        ("%PROFILES%", str(inst.settings.profiles_dir)),
        ("%OVERWRITE%", str(inst.overwrite_dir)),
        ("%DOWNLOADS%", str(inst.settings.downloads_dir)),
        ("%MO2%", str(inst.mo2_dir)),
    ]
    return sorted(pairs, key=lambda p: -len(p[1]))


def normalize_paths(value: str, tokens: list[tuple[str, str]]) -> str:
    """Replace instance paths (either slash style, any case) with placeholders."""
    for token, path in tokens:
        if not path:
            continue
        pattern = re.escape(path.replace("/", "\\")).replace(r"\\", r"[\\/]+")
        pattern += r"(?![^\\/\s\"'])"  # whole path components only
        value = re.sub(pattern, lambda _m, t=token: t, value, flags=re.IGNORECASE)
    return value


@dataclass
class ExecutableDiff:
    title: str
    status: str
    field_diffs: dict[str, tuple[str, str]]  # field -> (baseline, custom), normalized


def _exe_fields(exe) -> dict[str, str]:
    return {
        "binary": exe.binary,
        "workingDirectory": exe.working_directory,
        "arguments": exe.arguments,
        **exe.extra,
    }


def executable_diffs(custom: Instance, baseline: Instance) -> list[ExecutableDiff]:
    ct, bt = path_tokens(custom), path_tokens(baseline)
    c = {e.title.lower(): e for e in custom.settings.executables}
    b = {e.title.lower(): e for e in baseline.settings.executables}
    out = []
    for key in dict.fromkeys(list(b) + list(c)):
        be, ce = b.get(key), c.get(key)
        if not (be and ce):
            out.append(ExecutableDiff((be or ce).title, REMOVED if be else ADDED, {}))
            continue
        bf, cf = _exe_fields(be), _exe_fields(ce)
        fields = {}
        for name in dict.fromkeys(list(bf) + list(cf)):
            bv = normalize_paths(bf.get(name, ""), bt)
            cv = normalize_paths(cf.get(name, ""), ct)
            if bv.lower() != cv.lower():
                fields[name] = (bv, cv)
        if fields:
            out.append(ExecutableDiff(be.title, CHANGED, fields))
    return out


def mo2_settings_diff(custom: Instance, baseline: Instance) -> list[KeyDiff]:
    """ModOrganizer.ini key diff (executables excluded), with instance paths normalized."""
    ct, bt = path_tokens(custom), path_tokens(baseline)
    out = []

    def sections(s: Mo2Settings) -> dict[str, dict[str, str]]:
        return {
            name: s.ini.section(name)
            for name in s.ini.sections
            if name not in MO2_NOISE_SECTIONS and name != "customExecutables"
        }

    b, c = sections(baseline.settings), sections(custom.settings)
    for sec in dict.fromkeys(list(b) + list(c)):
        bs, cs = b.get(sec, {}), c.get(sec, {})
        for key in dict.fromkeys(list(bs) + list(cs)):
            if MO2_NOISE_KEYS.search(key):
                continue
            bv, cv = bs.get(key), cs.get(key)
            bn = normalize_paths(bv, bt) if bv is not None else None
            cn = normalize_paths(cv, ct) if cv is not None else None
            if bn == cn:
                continue
            if bv is not None and cv is not None:
                status = CHANGED
            else:
                status = REMOVED if bv is not None else ADDED
            out.append(KeyDiff(sec, key, status, bn, cn))
    return out


def custom_overwrites_diff(custom: Profile, baseline: Profile) -> list[KeyDiff]:
    b, c = baseline.custom_overwrites, custom.custom_overwrites
    out = []
    for key in dict.fromkeys(list(b) + list(c)):
        if b.get(key) == c.get(key):
            continue
        status = CHANGED if key in b and key in c else REMOVED if key in b else ADDED
        out.append(KeyDiff("custom_overwrites", key, status, b.get(key), c.get(key)))
    return out


# --- configs inside mods -------------------------------------------------------------------


@dataclass
class ModConfigChange:
    mod: str  # baseline mod name
    path: str  # relative to the mod folder
    status: str  # added / removed / changed
    custom_file: Path | None
    baseline_file: Path | None
    expected: str = ""  # from the mod, when rules were applied to the scan


def mod_config_changes(scan_mods) -> list[ModConfigChange]:
    """Config files that differ inside matched mods (from a ``mods.scan`` result)."""
    out = []
    for m in scan_mods:
        if not (m.custom and m.baseline):
            continue
        for d in m.files:
            if not is_config(d.path):
                continue
            cf = (
                m.custom.folder / d.path
                if m.custom.folder and d.status != REMOVED
                else None
            )
            bf = (
                m.baseline.folder / d.path
                if m.baseline.folder and d.status != ADDED
                else None
            )
            expected = d.expected or m.expected
            out.append(ModConfigChange(m.name, d.path, d.status, cf, bf, expected))
    return out
