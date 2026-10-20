import json
import shutil
import sys
from pathlib import Path

import pytest
import xxhash

from synth import WabbajackBuilder, write_files, write_mo2_ini, write_profile
from wabbadrift import config, gameroot, hashing, mo2ini, profile, wabbajack


# --- mo2ini -------------------------------------------------------------------------------


def test_unescape_handles_qt_escapes():
    assert mo2ini.unescape(r"C:\\Games\\X") == "C:\\Games\\X"
    assert mo2ini.unescape(r"-D:\"C:\\G\\Data\" -q") == '-D:"C:\\G\\Data" -q'
    assert mo2ini.unescape(r"a\nb\x41") == "a\nbA"


def test_decode_value_variants():
    assert mo2ini.decode_value(r"@ByteArray(C:\\X)") == "C:\\X"
    assert mo2ini.decode_value('"8,"') == "8,"
    assert mo2ini.decode_value(r"@Variant(\0\0\0\x43)").startswith("@Variant(")


@pytest.mark.parametrize("spaced", [False, True])
def test_mo2_settings_both_spellings(tmp_path, spaced):
    game = tmp_path / "Game Root"
    write_mo2_ini(
        tmp_path / "mo2",
        game_path=game,
        profile="Main",
        downloads=tmp_path / "dl",
        spaced=spaced,
        executables=[
            {
                "title": "Edit",
                "binary": (tmp_path / "tools" / "edit.exe").as_posix(),
                "arguments": f'-D:"{game}\\Data" -q',
                "workingDirectory": str(game),
            }
        ],
        extra="[Plugins]\nRootBuilder\\enabled=true",
    )
    s = mo2ini.read_mo2_settings(tmp_path / "mo2")
    assert s.game_name == "Skyrim Special Edition"
    assert s.selected_profile == "Main"
    assert Path(s.game_path) == game
    assert s.downloads_dir == tmp_path / "dl"
    assert s.mods_dir == tmp_path / "mo2" / "mods"
    (exe,) = s.executables
    assert exe.index == 1 and exe.title == "Edit"
    assert exe.arguments == f'-D:"{game}\\Data" -q'
    assert Path(exe.working_directory) == game
    assert s.ini.get("Plugins", "RootBuilder\\enabled") == "true"


def test_section_percent_decodes_keys():
    ini = mo2ini.parse_ini("[custom_overwrites]\nPandora%20Engine%2B=Out\n")
    assert ini.section("custom_overwrites") == {"Pandora Engine+": "Out"}


# --- profile ------------------------------------------------------------------------------


def test_modlist_priority_and_separators(tmp_path):
    write_profile(
        tmp_path / "p",
        modlist=["+Top", "-Mid", "-Group_separator", "+Bottom", "*DLC: Thing"],
        plugins=["*A.esp", "B.esp"],
        loadorder=["Skyrim.esm", "A.esp", "B.esp"],
        plugingroups={"A.esp": "Fixes"},
        settings="[custom_overwrites]\nEdit%20Tool=Output\n",
    )
    (tmp_path / "p" / "modlist.txt.2026_02_02_13_26_45").write_text("+Old\n")
    p = profile.read_profile(tmp_path / "p")
    names = [m.name for m in p.mods]
    assert names == ["DLC: Thing", "Bottom", "Group_separator", "Mid", "Top"]
    assert p.mod("dlc: thing").unmanaged
    assert p.mod("Bottom").separator is None
    assert p.mod("Mid").separator == "Group_separator" and not p.mod("Mid").enabled
    assert p.mod("Top").priority == 4
    assert p.mod("Group_separator").display_name == "Group"
    assert [(x.name, x.enabled) for x in p.plugins] == [
        ("A.esp", True),
        ("B.esp", False),
    ]
    assert p.loadorder == ["Skyrim.esm", "A.esp", "B.esp"]
    assert p.plugin_groups == {"A.esp": "Fixes"}
    assert p.custom_overwrites == {"Edit Tool": "Output"}
    assert profile.is_backup("modlist.txt.2026_02_02_13_26_45")


def test_modlist_without_header(tmp_path):
    write_profile(tmp_path / "p", modlist=["+A", "+B"], header=False)
    assert [m.name for m in profile.read_profile(tmp_path / "p").mods] == ["B", "A"]


def test_profile_ini_files(tmp_path):
    write_profile(
        tmp_path / "p",
        modlist=[],
        extra_files={"skyrim.ini": "", "SkyrimPrefs.ini": "", "notes.txt": ""},
    )
    names = [f.name for f in profile.read_profile(tmp_path / "p").ini_files]
    assert sorted(names) == ["SkyrimPrefs.ini", "skyrim.ini"]


# --- hashing ------------------------------------------------------------------------------


def test_hash_is_base64_little_endian_xxh64(tmp_path):
    data = b"hello wabbajack"
    f = tmp_path / "f.bin"
    f.write_bytes(data)
    import base64
    import struct

    expected = base64.b64encode(
        struct.pack("<Q", xxhash.xxh64_intdigest(data))
    ).decode()
    assert hashing.hash_bytes(data) == expected == hashing.hash_file(f)


# --- wabbajack ----------------------------------------------------------------------------


def build_small_list(path: Path) -> Path:
    wb = WabbajackBuilder(name="Synth", version="1.2.0")
    arc = wb.nexus_archive("ModA-100-1-0.7z", 100, 1000)
    wb.from_archive("mods\\Mod A\\a.esp", b"plugin", arc, "a.esp")
    wb.from_archive("mods\\Mod A\\textures\\t.dds", b"tex", arc, "textures\\t.dds")
    wb.inline_file(
        "mods\\Mod A\\meta.ini",
        "[General]\nmodid=100\n[installedFiles]\n1\\fileid=1000\n",
    )
    wb.from_archive("Game Root\\enb.dll", b"enb", arc, "enb.dll")
    wb.inline_file("profiles\\Main\\modlist.txt", "+Mod A\n")
    wb.from_archive("TEMP_BSA_FILES\\tid\\x.nif", b"nif", arc, "x.nif")
    wb.inline_file("ModOrganizer.ini", "[General]\n", remapped=True)
    return wb.write(path)


def test_load_modlist_groups_directives(tmp_path):
    ml = wabbajack.load_modlist(build_small_list(tmp_path / "s.wabbajack"))
    assert (ml.name, ml.version, ml.game_type) == (
        "Synth",
        "1.2.0",
        "SkyrimSpecialEdition",
    )
    assert ml.directive_count == 7
    (arc,) = ml.archives.values()
    assert arc.kind == "NexusDownloader" and (arc.mod_id, arc.file_id) == (100, 1000)
    assert sorted(f.path for f in ml.mods["Mod A"]) == [
        "a.esp",
        "meta.ini",
        "textures\\t.dds",
    ]
    assert [f.path for f in ml.game_root] == ["enb.dll"]
    assert [f.path for f in ml.profiles["Main"]] == ["modlist.txt"]
    assert [f.path for f in ml.bsa_sources["tid"]] == ["x.nif"]
    assert [f.to for f in ml.other] == ["ModOrganizer.ini"]
    assert ml.mod_archives("Mod A") == {arc.hash: 2}
    assert ml.mod_meta("Mod A").get("General", "modid") == "100"
    tex = next(f for f in ml.mods["Mod A"] if f.path.endswith(".dds"))
    assert tex.hash == hashing.hash_bytes(b"tex") and tex.archive_path == [
        "textures\\t.dds"
    ]


# --- config -------------------------------------------------------------------------------


def make_instance(root: Path, profile_name: str, spaced=False) -> Path:
    write_mo2_ini(
        root,
        game_path=root / "Game Root",
        profile=profile_name,
        downloads=root / "dl",
        spaced=spaced,
    )
    write_profile(root / "profiles" / profile_name, modlist=[])
    write_files(root / "Game Root", {"game.exe": b"x"})
    return root


def test_pair_defaults_from_ini(tmp_path):
    custom = make_instance(tmp_path / "custom", "Mine")
    base = make_instance(tmp_path / "base", "Default", spaced=True)
    wj = build_small_list(
        tmp_path / "wj" / "1.0" / "downloaded_mod_lists" / "s.wabbajack"
    )
    write_files(tmp_path / "wj" / "1.0" / "Extractors" / "windows-x64", {"7z.exe": b""})
    cfg = {
        "name": "t",
        "custom": {"mo2_dir": str(custom)},
        "baseline": {"mo2_dir": str(base), "game_root": str(tmp_path / "override")},
        "wabbajack_file": str(wj),
    }
    pairs = tmp_path / "pairs"
    pairs.mkdir()
    (pairs / "t.json").write_text(json.dumps(cfg))
    pair = config.load_pair("t", pairs)
    assert pair.custom.profile_name == "Mine"
    assert pair.custom.game_root == custom / "Game Root"
    assert pair.baseline.game_root == tmp_path / "override"
    assert pair.downloads_dir == custom / "dl"
    assert pair.backup_dir == config.DATA_DIR / "backups" / "t"
    assert pair.custom.profile_dir == custom / "profiles" / "Mine"
    assert pair.sevenzip_exe is not None


def test_find_7z_prefers_configured(tmp_path):
    exe = tmp_path / "7z.exe"
    exe.write_bytes(b"")
    assert config.find_7z(str(exe), None) == exe
    assert config.find_7z(str(tmp_path / "missing.exe"), None) is None


def test_list_game_version_from_game_file_archives(tmp_path):
    def game_file(version, game="SkyrimSpecialEdition"):
        state = {"$type": "GameFileSourceDownloader, Wabbajack.Lib"}
        return {
            "Hash": version + game,
            "Name": "x",
            "Size": 1,
            "State": {**state, "Game": game, "GameVersion": version},
        }

    data = {
        "Name": "L",
        "GameType": "SkyrimSpecialEdition",
        "Archives": [
            game_file("1.7.104.0"),
            game_file("1.7.104.0"),
            game_file("1.6.1170.0"),
            game_file("9.9", "Fallout4"),
        ],
        "Directives": [],
    }
    assert (
        wabbajack.parse_modlist(data, tmp_path / "l.wabbajack").game_version
        == "1.7.104.0"
    )
    data["Archives"] = []
    assert wabbajack.parse_modlist(data, tmp_path / "l.wabbajack").game_version == ""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows version resources")
def test_game_version_reads_the_main_executable(tmp_path):
    rules = ["SkyrimSE.exe", "SkyrimSELauncher.exe", "skse64_*"]
    assert gameroot.game_version(tmp_path, rules) == ""  # no executable
    shutil.copy(sys.executable, tmp_path / "SkyrimSE.exe")
    expected = gameroot.exe_version(Path(sys.executable))
    assert expected.count(".") == 3
    assert gameroot.game_version(tmp_path, rules) == expected
    (tmp_path / "plain.exe").write_bytes(b"MZ")
    assert gameroot.exe_version(tmp_path / "plain.exe") == ""
    assert gameroot.game_version(tmp_path, ["*.exe"]) == ""
