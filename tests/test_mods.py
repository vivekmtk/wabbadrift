import os
import time
from pathlib import Path

import pytest

from synth import WabbajackBuilder, write_files, write_mo2_ini, write_profile
from wabbadrift import config, mods, wabbajack
from wabbadrift.hashing import HashCache

BASE_MTIME = 1_600_000_000
NEW_MTIME = 1_700_000_000


def meta(mod_id=None, file_id=None):
    text = "[General]\n"
    if mod_id:
        text += f"modid={mod_id}\n[installedFiles]\nsize=1\n1\\modid={mod_id}\n"
        text += f"1\\fileid={file_id}\n"
    return text


def set_mtime(folder: Path, mtime: int):
    for p in folder.rglob("*"):
        if p.is_file():
            os.utime(p, (mtime, mtime))


@pytest.fixture
def synth_pair(tmp_path):
    """Baseline vs custom with every status. Layout in the docstring of each mod below."""
    base, cust = tmp_path / "base", tmp_path / "custom"
    wb = WabbajackBuilder()

    def baseline_mod(name, files, mod_id=None, file_id=None):
        arc = wb.nexus_archive(f"{name}.7z", mod_id or 999, file_id or 999)
        for rel, data in files.items():
            wb.from_archive(f"mods\\{name}\\{rel}", data, arc, rel)
        wb.inline_file(f"mods\\{name}\\meta.ini", meta(mod_id, file_id))
        write_files(base / "mods" / name, {**files, "meta.ini": meta(mod_id, file_id)})
        set_mtime(base / "mods" / name, BASE_MTIME)

    def custom_mod(name, files, mod_id=None, file_id=None, mtime=BASE_MTIME):
        write_files(cust / "mods" / name, {**files, "meta.ini": meta(mod_id, file_id)})
        set_mtime(cust / "mods" / name, mtime)

    # identical, same mtimes (no hashing); moved to another separator in custom
    baseline_mod("Alpha", {"a.esp": "A", "tex\\a.dds": "AT"}, 1, 10)
    custom_mod("Alpha", {"a.esp": "A", "tex\\a.dds": "AT"}, 1, 10)
    # renamed in custom, matched by Nexus ID; b.txt changed (same size), one file added
    baseline_mod("Beta", {"b.esp": "B", "b.txt": "hello"}, 2, 20)
    custom_mod(
        "Beta Renamed", {"b.esp": "B", "b.txt": "jello", "extra.txt": "x"}, 2, 20
    )
    os.utime(cust / "mods" / "Beta Renamed" / "b.txt", (NEW_MTIME, NEW_MTIME))
    # same name, different Nexus file
    baseline_mod("Gamma", {"g.esp": "G"}, 3, 30)
    custom_mod("Gamma", {"g.esp": "G2"}, 3, 31)
    # only in baseline
    baseline_mod("Delta", {"d.esp": "D"}, 4, 40)
    # no Nexus info; identical content but newer mtimes (hashed); disabled in custom
    baseline_mod("Eps", {"e.txt": "same"})
    custom_mod("Eps", {"e.txt": "same"}, mtime=NEW_MTIME)
    # only in custom
    custom_mod("Mine", {"m.esp": "M"})

    write_profile(
        base / "profiles" / "Main",
        modlist=[
            "+Eps",
            "+Delta",
            "+Gamma",
            "-Sep2_separator",
            "+Beta",
            "+Alpha",
            "-Sep1_separator",
        ],
        header=False,
    )
    write_profile(
        cust / "profiles" / "Mine",
        modlist=[
            "+Mine",
            "+Alpha",
            "-Eps",
            "+Gamma",
            "-Sep2_separator",
            "+Beta Renamed",
            "-Sep1_separator",
        ],
    )
    write_mo2_ini(base, game_path=base / "Game Root", profile="Main", spaced=True)
    write_mo2_ini(cust, game_path=cust / "Game", profile="Mine")
    wj = wb.write(tmp_path / "list.wabbajack")
    return (
        config.load_instance({"mo2_dir": str(cust)}),
        config.load_instance({"mo2_dir": str(base)}),
        wabbajack.load_modlist(wj),
    )


def by_name(result):
    return {r.name: r for r in result.mods}


def test_scan_statuses_and_matching(synth_pair, tmp_path):
    custom, baseline, ml = synth_pair
    cache = HashCache(tmp_path / "cache.sqlite")
    r = by_name(mods.scan(custom, baseline, ml, cache))
    assert r["Alpha"].status == mods.IDENTICAL and r["Alpha"].match == "nexus"
    assert r["Beta"].status == mods.CHANGED_FILES and r["Beta"].match == "nexus"
    assert r["Beta"].custom.name == "Beta Renamed"
    assert {(d.path, d.status) for d in r["Beta"].files} == {
        ("b.txt", mods.CHANGED),
        ("extra.txt", mods.ADDED),
    }
    assert r["Gamma"].status == mods.DIFFERENT_VERSION and r["Gamma"].match == "name"
    assert r["Delta"].status == mods.ONLY_BASELINE
    assert r["Mine"].status == mods.ONLY_CUSTOM
    assert r["Eps"].status == mods.IDENTICAL and r["Eps"].file_counts[mods.SAME] == 1


def test_scan_order_separator_enabled(synth_pair, tmp_path):
    custom, baseline, ml = synth_pair
    r = by_name(mods.scan(custom, baseline, ml, HashCache(None)))
    assert [n for n, m in r.items() if m.order_moved] == ["Alpha"]
    assert r["Alpha"].separator_changed and r["Alpha"].baseline.separator == "Sep1"
    assert not r["Beta"].separator_changed
    assert [n for n, m in r.items() if m.enabled_changed] == ["Eps"]


def test_scan_hashes_only_when_needed_and_caches(synth_pair, tmp_path):
    custom, baseline, ml = synth_pair
    db = tmp_path / "cache.sqlite"
    first = mods.scan(custom, baseline, ml, HashCache(db))
    # b.txt (same size, newer mtime) and e.txt (same size, newer mtime)
    assert first.hashed_files == 2
    cache = HashCache(db)
    assert len(cache.mem) == 2
    second = mods.scan(custom, baseline, ml, cache)
    assert by_name(second)["Beta"].files == by_name(first)["Beta"].files


def test_mod_file_diff_full_hashes_everything(synth_pair, tmp_path):
    custom, baseline, ml = synth_pair
    diffs = mods.mod_file_diff(
        custom.mods_dir / "Alpha",
        baseline.mods_dir / "Alpha",
        ml.mods["Alpha"],
        HashCache(None),
    )
    assert {d.status for d in diffs} == {mods.SAME}
    assert all(d.reason == "hash" for d in diffs)


def test_moved_mods_minimal_set():
    def side(p):
        return mods.ModSide("x", True, True, p, None, mods.NexusInfo(), None)

    # custom order 0..4, baseline priorities: one mod jumped from the end to the front
    pairs = [(side(i), side(b)) for i, b in enumerate([9, 1, 2, 3, 4])]
    assert mods.moved_mods(pairs) == {0}


def test_nexus_info_reads_installed_files():
    from wabbadrift.mo2ini import parse_ini

    ini = parse_ini(
        "[General]\nmodid=5\nversion=1.2\ninstallationFile=C:/dl/x.7z\n"
        "[installedFiles]\n1\\modid=5\n1\\fileid=50\n"
    )
    info = mods.nexus_info(ini)
    assert info.files == {(5, 50)} and info.mod_id == 5
    assert (info.version, info.installation_file) == ("1.2", "x.7z")


def test_scan_of_800_mods_per_side_is_quick(tmp_path):
    """The metadata scan target: ~800 mods per side well under 2 minutes."""
    base, cust = tmp_path / "base", tmp_path / "custom"
    wb = WabbajackBuilder()
    names = [f"Mod {i:03}" for i in range(800)]
    for i, name in enumerate(names):
        files = {
            "plugin.esp": f"P{i}",
            "textures\\t.dds": f"T{i}" * 50,
            "readme.txt": "r",
        }
        arc = wb.nexus_archive(f"{name}.7z", i + 1, (i + 1) * 10)
        for rel, data in files.items():
            wb.from_archive(f"mods\\{name}\\{rel}", data, arc, rel)
        wb.inline_file(f"mods\\{name}\\meta.ini", meta(i + 1, (i + 1) * 10))
        write_files(
            base / "mods" / name, {**files, "meta.ini": meta(i + 1, (i + 1) * 10)}
        )
        if i % 10:  # every tenth mod is missing from custom
            write_files(
                cust / "mods" / name, {**files, "meta.ini": meta(i + 1, (i + 1) * 10)}
            )
    write_profile(
        base / "profiles" / "Main", modlist=[f"+{n}" for n in reversed(names)]
    )
    write_profile(
        cust / "profiles" / "Mine",
        modlist=[f"+{n}" for n in reversed(names) if (cust / "mods" / n).is_dir()],
    )
    write_mo2_ini(base, game_path=base / "Game Root", profile="Main")
    write_mo2_ini(cust, game_path=cust / "Game", profile="Mine")
    wj = wb.write(tmp_path / "list.wabbajack")

    t = time.perf_counter()
    result = mods.scan(
        config.load_instance({"mo2_dir": str(cust)}),
        config.load_instance({"mo2_dir": str(base)}),
        wabbajack.load_modlist(wj),
        HashCache(None),
    )
    elapsed = time.perf_counter() - t
    counts = result.counts()
    assert counts[mods.ONLY_BASELINE] == 80 and counts[mods.IDENTICAL] == 720
    assert elapsed < 120, f"scan took {elapsed:.1f}s"
