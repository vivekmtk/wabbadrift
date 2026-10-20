"""Path-leak report for the custom instance (flag only, never fix).

A value leaks when it resolves into the baseline instance or its game root, into a Wabbajack
temp/install folder, or to a path that doesn't exist.
"""

from __future__ import annotations

import json
import ntpath
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import Instance, Pair
from .configs import parse_game_ini, path_tokens, read_text
from .mods import walk_files
from .profile import read_profile

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None

BASELINE_INSTANCE = "baseline_instance"
BASELINE_GAME_ROOT = "baseline_game_root"
WABBAJACK = "wabbajack"
MISSING = "missing"

# where a value was found
EXECUTABLE = "executable"
CUSTOM_OVERWRITE = "custom_overwrite"
PROFILE_INI = "profile_ini"
MO2_SETTINGS = "mo2_settings"
TOOL_CONFIG = "tool_config"
CONFIG = "config"

KNOWN_TOOLS_FILE = Path(__file__).parent / "defaults" / "known_tools.yaml"
SCAN_EXTENSIONS = {".ini", ".json", ".yaml", ".yml", ".toml", ".xml", ".cfg"}
WABBAJACK_TEMP_SEGMENTS = {"__temp__"}

_DRIVE = r"[A-Za-z]:[\\/]"
_PATHS_IN_TEXT = re.compile(
    rf'"({_DRIVE}[^"\r\n]*)"|\'({_DRIVE}[^\'\r\n]*)\'|(?<![\w])({_DRIVE}[^"\'<>|\r\n]*)'
)
WHOLE_PATH = re.compile(rf"^\s*\"?({_DRIVE}[^\"<>|\r\n]*)\"?\s*$")
_TRAILING_JUNK = " \t,;)]}"


def find_paths(text: str) -> list[str]:
    """Absolute Windows paths inside free text (quoted, or up to the end of the line)."""
    out = []
    for m in _PATHS_IN_TEXT.finditer(text):
        p = (m.group(1) or m.group(2) or m.group(3)).rstrip(_TRAILING_JUNK)
        if m.group(3):
            # unquoted: stop at the next option of a command line (" -x", " /x")
            p = re.split(r"\s+[-/][A-Za-z]", p)[0].rstrip(_TRAILING_JUNK)
        out.append(p)
    return out


def value_paths(value: str) -> list[str]:
    """A whole value that is a path, else the paths inside it."""
    if "%" in value:
        return []
    m = WHOLE_PATH.match(value)
    if m:
        return [m.group(1).rstrip(_TRAILING_JUNK)]
    return find_paths(value)


def _norm(p: str) -> str:
    return ntpath.normcase(ntpath.normpath(p.replace("/", "\\")))


def _under(path: str, root: str) -> bool:
    p, r = _norm(path), _norm(root)
    return p == r or p.startswith(r.rstrip("\\") + "\\")


@dataclass
class Leak:
    category: str
    location: str  # file the value was found in
    section: str
    key: str
    value: str
    path: str  # the path the value points to
    kind: str
    tool: str = ""
    suggestion: str = ""
    suggestion_exists: bool = False
    note: str = ""


class Classifier:
    def __init__(self, pair: Pair):
        self.custom = pair.custom
        self.baseline = pair.baseline
        # <Wabbajack>\<version>\downloaded_mod_lists\<list>.wabbajack
        self.wabbajack_dir = str(pair.wabbajack_file.parent.parent.parent)
        bt = dict(path_tokens(pair.baseline))
        ct = dict(path_tokens(pair.custom))
        self.mapping = sorted(
            ((bt[t], ct[t]) for t in bt if bt[t] and ct.get(t)),
            key=lambda m: -len(m[0]),
        )

    def kind(self, path: str) -> str | None:
        # a Wabbajack temp folder can sit inside the baseline instance; it wins
        if {s.lower() for s in _norm(path).split("\\")} & WABBAJACK_TEMP_SEGMENTS:
            return WABBAJACK
        if _under(path, str(self.baseline.game_root)):
            return BASELINE_GAME_ROOT
        if _under(path, str(self.baseline.mo2_dir)):
            return BASELINE_INSTANCE
        if _under(path, self.wabbajack_dir):
            return WABBAJACK
        if not os.path.exists(path):
            return MISSING
        return None

    def suggest(self, path: str) -> tuple[str, bool]:
        for base, cust in self.mapping:
            if _under(path, base):
                rest = ntpath.normpath(path.replace("/", "\\"))[len(_norm(base)) :]
                s = cust.rstrip("\\/") + rest
                return s, os.path.exists(s)
        return "", False

    def check(self, category, location, section, key, value, tool="") -> list[Leak]:
        out = []
        for p in value_paths(value):
            kind = self.kind(p)
            if kind is None:
                continue
            suggestion, exists = self.suggest(p) if kind != MISSING else ("", False)
            out.append(
                Leak(
                    category,
                    location,
                    section,
                    key,
                    value,
                    p,
                    kind,
                    tool,
                    suggestion,
                    exists,
                )
            )
        return out


# --- sources ------------------------------------------------------------------------------


def _ini_values(text: str):
    for (_, _), (section, key, value) in parse_game_ini(text).items():
        yield section, key, value


def _structured_values(path: Path, text: str):
    ext = path.suffix.lower()
    if ext == ".json":
        data = json.loads(text)
    elif ext in (".yaml", ".yml"):
        data = yaml.safe_load(text)
    elif ext == ".toml" and tomllib is not None:
        data = tomllib.loads(text)
    else:
        raise ValueError(ext)

    def walk(obj, prefix):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield from walk(v, f"{prefix}.{k}" if prefix else str(k))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                yield from walk(v, f"{prefix}[{i}]")
        elif isinstance(obj, str) and WHOLE_PATH.match(obj):  # skip paths inside prose
            yield "", prefix, obj

    yield from walk(data, "")


def file_values(path: Path):
    """(section, key, value) triples from a config file; free text falls back to lines."""
    text = read_text(path)
    if text is None:
        return
    ext = path.suffix.lower()
    if ext == ".ini":
        yield from _ini_values(text)
        return
    if ext in (".json", ".yaml", ".yml", ".toml"):
        try:
            yield from _structured_values(path, text)
            return
        except (ValueError, yaml.YAMLError):  # unparseable: scan as text
            pass
    for n, line in enumerate(text.splitlines(), 1):
        if re.search(_DRIVE, line):
            yield "", f"line {n}", line.strip()


def load_known_tools(extra: list[dict] | None = None) -> list[dict]:
    tools = yaml.safe_load(KNOWN_TOOLS_FILE.read_text(encoding="utf-8")) or []
    return tools + list(extra or [])


def expand_glob(pattern: str, inst: Instance) -> list[Path]:
    tokens = {
        "%MO2%": str(inst.mo2_dir),
        "%MODS%": str(inst.mods_dir),
        "%OVERWRITE%": str(inst.overwrite_dir),
        "%PROFILE%": str(inst.profile_dir),
    }
    for token, value in tokens.items():
        pattern = pattern.replace(token, value.replace("\\", "/"))
    pattern = os.path.expandvars(pattern)
    if "%" in pattern:
        return []
    m = re.match(r"^([^*?\[]*)/(.*)$", pattern.replace("\\", "/"))
    if not m:
        return []
    base, rest = Path(m.group(1)), m.group(2)
    if not base.is_dir():
        return []
    return sorted(p for p in base.glob(rest) if p.is_file())


def scan_leaks(pair: Pair, known_tools: list[dict] | None = None) -> list[Leak]:
    cls = Classifier(pair)
    inst = pair.custom
    s = inst.settings
    ini_path = str(inst.mo2_dir / "ModOrganizer.ini")
    leaks: list[Leak] = []

    # executables
    for exe in s.executables:
        key = f"{exe.index}\\"
        found = []
        found += cls.check(EXECUTABLE, ini_path, exe.title, key + "binary", exe.binary)
        found += cls.check(
            EXECUTABLE,
            ini_path,
            exe.title,
            key + "workingDirectory",
            exe.working_directory,
        )
        found += cls.check(
            EXECUTABLE, ini_path, exe.title, key + "arguments", exe.arguments
        )
        bin_kind = next((x.kind for x in found if x.key.endswith("binary")), None)
        wd = exe.working_directory
        if bin_kind in (BASELINE_GAME_ROOT, BASELINE_INSTANCE) and wd:
            if cls.kind(wd) is None and _under(wd, str(inst.game_root)):
                for x in found:
                    if x.key.endswith("binary"):
                        x.note = "binary in the baseline, working directory in the custom game root"
        leaks += found

    # ModOrganizer.ini settings
    for section in ("General", "Settings", "Plugins"):
        for key, value in s.ini.section(section).items():
            leaks += cls.check(MO2_SETTINGS, ini_path, section, key, value)

    # profile: custom_overwrites and INIs
    prof = read_profile(inst.profile_dir)
    settings_path = str(inst.profile_dir / "settings.ini")
    for title, mod in prof.custom_overwrites.items():
        target = inst.mods_dir / mod
        if not target.is_dir():
            leaks.append(
                Leak(
                    CUSTOM_OVERWRITE,
                    settings_path,
                    "custom_overwrites",
                    title,
                    mod,
                    str(target),
                    MISSING,
                    note="target mod folder doesn't exist",
                )
            )
    for ini in prof.ini_files:
        for section, key, value in file_values(ini):
            leaks += cls.check(PROFILE_INI, str(ini), section, key, value)

    # known tool configs
    tool_files: dict[str, str] = {}
    for tool in known_tools if known_tools is not None else load_known_tools():
        for g in tool.get("globs", []):
            for f in expand_glob(g, inst):
                tool_files.setdefault(str(f).lower(), tool["name"])
                for section, key, value in file_values(f):
                    leaks += cls.check(
                        TOOL_CONFIG, str(f), section, key, value, tool["name"]
                    )

    # generic scan: config files in mods, overwrite and the profile
    for root in (inst.mods_dir, inst.overwrite_dir, inst.profile_dir):
        for _, (abs_path, size, _) in walk_files(root).items():
            p = Path(abs_path)
            if (
                p.suffix.lower() not in SCAN_EXTENSIONS
                or abs_path.lower() in tool_files
            ):
                continue
            if p.parent == inst.profile_dir and p.suffix.lower() == ".ini":
                continue  # profile INIs are covered above
            if p.name.lower() == "meta.ini":
                continue  # MO2 metadata (download history), not read by tools
            for section, key, value in file_values(p):
                for x in cls.check(CONFIG, abs_path, section, key, value):
                    # a missing path on a drive this machine doesn't have is a mod author's
                    # leftover, not part of this setup
                    if x.kind != MISSING or os.path.exists(
                        ntpath.splitdrive(x.path)[0] + "\\"
                    ):
                        leaks.append(x)
    return leaks
