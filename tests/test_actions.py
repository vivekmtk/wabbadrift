"""Copy / replace / undo, on synthetic instances only."""

import os
from pathlib import Path

import pytest

from synth import WabbajackBuilder, write_files, write_mo2_ini, write_profile
from wabbadrift import actions, config, mods, wabbajack
from wabbadrift.hashing import HashCache


def meta(mod_id):
    return (
        f"[General]\nmodid={mod_id}\n[installedFiles]\nsize=1\n"
        f"1\\modid={mod_id}\n1\\fileid={mod_id * 10}\n"
    )


BASELINE = {
    "Alpha": ({"alpha.esp": "A"}, 1),
    "Lonely": ({"lonely.txt": "L"}, 2),
    "Beta": (
        {"beta.esp": "B", "b.ini": "[S]\nx=1\n", "sub/new.txt": "N", "keep.txt": "K"},
        3,
    ),
    "NewA": ({"newa.esp": "NA", "textures/a.dds": "TA"}, 4),
    "NewB": ({"newb.esm": "NB"}, 5),
    "Gamma": ({"gamma.esp": "G"}, 6),
}


def snapshot(root: Path) -> dict:
    """Every file's bytes and every folder under root."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for d in dirnames:
            out[Path(dirpath, d).relative_to(root).as_posix() + "/"] = None
        for f in filenames:
            p = Path(dirpath, f)
            out[p.relative_to(root).as_posix()] = p.read_bytes()
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    base, cust = tmp_path / "base", tmp_path / "custom"
    wb = WabbajackBuilder()
    for name, (files, mod_id) in BASELINE.items():
        arc = wb.nexus_archive(f"{name}.7z", mod_id, mod_id * 10)
        for rel, data in files.items():
            wb.from_archive(
                f"mods\\{name}\\{rel.replace('/', chr(92))}", data, arc, rel
            )
        wb.inline_file(f"mods\\{name}\\meta.ini", meta(mod_id))
        write_files(base / "mods" / name, {**files, "meta.ini": meta(mod_id)})
    write_profile(
        base / "profiles" / "Main",
        modlist=[
            "+Gamma",
            "+NewB",
            "+NewA",
            "+Beta",
            "-Sep2_separator",
            "+Alpha",
            "+Lonely",
            "-Sep1_separator",
        ],
        plugins=["*alpha.esp", "*beta.esp", "*newa.esp", "*newb.esm", "*gamma.esp"],
        loadorder=["alpha.esp", "beta.esp", "newa.esp", "newb.esm", "gamma.esp"],
    )
    # custom: Beta changed (b.ini), has an extra file and an extra empty folder, lacks sub/new.txt
    write_files(cust / "mods" / "Alpha", {"alpha.esp": "A", "meta.ini": meta(1)})
    write_files(
        cust / "mods" / "Beta",
        {
            "beta.esp": "B",
            "b.ini": "[S]\nx=2\n",
            "extra.txt": "E",
            "keep.txt": "K",
            "meta.ini": meta(3),
        },
    )
    (cust / "mods" / "Beta" / "empty" / "deeper").mkdir(parents=True)
    write_files(cust / "mods" / "Gamma", {"gamma.esp": "G", "meta.ini": meta(6)})
    write_files(cust / "mods" / "Mine", {"mine.esp": "M"})
    write_profile(
        cust / "profiles" / "Mine",
        modlist=[
            "+Mine",
            "+Gamma",
            "+Beta",
            "-Sep2_separator",
            "+Alpha",
            "-Sep1_separator",
        ],
        plugins=["*alpha.esp", "*beta.esp", "*gamma.esp", "*mine.esp"],
        loadorder=["alpha.esp", "beta.esp", "gamma.esp", "mine.esp"],
    )
    write_mo2_ini(base, game_path=base / "Game Root", profile="Main")
    write_mo2_ini(cust, game_path=cust / "Game", profile="Mine")
    (cust / "ModOrganizer.exe").write_bytes(b"MZ")
    wj = wb.write(tmp_path / "wj" / "list.wabbajack")
    pair = config.load_pair_config(
        {
            "name": "t",
            "custom": {"mo2_dir": str(cust)},
            "baseline": {"mo2_dir": str(base)},
            "wabbajack_file": str(wj),
            "backup_dir": str(tmp_path / "backups"),
        }
    )
    procs = []
    monkeypatch.setattr(actions, "running_executables", lambda: procs)
    cache = HashCache(None)
    scan = mods.scan(pair.custom, pair.baseline, wabbajack.load_modlist(wj), cache)
    return pair, scan.mods, cache, procs


def lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()[1:]  # without the MO2 header


def test_plan_is_a_dry_run(env):
    pair, comps, cache, _ = env
    before = snapshot(pair.custom.mo2_dir)
    p = actions.plan(pair, comps, ["NewA", "NewB", "Lonely"], ["Beta"], cache)
    assert snapshot(pair.custom.mo2_dir) == before
    by = {(m.action, m.mod): m for m in p.mods}
    assert all(m.ok for m in p.mods), [(m.mod, m.reason) for m in p.mods]
    assert by["copy", "NewA"].placement == "after Beta"
    assert by["copy", "NewB"].placement == "after NewA"
    assert by["copy", "Lonely"].placement == "before Alpha"
    assert by["copy", "NewA"].plugins == [
        {"name": "newa.esp", "placement": "after beta.esp"}
    ]
    assert by["copy", "NewB"].plugins == [
        {"name": "newb.esm", "placement": "after newa.esp"}
    ]
    beta = {(f["kind"], f["rel"]) for f in by["replace", "Beta"].files}
    assert beta == {
        ("overwrite", "Beta/b.ini"),
        ("create", "Beta/sub/new.txt"),
        ("delete", "Beta/extra.txt"),
    }
    assert {f["file"] for f in p.profile_diffs} == {
        "modlist.txt",
        "plugins.txt",
        "loadorder.txt",
    }


def test_copy_replace_then_undo_is_byte_identical(env):
    pair, comps, cache, _ = env
    custom_before = snapshot(pair.custom.mo2_dir)
    baseline_before = snapshot(pair.baseline.mo2_dir)
    p = actions.plan(pair, comps, ["NewA", "NewB", "Lonely"], ["Beta"], cache)
    j = actions.apply(
        pair, comps, ["NewA", "NewB", "Lonely"], ["Beta"], p.fingerprint, cache
    )
    assert j["status"] == "done"

    mods_dir, prof = pair.custom.mods_dir, pair.custom.profile_dir
    assert snapshot(mods_dir / "Beta") == snapshot(pair.baseline.mods_dir / "Beta")
    assert snapshot(mods_dir / "NewA") == snapshot(pair.baseline.mods_dir / "NewA")
    assert lines(prof / "modlist.txt") == [
        "+Mine",
        "+Gamma",
        "+NewB",
        "+NewA",
        "+Beta",
        "-Sep2_separator",
        "+Alpha",
        "+Lonely",
        "-Sep1_separator",
    ]
    assert lines(prof / "loadorder.txt") == [
        "alpha.esp",
        "beta.esp",
        "newa.esp",
        "newb.esm",
        "gamma.esp",
        "mine.esp",
    ]
    assert lines(prof / "plugins.txt")[2:4] == ["*newa.esp", "*newb.esm"]
    assert snapshot(pair.baseline.mo2_dir) == baseline_before

    runs = actions.list_runs(pair)
    assert [r["id"] for r in runs] == [j["id"]] and runs[0]["status"] == "done"
    actions.undo(pair, j["id"])
    assert snapshot(pair.custom.mo2_dir) == custom_before
    assert actions.list_runs(pair)[0]["status"] == "undone"
    with pytest.raises(actions.ActionError, match="already undone"):
        actions.undo(pair, j["id"])


def test_undo_refuses_when_written_files_changed(env):
    pair, comps, cache, _ = env
    p = actions.plan(pair, comps, ["NewA"], [], cache)
    j = actions.apply(pair, comps, ["NewA"], [], p.fingerprint, cache)
    (pair.custom.mods_dir / "NewA" / "newa.esp").write_text("edited", encoding="utf-8")
    with pytest.raises(actions.ActionError) as e:
        actions.undo(pair, j["id"])
    assert e.value.files == ["NewA/newa.esp (changed)"]
    assert (pair.custom.mods_dir / "NewA").is_dir()  # nothing restored


def test_apply_refuses_after_changes_and_while_mo2_runs(env):
    pair, comps, cache, procs = env
    p = actions.plan(pair, comps, [], ["Beta"], cache)
    (pair.custom.mods_dir / "Beta" / "extra.txt").write_text(
        "changed!", encoding="utf-8"
    )
    with pytest.raises(actions.ActionError, match="changed since the preview"):
        actions.apply(pair, comps, [], ["Beta"], p.fingerprint, cache)

    procs.append(str(pair.custom.mo2_dir / "ModOrganizer.exe"))
    assert actions.plan(pair, comps, [], ["Beta"], cache).mo2_running
    before = snapshot(pair.custom.mo2_dir)
    p = actions.plan(pair, comps, [], ["Beta"], cache)
    with pytest.raises(actions.ActionError, match="Mod Organizer is running"):
        actions.apply(pair, comps, [], ["Beta"], p.fingerprint, cache)
    assert snapshot(pair.custom.mo2_dir) == before
    assert actions.list_runs(pair) == []


def test_refusals_in_the_plan(env):
    pair, comps, cache, _ = env
    (pair.custom.mods_dir / "NewB").mkdir()  # an unmatched folder with the same name
    p = actions.plan(pair, comps, ["Alpha", "NewB", "Nope"], ["Alpha", "Mine"], cache)
    reasons = {(m.action, m.mod): m.reason for m in p.mods}
    assert reasons == {
        ("copy", "Alpha"): "your instance already has it as 'Alpha'; use Reset",
        ("copy", "NewB"): "a custom folder with this name exists but isn't matched",
        ("copy", "Nope"): "not in the baseline",
        ("replace", "Alpha"): "already identical",
        ("replace", "Mine"): "not in the baseline",
    }
    assert not p.ok_mods and not p.profile_diffs


def test_text_file_keeps_bom_and_crlf(tmp_path):
    f = tmp_path / "plugins.txt"
    f.write_bytes(b"\xef\xbb\xbf# header\r\n*a.esp\r\n*b.esp")
    t = actions.TextFile(f)
    t.insert(t.find("a.esp", strip="*") + 1, "*new.esp")
    assert t.content() == b"\xef\xbb\xbf# header\r\n*a.esp\r\n*new.esp\r\n*b.esp\r\n"
    assert t.find("B.ESP", strip="*") == 3
