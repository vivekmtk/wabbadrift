"""Reader for an MO2 profile folder."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .mo2ini import IniFile, read_ini

# MO2 keeps copies such as ``modlist.txt.2026_02_02_13_26_45``; they are ignored.
BACKUP_SUFFIX = re.compile(r"\.\d{4}_\d{2}_\d{2}_\d{2}_\d{2}_\d{2}$")
SEPARATOR_SUFFIX = "_separator"


def is_backup(name: str) -> bool:
    return bool(BACKUP_SUFFIX.search(name))


def _lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return [ln.rstrip("\r") for ln in text.split("\n") if ln.strip()]


@dataclass
class ModEntry:
    """One ``modlist.txt`` line.

    ``priority`` follows the MO2 UI: 0 is the lowest priority (bottom of the file).
    ``separator`` is the separator the mod sits under, or None above the first one.
    """

    name: str
    enabled: bool
    priority: int
    separator: str | None = None
    unmanaged: bool = False

    @property
    def is_separator(self) -> bool:
        return self.name.endswith(SEPARATOR_SUFFIX)

    @property
    def display_name(self) -> str:
        if self.is_separator:
            return self.name[: -len(SEPARATOR_SUFFIX)]
        return self.name


def parse_modlist(lines: list[str]) -> list[ModEntry]:
    """Parse modlist.txt lines into entries ordered by priority (lowest first)."""
    raw = []
    for line in lines:
        if line.startswith("#") or len(line) < 2:
            continue
        flag, name = line[0], line[1:]
        if flag not in "+-*":
            continue
        raw.append((flag, name))
    entries: list[ModEntry] = []
    separator = None
    for priority, (flag, name) in enumerate(reversed(raw)):
        entry = ModEntry(
            name=name,
            enabled=flag in "+*",
            priority=priority,
            unmanaged=flag == "*",
        )
        if entry.is_separator:
            separator = name
        else:
            entry.separator = separator
        entries.append(entry)
    return entries


@dataclass
class PluginEntry:
    name: str
    enabled: bool


def parse_plugins(lines: list[str]) -> list[PluginEntry]:
    out = []
    for line in lines:
        if line.startswith("#"):
            continue
        if line.startswith("*"):
            out.append(PluginEntry(line[1:], True))
        else:
            out.append(PluginEntry(line, False))
    return out


def parse_pipe_map(lines: list[str]) -> dict[str, str]:
    """``plugin|value`` lines (plugingroups.txt, lockedorder.txt)."""
    out = {}
    for line in lines:
        if line.startswith("#") or "|" not in line:
            continue
        key, value = line.split("|", 1)
        out[key] = value
    return out


@dataclass
class Profile:
    path: Path
    mods: list[ModEntry] = field(default_factory=list)
    plugins: list[PluginEntry] = field(default_factory=list)
    loadorder: list[str] = field(default_factory=list)
    plugin_groups: dict[str, str] = field(default_factory=dict)
    locked_order: dict[str, str] = field(default_factory=dict)
    archives: list[str] = field(default_factory=list)
    settings: IniFile = field(default_factory=IniFile)

    @property
    def name(self) -> str:
        return self.path.name

    def mod(self, name: str) -> ModEntry | None:
        key = name.lower()
        for entry in self.mods:
            if entry.name.lower() == key:
                return entry
        return None

    @property
    def custom_overwrites(self) -> dict[str, str]:
        """Executable title -> mod that receives its output."""
        return self.settings.section("custom_overwrites")

    @property
    def ini_files(self) -> list[Path]:
        """Profile-local game INIs (everything ``*.ini`` except MO2's settings.ini)."""
        if not self.path.is_dir():
            return []
        return sorted(
            p
            for p in self.path.iterdir()
            if p.is_file()
            and p.suffix.lower() == ".ini"
            and p.name.lower() != "settings.ini"
        )


def read_profile(path: str | Path) -> Profile:
    path = Path(path)
    if not path.is_dir():
        raise FileNotFoundError(f"Profile folder not found: {path}")
    settings_path = path / "settings.ini"
    return Profile(
        path=path,
        mods=parse_modlist(_lines(path / "modlist.txt")),
        plugins=parse_plugins(_lines(path / "plugins.txt")),
        loadorder=[
            ln for ln in _lines(path / "loadorder.txt") if not ln.startswith("#")
        ],
        plugin_groups=parse_pipe_map(_lines(path / "plugingroups.txt")),
        locked_order=parse_pipe_map(_lines(path / "lockedorder.txt")),
        archives=[ln for ln in _lines(path / "archives.txt") if not ln.startswith("#")],
        settings=read_ini(settings_path) if settings_path.is_file() else IniFile(),
    )
