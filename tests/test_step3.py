import os
from pathlib import Path

from synth import WabbajackBuilder, write_files, write_mo2_ini, write_profile
from wabbadrift import config, configs, gameroot, plugins, profile, wabbajack
from wabbadrift.hashing import HashCache
from wabbadrift.mods import ADDED, CHANGED, REMOVED, SAME

# --- plugins ------------------------------------------------------------------------------


def test_compare_plugins(tmp_path):
    write_profile(
        tmp_path / "b",
        modlist=[],
        plugins=["*A.esp", "*B.esp", "*C.esp", "D.esp"],
        loadorder=["Skyrim.esm", "A.esp", "B.esp", "C.esp", "D.esp"],
        plugingroups={"A.esp": "Fixes", "B.esp": "Gameplay", "C.esp": "Late"},
    )
    (tmp_path / "b" / "lockedorder.txt").write_text("A.esp|1\n")
    write_profile(
        tmp_path / "c",
        modlist=[],
        plugins=["*a.esp", "*C.esp", "*B.esp", "*D.esp", "*Mine.esp"],
        loadorder=["Skyrim.esm", "a.esp", "C.esp", "B.esp", "D.esp", "Mine.esp"],
        plugingroups={"A.esp": "Fixes", "B.esp": "[Custom] Gameplay", "C.esp": "Late"},
    )
    userlist = tmp_path / "userlist.yaml"
    userlist.write_text(
        "groups:\n  - name: 'Late'\n    after: ['Fixes']\n"
        "plugins:\n  - name: 'C.esp'\n    group: 'Other'\n"
    )
    loot = plugins.read_loot_userlist(userlist)
    report = plugins.compare_plugins(
        profile.read_profile(tmp_path / "c"), profile.read_profile(tmp_path / "b"), loot
    )
    p = {d.name: d for d in report.plugins}
    assert p["A.esp"].status == plugins.BOTH and not p["A.esp"].group_changed
    assert p["Mine.esp"].status == plugins.ONLY_CUSTOM
    assert p["Skyrim.esm"].baseline_enabled is None  # implicit, not in plugins.txt
    assert [n for n, d in p.items() if d.enabled_changed] == ["D.esp"]
    assert [n for n, d in p.items() if d.order_moved] in (["B.esp"], ["C.esp"])
    assert p["B.esp"].group_changed  # MO2 group differs
    assert (
        p["C.esp"].group_changed and p["C.esp"].loot_group == "Other"
    )  # LOOT group differs
    assert report.locked_order == [("A.esp", "1", None)]
    assert report.loot_groups[0]["name"] == "Late"


def test_missing_userlist_is_empty():
    assert plugins.read_loot_userlist(None).plugin_groups == {}


# --- game root ----------------------------------------------------------------------------


def test_compare_game_roots(tmp_path):
    croot, broot = tmp_path / "custom_root", tmp_path / "base_root"
    wb = WabbajackBuilder()
    arc = wb.nexus_archive("enb.7z", 1, 1)
    wb.from_archive("Game Root\\d3d11.dll", b"enb-v1", arc, "d3d11.dll")
    wb.from_archive("Game Root\\Data\\Skyrim.esm", b"vanilla", arc, "Skyrim.esm")
    wb.from_archive("Game Root\\enbseries.ini", b"[A]\nx=1\n", arc, "enbseries.ini")
    ml = wabbajack.parse_modlist(wb.data, tmp_path / "x.wabbajack")
    write_files(
        broot,
        {
            "d3d11.dll": b"enb-v1",
            "Data\\Skyrim.esm": b"vanilla",
            "enbseries.ini": b"[A]\nx=1\n",
            "notes.txt": b"base",
        },
    )
    write_files(
        croot,
        {
            "d3d11.dll": b"enb-v2",
            "Data\\Skyrim.esm": b"vanilla",
            "notes.txt": b"cust",
            "skse64_loader.exe": b"skse",
        },
    )
    for p in list(broot.rglob("*")) + list(croot.rglob("*")):
        if p.is_file():
            os.utime(p, (1_600_000_000, 1_600_000_000))
    for name in ("notes.txt", "d3d11.dll"):  # same size, newer mtime -> hashed
        os.utime(croot / name, (1_700_000_000, 1_700_000_000))
    files = gameroot.compare_game_roots(
        croot,
        broot,
        ml,
        HashCache(None),
        custom_rootbuilder={"enbseries.ini": "ENB Root Mod"},
    )
    f = {x.path.lower(): x for x in files}
    assert f["d3d11.dll"].status == CHANGED and f["d3d11.dll"].reason == "hash"
    assert f["data\\skyrim.esm"].status == SAME
    assert f["enbseries.ini"].status == REMOVED
    assert f["enbseries.ini"].custom_root_mod == "ENB Root Mod"
    assert f["notes.txt"].status == CHANGED and f["notes.txt"].source == "install"
    assert f["skse64_loader.exe"].status == ADDED


def test_root_builder_files(tmp_path):
    write_files(tmp_path / "mods" / "Low" / "root", {"x.dll": b"1"})
    write_files(tmp_path / "mods" / "High" / "Root", {"x.dll": b"2", "y.ini": b""})
    write_files(tmp_path / "mods" / "Off" / "root", {"z.dll": b""})
    write_profile(tmp_path / "p", modlist=["+High", "-Off", "+Low"])
    rb = gameroot.root_builder_files(
        profile.read_profile(tmp_path / "p"), tmp_path / "mods"
    )
    assert rb == {"x.dll": "High", "y.ini": "High"}


# --- configs ------------------------------------------------------------------------------


def test_ini_diff():
    diffs = configs.ini_diff(
        "[Display]\niSize W=1920\nbFull=1\n[General]\nsLang=EN\n",
        "[display]\nisize w=2560\nbFull=1\n[Extra]\nx=1\n",
    )
    got = {(d.key.lower(), d.status, d.baseline, d.custom) for d in diffs}
    assert got == {
        ("isize w", CHANGED, "1920", "2560"),
        ("slang", REMOVED, "EN", None),
        ("x", ADDED, None, "1"),
    }


def test_structured_and_text_diff(tmp_path):
    b, c = tmp_path / "b.json", tmp_path / "c.json"
    b.write_text('{"a": {"b": 1, "c": [1, 2]}}')
    c.write_text('{"a": {"b": 2, "c": [1, 2, 3]}}')
    got = {(d.key, d.status) for d in configs.structured_diff(b, c)}
    assert got == {("a.b", CHANGED), ("a.c[2]", ADDED)}
    yb, yc = tmp_path / "b.yaml", tmp_path / "c.yaml"
    yb.write_text("x: 1\n")
    yc.write_text("x: 1\ny: 2\n")
    assert [(d.key, d.status) for d in configs.structured_diff(yb, yc)] == [
        ("y", ADDED)
    ]
    lines = configs.text_diff(yb, yc)
    assert "+y: 2" in lines
    bin_file = tmp_path / "x.bin"
    bin_file.write_bytes(b"\0\1\2")
    assert configs.text_diff(bin_file, yc) is None


def test_profile_ini_diffs(tmp_path):
    write_profile(tmp_path / "b", modlist=[], extra_files={"skyrim.ini": "[A]\nx=1\n"})
    write_profile(
        tmp_path / "c",
        modlist=[],
        extra_files={"Skyrim.ini": "[A]\nx=2\n", "custom.ini": ""},
    )
    diffs = configs.profile_ini_diffs(
        profile.read_profile(tmp_path / "c"), profile.read_profile(tmp_path / "b")
    )
    assert {(d.name.lower(), d.status) for d in diffs} == {
        ("skyrim.ini", CHANGED),
        ("custom.ini", ADDED),
    }


def make_instance(root: Path, profile_name: str, executables, spaced=False, extra=""):
    write_mo2_ini(
        root,
        game_path=root / "Game Root",
        profile=profile_name,
        downloads=root.parent / "downloads",
        executables=executables,
        spaced=spaced,
        extra=extra,
    )
    write_profile(root / "profiles" / profile_name, modlist=[])
    return config.load_instance({"mo2_dir": str(root)})


def test_executables_and_settings_normalize_instance_paths(tmp_path):
    def exes(root: Path, args_extra=""):
        return [
            {
                "title": "xEdit",
                "binary": (root / "tools" / "xEdit.exe").as_posix(),
                "arguments": f'-D:"{root / "Game Root" / "Data"}"{args_extra}',
            },
            {"title": "Only " + root.name, "binary": "C:/x.exe"},
        ]

    base = make_instance(
        tmp_path / "base",
        "Main",
        exes(tmp_path / "base"),
        spaced=True,
        extra="[Plugins]\nRootBuilder\\backup=true",
    )
    cust = make_instance(
        tmp_path / "cust",
        "Mine",
        exes(tmp_path / "cust", " -q"),
        extra="[Plugins]\nRootBuilder\\backup=false",
    )
    ed = {e.title: e for e in configs.executable_diffs(cust, base)}
    assert ed["xEdit"].status == CHANGED
    assert ed["xEdit"].field_diffs == {
        "arguments": ('-D:"%GAME_ROOT%\\Data"', '-D:"%GAME_ROOT%\\Data" -q')
    }
    assert ed["Only base"].status == REMOVED and ed["Only cust"].status == ADDED
    sd = {(d.section, d.key): d for d in configs.mo2_settings_diff(cust, base)}
    assert ("General", "gamePath") not in sd  # same once normalized
    assert ("General", "selected_profile") in sd
    assert sd[("Plugins", "RootBuilder\\backup")].custom == "false"


def test_normalize_paths_whole_components():
    tokens = [("%MO2%", "D:\\Lists\\X")]
    assert configs.normalize_paths("d:/lists/x/mods", tokens) == "%MO2%/mods"
    assert (
        configs.normalize_paths("D:\\Lists\\X2\\mods", tokens) == "D:\\Lists\\X2\\mods"
    )


def test_custom_overwrites_diff(tmp_path):
    write_profile(tmp_path / "b", modlist=[], settings="[custom_overwrites]\nA=Out\n")
    write_profile(
        tmp_path / "c", modlist=[], settings="[custom_overwrites]\nA=Mine\nB=X\n"
    )
    diffs = configs.custom_overwrites_diff(
        profile.read_profile(tmp_path / "c"), profile.read_profile(tmp_path / "b")
    )
    assert {(d.key, d.status) for d in diffs} == {("A", CHANGED), ("B", ADDED)}


def test_mod_config_changes(tmp_path):
    from wabbadrift.mods import FileDiff, ModComparison, ModSide, NexusInfo

    def side(folder):
        return ModSide("M", True, True, 0, None, NexusInfo(), folder)

    m = ModComparison(
        "changed_files",
        side(tmp_path / "c"),
        side(tmp_path / "b"),
        files=[
            FileDiff("SKSE\\Plugins\\x.ini", CHANGED),
            FileDiff("cfg.json", ADDED),
            FileDiff("a.dds", CHANGED),
        ],
    )
    got = configs.mod_config_changes([m])
    assert [(c.path, c.status) for c in got] == [
        ("SKSE\\Plugins\\x.ini", CHANGED),
        ("cfg.json", ADDED),
    ]
    assert (
        got[1].baseline_file is None
        and got[1].custom_file == tmp_path / "c" / "cfg.json"
    )
