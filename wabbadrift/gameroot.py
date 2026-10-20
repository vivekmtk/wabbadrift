"""Game root comparison.

Each file in either game root is checked against the .wabbajack ``Game Root\\`` directive for
that path when there is one, and against the installed baseline game root otherwise. Files that
a RootBuilder ``root/`` folder in an enabled mod provides are annotated, since RootBuilder only
copies them into the game root while the game runs.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path

from .hashing import HashCache
from .mods import ADDED, CHANGED, PENDING, REMOVED, SAME, FileDiff, walk_files
from .profile import Profile
from .wabbajack import FileDirective, Modlist

ROOT_FOLDER = "root"


def exe_version(path: Path) -> str:
    """File version of a Windows executable ("1.6.1170.0"), or "" if it has none."""
    try:
        ver = ctypes.WinDLL("version")
    except (AttributeError, OSError):  # not Windows
        return ""
    ver.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, wintypes.LPDWORD]
    ver.GetFileVersionInfoW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    ver.VerQueryValueW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.UINT),
    ]
    size = ver.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return ""
    buf = ctypes.create_string_buffer(size)
    info, n = ctypes.c_void_p(), wintypes.UINT()
    if not ver.GetFileVersionInfoW(str(path), 0, size, buf) or not ver.VerQueryValueW(
        buf, "\\", ctypes.byref(info), ctypes.byref(n)
    ):
        return ""
    if n.value < 16:
        return ""
    # VS_FIXEDFILEINFO: signature, struct version, file version MS, file version LS, ...
    fixed = ctypes.cast(info, ctypes.POINTER(ctypes.c_uint32 * 4)).contents
    ms, ls = fixed[2], fixed[3]
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def game_version(game_root: Path, version_files: list[str]) -> str:
    """Version of the game's executable: the first exact .exe name among the game-version files
    of the game's rules (the main executable comes first there)."""
    for name in version_files:
        if name.lower().endswith(".exe") and not any(c in name for c in "*?["):
            exe = game_root / name
            return exe_version(exe) if exe.is_file() else ""
    return ""


@dataclass
class GameRootFile(FileDiff):
    source: str = ""  # vanilla game file / mod archive / inline / install
    custom_root_mod: str = ""  # enabled custom mod whose root/ provides this path
    baseline_root_mod: str = ""
    baseline_abs: str | None = field(default=None, repr=False)
    baseline_mtime: int | None = field(default=None, repr=False)


def root_builder_files(profile: Profile, mods_dir: Path) -> dict[str, str]:
    """Lower-cased game-root path -> enabled mod that ships it in a ``root/`` folder.

    Higher-priority mods win, as in MO2.
    """
    out: dict[str, str] = {}
    for entry in profile.mods:  # lowest priority first; later entries overwrite
        if entry.is_separator or not entry.enabled:
            continue
        mod_dir = mods_dir / entry.name
        if not mod_dir.is_dir():
            continue
        root = next(
            (
                p
                for p in mod_dir.iterdir()
                if p.is_dir() and p.name.lower() == ROOT_FOLDER
            ),
            None,
        )
        if root is None:
            continue
        for key in walk_files(root):
            out[key] = entry.name
    return out


def _source(d: FileDirective, ml: Modlist) -> str:
    if d.kind == "CreateBSA":
        return "built BSA"
    if d.source_data_id:
        return "inline"
    arc = ml.archives.get(d.archive_hash or "")
    if arc and arc.kind == "GameFileSourceDownloader":
        return "vanilla game file"
    return "mod archive"


def compare_game_roots(
    custom_root: Path,
    baseline_root: Path,
    ml: Modlist,
    cache: HashCache,
    custom_rootbuilder: dict[str, str] | None = None,
    baseline_rootbuilder: dict[str, str] | None = None,
) -> list[GameRootFile]:
    """Every path in either root, with differences and annotations. ``SAME`` entries included."""
    cfiles = walk_files(custom_root)
    bfiles = walk_files(baseline_root)
    directives = {d.path.lower(): d for d in ml.game_root}
    crb = custom_rootbuilder or {}
    brb = baseline_rootbuilder or {}

    out: list[GameRootFile] = []
    for key in dict.fromkeys(list(directives) + list(bfiles) + list(cfiles)):
        d, c, b = directives.get(key), cfiles.get(key), bfiles.get(key)
        f = GameRootFile(
            path=d.path if d else (b or c)[0][-len(key) :],
            status=PENDING,
            source=_source(d, ml) if d else "install",
            custom_root_mod=crb.get(key, ""),
            baseline_root_mod=brb.get(key, ""),
        )
        if c:
            f.abs_path, f.custom_size, f.mtime_ns = c
        if b:
            f.baseline_abs, f.baseline_size, f.baseline_mtime = b
        if d:
            f.baseline_hash = d.hash
            if d.kind != "CreateBSA":
                f.baseline_size = d.size

        if c is None:
            f.status = REMOVED
        elif d is None and b is None:
            f.status = ADDED
        elif f.custom_size != f.baseline_size and (d is None or d.kind != "CreateBSA"):
            f.status, f.reason = CHANGED, "size"
        elif b and b[1] == c[1] and b[2] == c[2]:
            f.status, f.reason = SAME, "size+mtime"
        out.append(f)

    pending = [f for f in out if f.status == PENDING]
    todo = [(f.abs_path, f.custom_size, f.mtime_ns) for f in pending]
    # without a directive, the baseline file has to be hashed too
    todo += [
        (f.baseline_abs, f.baseline_size, f.baseline_mtime)
        for f in pending
        if not f.baseline_hash and f.baseline_abs
    ]
    hashes = cache.hash_many(todo)
    for f in pending:
        f.custom_hash = hashes.get(f.abs_path)
        if not f.baseline_hash:
            f.baseline_hash = hashes.get(f.baseline_abs)
        f.status = SAME if f.custom_hash == f.baseline_hash else CHANGED
        f.reason = "hash"
    return out
