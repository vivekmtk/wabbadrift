"""Pair configs and instance path resolution.

A pair config is ``data/pairs/<name>.json``. Relative paths in it are resolved against the
project root. Game roots, downloads and the profile default to what each instance's
ModOrganizer.ini says when the config leaves them empty.
"""

from __future__ import annotations

import json
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .mo2ini import Mo2Settings, read_mo2_settings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
PAIRS_DIR = DATA_DIR / "pairs"


def _resolve(value: str | None) -> Path | None:
    if not value:
        return None
    p = Path(value)
    return p if p.is_absolute() else PROJECT_ROOT / p


@dataclass
class Instance:
    mo2_dir: Path
    profile_name: str
    game_root: Path
    settings: Mo2Settings

    @property
    def profile_dir(self) -> Path:
        return self.settings.profiles_dir / self.profile_name

    @property
    def mods_dir(self) -> Path:
        return self.settings.mods_dir

    @property
    def overwrite_dir(self) -> Path:
        return self.settings.overwrite_dir


def load_instance(cfg: dict) -> Instance:
    mo2_dir = Path(cfg["mo2_dir"])
    settings = read_mo2_settings(mo2_dir)
    profile = cfg.get("profile") or settings.selected_profile
    game_root = cfg.get("game_root") or settings.game_path
    if not profile:
        raise ValueError(f"No profile configured or selected in {mo2_dir}")
    if not game_root:
        raise ValueError(f"No game root configured or set in {mo2_dir}")
    return Instance(
        mo2_dir=mo2_dir,
        profile_name=profile,
        game_root=Path(game_root),
        settings=settings,
    )


def find_7z(configured: str | None, wabbajack_file: Path | None) -> Path | None:
    """Configured path, else a 7-Zip install, else Wabbajack's bundled 7z.exe."""
    if configured:
        p = Path(configured)
        return p if p.is_file() else None
    candidates = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env)
        if base:
            candidates.append(Path(base) / "7-Zip" / "7z.exe")
    if wabbajack_file:
        # <Wabbajack>\<version>\downloaded_mod_lists\<list>.wabbajack
        version_dir = wabbajack_file.parent.parent
        bundled = Path("Extractors") / "windows-x64" / "7z.exe"
        candidates.append(version_dir / bundled)
        if version_dir.parent.is_dir():
            for sibling in sorted(version_dir.parent.iterdir(), reverse=True):
                candidates.append(sibling / bundled)
    return next((c for c in candidates if c.is_file()), None)


@dataclass
class Pair:
    name: str
    custom: Instance
    baseline: Instance
    wabbajack_file: Path
    downloads_dir: Path
    loot_userlist: Path | None
    backup_dir: Path
    sevenzip_exe: Path | None
    rules_file: Path


def load_pair_config(cfg: dict) -> Pair:
    name = cfg["name"]
    custom = load_instance(cfg["custom"])
    baseline = load_instance(cfg["baseline"])
    wj = Path(cfg["wabbajack_file"])
    downloads = _resolve(cfg.get("downloads_dir")) or custom.settings.downloads_dir
    return Pair(
        name=name,
        custom=custom,
        baseline=baseline,
        wabbajack_file=wj,
        downloads_dir=downloads,
        loot_userlist=_resolve(cfg.get("loot_userlist")),
        backup_dir=_resolve(cfg.get("backup_dir")) or DATA_DIR / "backups" / name,
        sevenzip_exe=find_7z(cfg.get("sevenzip_exe"), wj),
        rules_file=_resolve(cfg.get("rules_file")) or PAIRS_DIR / f"{name}.rules.yaml",
    )


def load_pair(name: str, pairs_dir: Path = PAIRS_DIR) -> Pair:
    cfg = json.loads((pairs_dir / f"{name}.json").read_text(encoding="utf-8"))
    return load_pair_config(cfg)


# --- setting up a pair from the UI ---------------------------------------------------------

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
SIDES = {"custom": "Your instance", "baseline": "Baseline instance"}


def user_path(value) -> Path | None:
    """A path typed or pasted by the user (surrounding quotes allowed); must be absolute."""
    s = str(value or "").strip().strip('"').strip()
    if not s:
        return None
    p = Path(s)
    if not p.is_absolute():
        raise ValueError(f"Enter a full path, like D:\\... (got '{s}').")
    return p


def list_profiles(settings: Mo2Settings) -> list[str]:
    d = settings.profiles_dir
    if not d.is_dir():
        return []
    return sorted((p.name for p in d.iterdir() if p.is_dir()), key=str.lower)


def inspect_instance(value) -> dict:
    """What the setup form shows for an MO2 folder: game, profiles, detected paths."""
    mo2_dir = user_path(value)
    if mo2_dir is None:
        raise ValueError("Enter the MO2 folder.")
    if not (mo2_dir / "ModOrganizer.ini").is_file():
        raise ValueError(
            "No ModOrganizer.ini in this folder. Enter the MO2 folder that holds "
            "ModOrganizer.exe and ModOrganizer.ini."
        )
    s = read_mo2_settings(mo2_dir)
    profiles = list_profiles(s)
    if not profiles:
        raise ValueError(f"No profiles in {s.profiles_dir}.")
    return {
        "mo2_dir": str(mo2_dir),
        "game": s.game_name,
        "game_root": s.game_path,
        "profiles": profiles,
        "selected_profile": s.selected_profile,
        "mods_dir": str(s.mods_dir),
        "downloads_dir": str(s.downloads_dir),
    }


def inspect_wabbajack(value) -> dict:
    """Name, version and game of a .wabbajack file."""
    from .wabbajack import load_modlist

    p = user_path(value)
    if p is None or p.suffix.lower() != ".wabbajack" or not p.is_file():
        raise ValueError("Enter the path of a .wabbajack file.")
    try:
        ml = load_modlist(p)
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as e:
        raise ValueError(f"Can't read this .wabbajack file ({e}).") from e
    return {
        "wabbajack_file": str(p),
        "name": ml.name,
        "version": ml.version,
        "game": ml.game_type,
    }


def pair_form(name: str, pairs_dir: Path = PAIRS_DIR) -> dict:
    """A pair config as the setup form edits it (paths as stored, the backup folder resolved)."""
    cfg = json.loads((pairs_dir / f"{name}.json").read_text(encoding="utf-8"))

    def side(s: dict) -> dict:
        return {k: s.get(k) or "" for k in ("mo2_dir", "profile", "game_root")}

    return {
        "name": name,
        "custom": side(cfg.get("custom") or {}),
        "baseline": side(cfg.get("baseline") or {}),
        "wabbajack_file": cfg.get("wabbajack_file") or "",
        "downloads_dir": str(_resolve(cfg.get("downloads_dir")) or ""),
        "loot_userlist": str(_resolve(cfg.get("loot_userlist")) or ""),
        "backup_dir": str(_resolve(cfg.get("backup_dir")) or ""),
    }


def _inside(path: Path, folder: Path) -> bool:
    path, folder = path.resolve(), folder.resolve()
    return path == folder or folder in path.parents


def save_pair(form: dict, create: bool, pairs_dir: Path = PAIRS_DIR) -> dict:
    """Check everything the setup form sent, then write data/pairs/<name>.json.

    Editing keeps keys the form doesn't show (rules file, 7-Zip path).
    """
    name = str(form.get("name") or "")
    if not NAME_RE.match(name):
        raise ValueError(
            "Name: letters, digits, - and _ only (no spaces), up to 40 characters."
        )
    path = pairs_dir / f"{name}.json"
    if create and path.exists():
        raise ValueError(f"A comparison named '{name}' already exists.")
    if not create and not path.is_file():
        raise LookupError(f"unknown pair: {name}")
    cfg = {} if create else json.loads(path.read_text(encoding="utf-8"))

    for key, label in SIDES.items():
        f = form.get(key) or {}
        try:
            info = inspect_instance(f.get("mo2_dir"))
            profile = str(f.get("profile") or "")
            if profile not in info["profiles"]:
                raise ValueError("choose a profile.")
            game_root = user_path(f.get("game_root"))
            if game_root and not game_root.is_dir():
                raise ValueError(f"game folder {game_root} doesn't exist.")
            if not game_root and not info["game_root"]:
                raise ValueError("ModOrganizer.ini has no game folder; enter it.")
        except ValueError as e:
            raise ValueError(f"{label}: {e}") from e
        cfg[key] = {
            **(cfg.get(key) or {}),
            "mo2_dir": info["mo2_dir"],
            "profile": profile,
            "game_root": str(game_root) if game_root else None,
        }
    custom, baseline = Path(cfg["custom"]["mo2_dir"]), Path(cfg["baseline"]["mo2_dir"])
    if _inside(custom, baseline) or _inside(baseline, custom):
        raise ValueError(
            "The two instances must be separate folders, not the same or one inside "
            "the other."
        )
    cfg["wabbajack_file"] = inspect_wabbajack(form.get("wabbajack_file"))[
        "wabbajack_file"
    ]

    downloads = user_path(form.get("downloads_dir"))
    if downloads and not downloads.is_dir():
        raise ValueError(f"Downloads folder {downloads} doesn't exist.")
    loot = user_path(form.get("loot_userlist"))
    if loot and not loot.is_file():
        raise ValueError(f"LOOT userlist {loot} doesn't exist.")
    backup = user_path(form.get("backup_dir"))
    if backup and (_inside(backup, custom) or _inside(backup, baseline)):
        raise ValueError("The backup folder can't be inside either instance.")
    cfg.update(
        name=name,
        downloads_dir=str(downloads) if downloads else None,
        loot_userlist=str(loot) if loot else None,
        backup_dir=str(backup) if backup else None,
    )
    load_pair_config(cfg)  # resolves everything the scan needs, or raises

    pairs_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return {"name": name}


def delete_pair(name: str, pairs_dir: Path = PAIRS_DIR) -> None:
    """Only the pair config; caches, snapshots and backups (the undo history) stay."""
    path = pairs_dir / f"{name}.json"
    if not NAME_RE.match(name) or not path.is_file():
        raise LookupError(f"unknown pair: {name}")
    path.unlink()
