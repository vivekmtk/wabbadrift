"""Reader for Qt-style INI files written by MO2 (ModOrganizer.ini, settings.ini, meta.ini).

MO2 writes ``key=value``; Wabbajack writes ``key = value``. Values may be quoted or wrapped in
``@ByteArray(...)`` / ``@Variant(...)`` and use backslash escapes. Keys may be percent-encoded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

_HEX = "0123456789abcdefABCDEF"
_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "a": "\a", "f": "\f"}


def unescape(s: str) -> str:
    """Undo Qt INI backslash escaping (``\\\\``, ``\\"``, ``\\n``, ``\\xHH``...)."""
    if "\\" not in s:
        return s
    out = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c != "\\" or i + 1 == n:
            out.append(c)
            i += 1
            continue
        nxt = s[i + 1]
        if nxt == "x":
            j = i + 2
            while j < n and s[j] in _HEX and j - (i + 2) < 4:
                j += 1
            if j > i + 2:
                out.append(chr(int(s[i + 2 : j], 16)))
                i = j
                continue
            out.append("x")
        elif nxt in _SIMPLE_ESCAPES:
            out.append(_SIMPLE_ESCAPES[nxt])
        else:
            out.append(nxt)
        i += 2
    return "".join(out)


def decode_value(raw: str) -> str:
    """Turn a raw INI value into the string MO2 means by it.

    ``@Variant(...)`` values are binary blobs; they are returned unchanged.
    """
    v = raw.strip()
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        v = v[1:-1]
    if v.startswith("@ByteArray(") and v.endswith(")"):
        return unescape(v[len("@ByteArray(") : -1])
    if v.startswith("@Variant("):
        return v
    return unescape(v)


@dataclass
class IniFile:
    """Sections in file order; each maps the raw key to the raw value."""

    sections: dict[str, dict[str, str]] = field(default_factory=dict)

    def raw(self, section: str, key: str, default: str | None = None) -> str | None:
        return self.sections.get(section, {}).get(key, default)

    def get(self, section: str, key: str, default: str | None = None) -> str | None:
        raw = self.raw(section, key)
        return default if raw is None else decode_value(raw)

    def section(self, section: str) -> dict[str, str]:
        """Decoded keys (percent-decoded) and values of one section."""
        return {
            unquote(k): decode_value(v)
            for k, v in self.sections.get(section, {}).items()
        }


def parse_ini(text: str) -> IniFile:
    ini = IniFile()
    current = ini.sections.setdefault("General", {})
    for line in text.splitlines():
        s = line.strip()
        if not s or s[0] in ";#":
            continue
        if s[0] == "[" and s[-1] == "]":
            current = ini.sections.setdefault(s[1:-1], {})
            continue
        if "=" not in s:
            continue
        key, value = s.split("=", 1)
        current[key.strip()] = value.strip()
    return ini


def read_ini(path: str | Path) -> IniFile:
    return parse_ini(Path(path).read_text(encoding="utf-8-sig", errors="replace"))


_EXE_KEY = re.compile(r"^(\d+)\\(.+)$")


@dataclass
class Executable:
    index: int
    title: str = ""
    binary: str = ""
    working_directory: str = ""
    arguments: str = ""
    extra: dict[str, str] = field(default_factory=dict)


@dataclass
class Mo2Settings:
    """The parts of ModOrganizer.ini Wabbadrift uses."""

    ini: IniFile
    mo2_dir: Path

    @property
    def game_name(self) -> str:
        return self.ini.get("General", "gameName", "") or ""

    @property
    def game_path(self) -> str:
        return self.ini.get("General", "gamePath", "") or ""

    @property
    def selected_profile(self) -> str:
        return self.ini.get("General", "selected_profile", "") or ""

    @property
    def base_dir(self) -> Path:
        base = self.ini.get("Settings", "base_directory")
        return Path(base) if base else self.mo2_dir

    def _dir_setting(self, key: str, default: str) -> Path:
        value = self.ini.get("Settings", key)
        if not value:
            return self.base_dir / default
        value = value.replace("%BASE_DIR%", str(self.base_dir))
        return Path(value)

    @property
    def mods_dir(self) -> Path:
        return self._dir_setting("mod_directory", "mods")

    @property
    def profiles_dir(self) -> Path:
        return self._dir_setting("profiles_directory", "profiles")

    @property
    def overwrite_dir(self) -> Path:
        return self._dir_setting("overwrite_directory", "overwrite")

    @property
    def downloads_dir(self) -> Path:
        return self._dir_setting("download_directory", "downloads")

    @property
    def executables(self) -> list[Executable]:
        by_index: dict[int, Executable] = {}
        for key, raw in self.ini.sections.get("customExecutables", {}).items():
            m = _EXE_KEY.match(key)
            if not m:
                continue
            idx, name = int(m.group(1)), m.group(2)
            exe = by_index.setdefault(idx, Executable(index=idx))
            value = decode_value(raw)
            if name == "title":
                exe.title = value
            elif name == "binary":
                exe.binary = value
            elif name == "workingDirectory":
                exe.working_directory = value
            elif name == "arguments":
                exe.arguments = value
            else:
                exe.extra[name] = value
        return [by_index[i] for i in sorted(by_index)]


def read_mo2_settings(mo2_dir: str | Path) -> Mo2Settings:
    mo2_dir = Path(mo2_dir)
    return Mo2Settings(ini=read_ini(mo2_dir / "ModOrganizer.ini"), mo2_dir=mo2_dir)
