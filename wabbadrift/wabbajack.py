"""Reader for .wabbajack files.

A .wabbajack is a zip holding a ``modlist`` JSON entry plus inlined files named by GUID. Only
``modlist`` (and single inlined files on request) are read, in memory; the zip is never
extracted.
"""

from __future__ import annotations

import json
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .mo2ini import IniFile, parse_ini

MODS = "mods"
GAME_ROOT = "Game Root"
PROFILES = "profiles"
BSA_TEMP = "TEMP_BSA_FILES"

FROM_ARCHIVE = {"FromArchive", "PatchedFromArchive"}
INLINE = {"InlineFile", "RemappedInlineFile"}


def type_name(obj: dict) -> str:
    """``"NexusDownloader, Wabbajack.Lib"`` -> ``"NexusDownloader"``."""
    return obj.get("$type", "").split(",")[0].strip()


@dataclass
class Archive:
    hash: str
    name: str
    size: int
    kind: str  # NexusDownloader, GameFileSourceDownloader, HttpDownloader, ...
    meta: str = ""
    mod_id: int | None = None
    file_id: int | None = None
    version: str = ""
    title: str = ""
    author: str = ""
    url: str = ""
    game_file: str = ""
    game_version: str = ""

    @classmethod
    def from_json(cls, a: dict) -> "Archive":
        st = a.get("State", {})
        return cls(
            hash=a["Hash"],
            name=a.get("Name", ""),
            size=a.get("Size", 0),
            kind=type_name(st),
            meta=a.get("Meta", ""),
            mod_id=st.get("ModID"),
            file_id=st.get("FileID"),
            version=str(st.get("Version", "") or ""),
            title=st.get("Name", "") or "",
            author=st.get("Author", "") or "",
            url=st.get("Url", "") or "",
            game_file=st.get("GameFile", "") or "",
            game_version=st.get("GameVersion", "") or "",
        )


@dataclass
class FileDirective:
    """One installed file. ``path`` is relative to its group (mod folder, game root...)."""

    path: str
    to: str
    kind: str
    hash: str
    size: int
    archive_hash: str | None = None
    archive_path: list[str] = field(default_factory=list)
    source_data_id: str | None = None
    temp_id: str | None = None  # CreateBSA only

    @classmethod
    def from_json(cls, d: dict, path: str) -> "FileDirective":
        ahp = d.get("ArchiveHashPath") or []
        return cls(
            path=path,
            to=d["To"],
            kind=type_name(d),
            hash=d.get("Hash", ""),
            size=d.get("Size", 0),
            archive_hash=ahp[0] if ahp else None,
            archive_path=list(ahp[1:]),
            source_data_id=d.get("SourceDataID"),
            temp_id=d.get("TempID"),
        )


@dataclass
class Modlist:
    path: Path
    name: str = ""
    version: str = ""
    author: str = ""
    game_type: str = ""
    wabbajack_version: str = ""
    game_version: str = ""  # of the game files the list was built from
    archives: dict[str, Archive] = field(default_factory=dict)
    # mod folder name -> files inside it (path relative to the mod folder)
    mods: dict[str, list[FileDirective]] = field(default_factory=dict)
    game_root: list[FileDirective] = field(default_factory=list)
    # profile name -> files inside it
    profiles: dict[str, list[FileDirective]] = field(default_factory=dict)
    # CreateBSA TempID -> source files that go into the BSA
    bsa_sources: dict[str, list[FileDirective]] = field(default_factory=dict)
    # everything else (MO2's own files, tools\, ...), path relative to the install root
    other: list[FileDirective] = field(default_factory=list)
    directive_count: int = 0
    # inlined files kept in a snapshot (SourceDataID -> bytes); None reads the .wabbajack zip
    inline_data: dict[str, bytes] | None = field(default=None, repr=False)

    def read_inline(self, source_data_id: str) -> bytes:
        return self.read_inlines([source_data_id])[source_data_id]

    def read_inlines(self, ids: list[str]) -> dict[str, bytes]:
        """Several inlined files, reading the zip once. A snapshot only has some of them."""
        if self.inline_data is not None:
            return {i: self.inline_data[i] for i in ids if i in self.inline_data}
        with zipfile.ZipFile(self.path) as z:
            return {i: z.read(i) for i in ids}

    def mod_meta(self, mod: str) -> IniFile | None:
        """The mod's meta.ini as delivered in the .wabbajack (an InlineFile)."""
        return self.mod_metas([mod]).get(mod)

    def mod_metas(self, mods: list[str]) -> dict[str, IniFile]:
        """meta.ini of several mods, reading the zip once."""
        ids = {}
        for mod in mods:
            for f in self.mods.get(mod, []):
                if f.path.lower() == "meta.ini" and f.source_data_id:
                    ids[mod] = f.source_data_id
        if not ids:
            return {}
        data = self.read_inlines(list(ids.values()))
        return {
            mod: parse_ini(data[sid].decode("utf-8-sig", errors="replace"))
            for mod, sid in ids.items()
            if sid in data
        }

    def mod_archives(self, mod: str) -> dict[str, int]:
        """Archive hash -> number of files the mod takes from it (most first)."""
        counts: dict[str, int] = {}
        for f in self.mods.get(mod, []):
            if f.archive_hash:
                counts[f.archive_hash] = counts.get(f.archive_hash, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _split(to: str) -> tuple[str, str]:
    head, _, rest = to.partition("\\")
    return head, rest


def parse_modlist(data: dict, path: Path) -> Modlist:
    ml = Modlist(
        path=path,
        name=data.get("Name", ""),
        version=str(data.get("Version", "")),
        author=data.get("Author", ""),
        game_type=data.get("GameType", ""),
        wabbajack_version=str(data.get("WabbajackVersion", "")),
    )
    game_versions: Counter[str] = Counter()
    for a in data.get("Archives", []):
        arc = Archive.from_json(a)
        ml.archives[arc.hash] = arc
        state = a.get("State") or {}
        if state.get("GameVersion") and state.get("Game") == ml.game_type:
            game_versions[str(state["GameVersion"])] += 1
    if game_versions:  # the game files the list was built from
        ml.game_version = game_versions.most_common(1)[0][0]
    directives = data.get("Directives", [])
    ml.directive_count = len(directives)
    for d in directives:
        to = d["To"]
        head, rest = _split(to)
        if head == MODS and rest:
            mod, _, inner = rest.partition("\\")
            ml.mods.setdefault(mod, []).append(FileDirective.from_json(d, inner))
        elif head == GAME_ROOT and rest:
            ml.game_root.append(FileDirective.from_json(d, rest))
        elif head == PROFILES and rest:
            prof, _, inner = rest.partition("\\")
            ml.profiles.setdefault(prof, []).append(FileDirective.from_json(d, inner))
        elif head == BSA_TEMP and rest:
            temp_id, _, inner = rest.partition("\\")
            ml.bsa_sources.setdefault(temp_id, []).append(
                FileDirective.from_json(d, inner)
            )
        else:
            ml.other.append(FileDirective.from_json(d, to))
    return ml


def load_modlist(path: str | Path) -> Modlist:
    path = Path(path)
    with zipfile.ZipFile(path) as z:
        data = json.loads(z.read("modlist"))
    return parse_modlist(data, path)
