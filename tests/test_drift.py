import pytest

from synth import WabbajackBuilder, write_files, write_mo2_ini, write_profile
from wabbadrift import config, drift, wabbajack
from wabbadrift.hashing import HashCache


def meta(mod_id, file_id, version):
    return (
        f"[General]\nmodid={mod_id}\nversion={version}\n[installedFiles]\nsize=1\n"
        f"1\\modid={mod_id}\n1\\fileid={file_id}\n"
    )


def build_version(path, version, mods, root, modlist, plugins, loadorder, ini):
    """``mods``: name -> (files, mod id, file id)."""
    wb = WabbajackBuilder(version=version)
    for name, (files, mod_id, file_id) in mods.items():
        arc = wb.nexus_archive(f"{name}-{file_id}.7z", mod_id, file_id)
        for rel, data in files.items():
            wb.from_archive(f"mods\\{name}\\{rel}", data, arc, rel)
        wb.inline_file(f"mods\\{name}\\meta.ini", meta(mod_id, file_id, file_id))
    game = wb.nexus_archive("game.7z", 900, 900)
    for rel, data in root.items():
        wb.from_archive(f"Game Root\\{rel}", data, game, rel)
    profile = {
        "modlist.txt": "\n".join(modlist),
        "plugins.txt": "\n".join(plugins),
        "loadorder.txt": "\n".join(loadorder),
        "skyrim.ini": ini,
        "modlist.txt.2026_01_01_00_00_00": "backup, ignored",
    }
    for name, text in profile.items():
        wb.inline_file(f"profiles\\Main\\{name}", text)
    return wb.write(path)


OLD_MODS = {
    "Pull": ({"p.esp": "P1"}, 1, 10),  # updated; custom has the old version: safe
    "Done": ({"d.esp": "D1"}, 2, 20),  # updated; custom already has the new one
    "Mine": ({"m.esp": "M1", "m.ini": "x=1"}, 3, 30),  # updated; custom changed it
    "Gone": ({"g.esp": "G"}, 4, 40),  # removed; custom still has it unchanged
    "Dropped": ({"x.esp": "X"}, 5, 50),  # removed; not in custom
    "Same": ({"s.esp": "S"}, 6, 60),
}
NEW_MODS = {
    "Pull": ({"p.esp": "P2"}, 1, 11),
    "Done": ({"d.esp": "D2", "d.txt": "new"}, 2, 21),
    "Mine": ({"m.esp": "M2", "m.ini": "x=1"}, 3, 31),
    "Fresh": ({"f.esp": "F"}, 7, 70),  # added
    "Same": ({"s.esp": "S"}, 6, 60),
}


@pytest.fixture
def versions(tmp_path):
    old = build_version(
        tmp_path / "v1" / "list.wabbajack",
        "1.0",
        OLD_MODS,
        {"game.exe": "EXE1", "root.dll": "R1"},
        ["+Same", "+Dropped", "+Gone", "+Mine", "+Done", "+Pull", "-Core_separator"],
        ["*p.esp", "*d.esp", "*m.esp", "*g.esp", "*s.esp"],
        ["p.esp", "d.esp", "m.esp", "g.esp", "s.esp"],
        "[Display]\nfGamma=1.0\nbFull Screen=1\n",
    )
    new = build_version(
        tmp_path / "v2" / "list.wabbajack",
        "2.0",
        NEW_MODS,
        {"game.exe": "EXE1", "root.dll": "R2"},
        ["+Fresh", "+Same", "-Mine", "+Pull", "+Done", "-Core_separator"],
        ["*p.esp", "*d.esp", "m.esp", "*s.esp", "*f.esp"],
        ["d.esp", "p.esp", "m.esp", "s.esp", "f.esp"],
        "[Display]\nfGamma=1.2\nbFull Screen=1\n",
    )
    return old, new


@pytest.fixture
def custom(tmp_path):
    cust = tmp_path / "custom"
    mods = {
        "Pull": {"p.esp": "P1", "meta.ini": meta(1, 10, 10)},
        "Done": {"d.esp": "D2", "d.txt": "new", "meta.ini": meta(2, 21, 21)},
        "Mine": {"m.esp": "M1", "m.ini": "x=2", "meta.ini": meta(3, 30, 30)},
        "Gone": {"g.esp": "G", "meta.ini": meta(4, 40, 40)},
        "Same": {"s.esp": "S", "meta.ini": meta(6, 60, 60)},
    }
    for name, files in mods.items():
        write_files(cust / "mods" / name, files)
    write_files(cust / "Game", {"game.exe": "EXE1", "root.dll": "R1"})
    write_profile(
        cust / "profiles" / "Mine",
        modlist=["+Same", "+Gone", "+Mine", "+Done", "+Pull", "-Core_separator"],
        plugins=["*p.esp", "*d.esp", "*m.esp", "*g.esp", "*s.esp"],
        loadorder=["d.esp", "p.esp", "m.esp", "g.esp", "s.esp"],
        extra_files={"skyrim.ini": "[Display]\nfGamma=1.0\n"},
    )
    write_mo2_ini(cust, game_path=cust / "Game", profile="Mine")
    return config.load_instance({"mo2_dir": str(cust)})


def test_snapshots_dedupe_and_round_trip(versions, tmp_path):
    old, new = versions
    snaps = tmp_path / "snapshots"
    a = drift.take_snapshot(old, snaps)
    again = drift.take_snapshot(old, snaps)
    b = drift.take_snapshot(new, snaps)
    assert a.file == again.file and a.version == "1.0" and b.version == "2.0"
    assert [i.version for i in drift.list_snapshots(snaps)] == ["1.0", "2.0"]
    assert len(list(snaps.glob("*.json.gz"))) == 2

    snap = drift.load_snapshot(a)
    live = wabbajack.load_modlist(old)
    assert drift.compare_versions(snap, live).identical
    # the snapshot keeps the delivered profile files (not backups) and meta.ini
    assert set(drift.profile_texts(snap, "Main")) == {
        "modlist.txt",
        "plugins.txt",
        "loadorder.txt",
        "skyrim.ini",
    }
    assert snap.mod_meta("Pull").get("General", "modid") == "1"


def test_identical_versions(versions):
    old, _ = versions
    rep = drift.compare_versions(
        wabbajack.load_modlist(old), wabbajack.load_modlist(old)
    )
    assert rep.identical and not rep.mods


def test_compare_versions(versions):
    old, new = (wabbajack.load_modlist(v) for v in versions)
    rep = drift.compare_versions(old, new, "Main")
    assert rep.old == "Synth 1.0" and rep.new == "Synth 2.0" and not rep.identical
    mods = {m.name: m for m in rep.mods}
    assert {n: m.change for n, m in mods.items()} == {
        "Pull": drift.UPDATED,
        "Done": drift.UPDATED,
        "Mine": drift.UPDATED,
        "Gone": drift.REMOVED,
        "Dropped": drift.REMOVED,
        "Fresh": drift.ADDED,
    }
    assert [(f.path, f.change) for f in mods["Done"].files] == [
        ("d.esp", drift.CHANGED),
        ("d.txt", drift.ADDED),
    ]
    assert (mods["Pull"].old_version, mods["Pull"].new_version) == ("10", "11")
    assert [(f.path, f.change) for f in rep.game_root] == [("root.dll", drift.CHANGED)]
    # Pull and Done swapped: one of the two counts as moved
    order = {(o.kind, o.name, o.change) for o in rep.order if o.kind == "plugin"}
    assert order == {
        ("plugin", "f.esp", drift.ADDED),
        ("plugin", "g.esp", drift.REMOVED),
        ("plugin", "d.esp", drift.MOVED),
        ("plugin", "m.esp", drift.DISABLED),
    }
    mod_order = [(o.name, o.change) for o in rep.order if o.kind == "mod"]
    assert sorted(c for _, c in mod_order) == [drift.DISABLED, drift.MOVED]
    assert {n for n, c in mod_order if c == drift.MOVED} <= {"Pull", "Done"}
    assert [(i.file, i.key, i.old, i.new) for i in rep.inis] == [
        ("skyrim.ini", "fGamma", "1.0", "1.2")
    ]


def test_relate_to_custom(versions, custom):
    old, new = (wabbajack.load_modlist(v) for v in versions)
    rep = drift.compare_versions(old, new, "Main")
    drift.relate(rep, old, new, custom, HashCache(None))
    mods = {m.name: m.relation for m in rep.mods}
    assert mods == {
        "Pull": drift.SAFE,
        "Done": drift.MATCHES,
        "Mine": drift.CONFLICT,
        "Gone": drift.SAFE,
        "Dropped": drift.MATCHES,
        "Fresh": drift.SAFE,
    }
    assert rep.game_root[0].relation == drift.SAFE
    order = {(o.kind, o.change): o.relation for o in rep.order}
    assert order == {
        ("plugin", drift.ADDED): drift.SAFE,  # not in custom yet
        ("plugin", drift.REMOVED): drift.SAFE,  # custom still has it
        ("plugin", drift.MOVED): drift.MATCHES,  # custom already has the new order
        ("plugin", drift.DISABLED): drift.SAFE,
        ("mod", drift.MOVED): drift.SAFE,  # custom keeps the old order
        ("mod", drift.DISABLED): drift.SAFE,
    }
    assert rep.inis[0].relation == drift.SAFE
    mine = next(m for m in rep.mods if m.name == "Mine")
    assert "m.ini" in mine.relation_detail


def test_order_relation():
    old, new = ["a", "b", "c", "d"], ["b", "c", "a", "d"]  # a moved after b and c
    assert drift._order_relation("a", old, new, ["b", "c", "a", "d"]) == drift.MATCHES
    assert drift._order_relation("a", old, new, ["a", "b", "c", "d"]) == drift.SAFE
    assert drift._order_relation("a", old, new, ["b", "a", "c", "d"]) == drift.CONFLICT
    assert drift._order_relation("a", old, new, ["b", "c"]) == drift.NOT_IN_CUSTOM
