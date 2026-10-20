import json

import pytest

from synth import WabbajackBuilder, write_files, write_mo2_ini, write_profile
from wabbadrift import config, leaks


@pytest.fixture
def leak_pair(tmp_path):
    base = tmp_path / "Lists" / "Base"
    cust = tmp_path / "MO2"
    cgame = tmp_path / "Steam" / "Game"
    wjdir = tmp_path / "Wabbajack" / "4.0" / "downloaded_mod_lists"
    write_files(base / "Game Root", {"SkyrimSE.exe": b"", "skse64_loader.exe": b""})
    write_files(base / "tools" / "xEdit", {"xEdit.exe": b""})
    write_files(base / "mods" / "Shared", {"x.txt": b""})
    write_files(cgame, {"SkyrimSE.exe": b"", "Data/Skyrim.esm": b""})
    write_files(cust / "tools" / "xEdit", {"xEdit.exe": b""})
    write_files(cust / "mods" / "Shared", {"x.txt": b""})
    write_files(cust / "mods" / "Output", {"keep.txt": b""})
    write_mo2_ini(base, game_path=base / "Game Root", profile="Main", spaced=True)
    write_profile(base / "profiles" / "Main", modlist=[])
    exes = [
        # fine: everything in the custom instance
        {
            "title": "SKSE",
            "binary": (cgame / "SkyrimSE.exe").as_posix(),
            "workingDirectory": str(cgame),
        },
        # binary in the baseline game root, working directory in the custom game root
        {
            "title": "Skyrim",
            "binary": (base / "Game Root" / "SkyrimSE.exe").as_posix(),
            "workingDirectory": str(cgame),
        },
        # binary in the baseline tools; arguments point to the custom game (fine)
        {
            "title": "xEdit",
            "binary": (base / "tools" / "xEdit" / "xEdit.exe").as_posix(),
            "arguments": f'-D:"{cgame / "Data"}" -IKnowWhatImDoing',
        },
        # arguments point into the baseline and a Wabbajack temp folder
        {
            "title": "Tool",
            "binary": (cust / "tools" / "xEdit" / "xEdit.exe").as_posix(),
            "arguments": f'-o "{base / "mods" / "Shared"}" -t "{base / "__temp__" / "x"}"',
        },
        # binary doesn't exist
        {"title": "Gone", "binary": (tmp_path / "nowhere" / "gone.exe").as_posix()},
    ]
    write_mo2_ini(cust, game_path=cgame, profile="Mine", executables=exes)
    write_profile(
        cust / "profiles" / "Mine",
        modlist=["+Output", "+Shared"],
        settings="[custom_overwrites]\nxEdit=Output\nTool=No%20Such%20Mod\n",
        extra_files={
            "skyrimprefs.ini": f"[Launcher]\nsPath={base / 'Game Root'}\n[Display]\niSize=1\n",
        },
    )
    write_files(
        cust / "mods" / "BodySlide" / "CalienteTools" / "BodySlide",
        {
            "Config.xml": f"<Config><GameDataPath>{base / 'Game Root' / 'Data'}\\</GameDataPath>"
            "</Config>"
        },
    )
    write_files(
        cust / "mods" / "SomeMod" / "SKSE" / "Plugins",
        {
            "cfg.json": json.dumps(
                {"out": str(tmp_path / "missing" / "dir"), "ok": str(cgame)}
            )
        },
    )
    wj = WabbajackBuilder().write(wjdir / "list.wabbajack")
    pair = config.load_pair_config(
        {
            "name": "t",
            "custom": {"mo2_dir": str(cust)},
            "baseline": {"mo2_dir": str(base)},
            "wabbajack_file": str(wj),
        }
    )
    return pair, base, cust, cgame


def test_leak_report(leak_pair):
    pair, base, cust, cgame = leak_pair
    found = leaks.scan_leaks(pair)
    got = {(x.category, x.section, x.key, x.kind) for x in found}

    assert (leaks.EXECUTABLE, "Skyrim", "2\\binary", leaks.BASELINE_GAME_ROOT) in got
    assert (leaks.EXECUTABLE, "xEdit", "3\\binary", leaks.BASELINE_INSTANCE) in got
    assert (leaks.EXECUTABLE, "Tool", "4\\arguments", leaks.BASELINE_INSTANCE) in got
    assert (leaks.EXECUTABLE, "Tool", "4\\arguments", leaks.WABBAJACK) in got
    assert (leaks.EXECUTABLE, "Gone", "5\\binary", leaks.MISSING) in got
    assert not any(x.section == "SKSE" for x in found)
    assert not any(x.section == "xEdit" and "arguments" in x.key for x in found)
    assert (leaks.CUSTOM_OVERWRITE, "custom_overwrites", "Tool", leaks.MISSING) in got
    assert (leaks.PROFILE_INI, "Launcher", "sPath", leaks.BASELINE_GAME_ROOT) in got
    assert (leaks.CONFIG, "", "out", leaks.MISSING) in got
    assert not any(x.key == "ok" for x in found)

    sky = next(x for x in found if x.section == "Skyrim")
    assert sky.note.startswith(
        "binary in the baseline, working directory in the custom game root"
    )
    assert sky.suggestion == str(cgame / "SkyrimSE.exe")
    assert sky.suggestion_exists

    xedit = next(x for x in found if x.section == "xEdit")
    assert xedit.suggestion == str(cust / "tools" / "xEdit" / "xEdit.exe")

    temp = [x for x in found if x.kind == leaks.WABBAJACK]
    assert temp and "__temp__" in temp[0].path

    body = [x for x in found if x.category == leaks.TOOL_CONFIG]
    assert (
        body
        and body[0].tool == "BodySlide"
        and body[0].kind == leaks.BASELINE_GAME_ROOT
    )


def test_find_paths():
    text = '-sse -D:"D:\\G\\Data" -o C:/out/dir -q, "E:\\x y\\z"'
    assert leaks.find_paths(text) == ["D:\\G\\Data", "C:/out/dir", "E:\\x y\\z"]
    assert leaks.value_paths("D:\\Steam\\Skyrim Special Edition\\Data") == [
        "D:\\Steam\\Skyrim Special Edition\\Data"
    ]
    assert leaks.value_paths("%BASE_DIR%/mods") == []


def test_extra_known_tools(leak_pair):
    pair, base, cust, _ = leak_pair
    write_files(cust / "mods" / "Thing", {"thing.cfg": f"path={base / 'tools'}\n"})
    extra = [{"name": "Thing", "globs": ["%MODS%/Thing/thing.cfg"]}]
    found = leaks.scan_leaks(pair, leaks.load_known_tools(extra))
    assert any(x.tool == "Thing" and x.kind == leaks.BASELINE_INSTANCE for x in found)


def test_generic_scan_skips_noise(leak_pair):
    pair, base, cust, _ = leak_pair
    write_files(
        cust / "mods" / "Noise",
        {
            "meta.ini": "[General]\ninstallationFile=C:/gone/archive.7z\n",
            "schema.xml": '<a xsi:schemaLocation="Q:/author/schema.xsd"/>',
            "tr.json": json.dumps(
                {"hint": "Pick a folder (e.g. C:/Captures) to save to."}
            ),
        },
    )
    found = leaks.scan_leaks(pair)
    assert not any("Noise" in x.location for x in found)
