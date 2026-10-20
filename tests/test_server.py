import json
import threading
import urllib.error
import urllib.request

import pytest

from synth import WabbajackBuilder, write_files, write_mo2_ini, write_profile
from wabbadrift import actions, report, server


def meta(mod_id, file_id):
    return (
        f"[General]\nmodid={mod_id}\n[installedFiles]\nsize=1\n"
        f"1\\modid={mod_id}\n1\\fileid={file_id}\n"
    )


@pytest.fixture
def running(tmp_path, monkeypatch):
    """A synthetic pair served on a free port; data/ goes to tmp_path."""
    base, cust = tmp_path / "base", tmp_path / "custom"
    wb = WabbajackBuilder()
    for name, files in {
        "Alpha": {"a.esp": "A", "a.ini": "[S]\nx=1\n"},
        "Delta": {"d.esp": "D"},
    }.items():
        arc = wb.nexus_archive(f"{name}.7z", 1 if name == "Alpha" else 4, 10)
        for rel, data in files.items():
            wb.from_archive(f"mods\\{name}\\{rel}", data, arc, rel)
        wb.inline_file(f"mods\\{name}\\meta.ini", meta(1 if name == "Alpha" else 4, 10))
        write_files(
            base / "mods" / name,
            {**files, "meta.ini": meta(1 if name == "Alpha" else 4, 10)},
        )
    write_files(
        cust / "mods" / "Alpha",
        {"a.esp": "A", "a.ini": "[S]\nx=2\n", "meta.ini": meta(1, 10)},
    )
    write_files(cust / "mods" / "Mine", {"m.esp": "M"})
    write_profile(base / "profiles" / "Main", modlist=["+Delta", "+Alpha"])
    write_profile(cust / "profiles" / "Mine", modlist=["+Mine", "+Alpha"])
    write_mo2_ini(base, game_path=base / "Game Root", profile="Main")
    write_mo2_ini(cust, game_path=cust / "Game", profile="Mine")
    wj = wb.write(tmp_path / "wj" / "list.wabbajack")

    pairs = tmp_path / "data" / "pairs"
    pairs.mkdir(parents=True)
    (pairs / "synth.json").write_text(
        json.dumps(
            {
                "name": "synth",
                "custom": {"mo2_dir": str(cust)},
                "baseline": {"mo2_dir": str(base)},
                "wabbajack_file": str(wj),
                "rules_file": str(pairs / "synth.rules.yaml"),
                "backup_dir": str(tmp_path / "data" / "backups"),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "PAIRS_DIR", pairs)
    monkeypatch.setattr(report, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(server, "_states", {})
    monkeypatch.setattr(actions, "running_executables", lambda: [])
    srv = server.make_server(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", wj
    srv.shutdown()
    srv.server_close()


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def post(url, body, headers=None):
    h = {"Content-Type": "application/json", "X-Wabbadrift": "1"}
    h.update(headers or {})
    req = urllib.request.Request(url, json.dumps(body).encode(), h, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_static_allowlist_and_host_check(running):
    url, _ = running
    status, body = get(url + "/")
    assert status == 200 and b"<title>Wabbadrift</title>" in body
    assert get(url + "/web/app.js")[0] == 200
    for path in (
        "/web/../wabbadrift/server.py",
        "/data/pairs/synth.json",
        "/.env",
        "/web/",
    ):
        assert get(url + path)[0] == 404, path
    assert get(url + "/", {"Host": "evil.example"})[0] == 403


def test_post_needs_custom_header(running):
    url, _ = running
    assert post(url + "/api/rescan", {"pair": "synth"}, {"X-Wabbadrift": ""})[0] == 400
    assert post(url + "/api/rescan", {"pair": "../x"})[0] == 400


def test_scan_mod_and_diff(running):
    url, _ = running
    assert json.loads(get(url + "/api/pairs")[1]) == {"pairs": ["synth"]}
    status, body = get(url + "/api/scan?pair=synth")
    rep = json.loads(body)
    assert status == 200
    assert {m["name"]: m["status"] for m in rep["mods"]} == {
        "Alpha": "changed_files",
        "Delta": "only_baseline",
        "Mine": "only_custom",
    }
    mod = json.loads(get(url + "/api/mod?pair=synth&name=Alpha")[1])
    assert [(f["path"], f["status"], f["text"]) for f in mod["files"]] == [
        ("a.ini", "changed", True)
    ]
    diff = json.loads(get(url + "/api/filediff?pair=synth&name=Alpha&path=a.ini")[1])
    assert [(k["key"], k["baseline"], k["custom"]) for k in diff["keys"]] == [
        ("x", "1", "2")
    ]
    assert (
        get(url + "/api/filediff?pair=synth&name=Alpha&path=..%2F..%2Fx.ini")[0] == 400
    )
    assert get(url + "/api/mod?pair=synth&name=Nope")[0] == 400


def test_snapshot_and_identical_drift(running):
    url, wj = running
    status, info = post(url + "/api/snapshot", {"pair": "synth"})
    assert status == 200 and info["version"] == "1.0.0"
    assert (
        post(url + "/api/snapshot", {"pair": "synth", "path": str(wj) + ".txt"})[0]
        == 400
    )
    snaps = json.loads(get(url + "/api/snapshots?pair=synth")[1])["snapshots"]
    sid = snaps[0]["id"]
    drift = json.loads(get(url + f"/api/drift?pair=synth&old={sid}&new={sid}")[1])
    assert drift["identical"] is True


def test_plan_apply_undo(running):
    url, _ = running
    status, plan = post(
        url + "/api/plan", {"pair": "synth", "copy": ["Delta"], "replace": []}
    )
    assert status == 200 and not plan["mo2_running"]
    assert [(m["action"], m["mod"], m["ok"]) for m in plan["mods"]] == [
        ("copy", "Delta", True)
    ]
    assert "ops" not in plan["mods"][0]
    assert post(url + "/api/plan", {"pair": "synth", "copy": "Delta"})[0] == 400

    stale = {"pair": "synth", "copy": ["Delta"], "replace": [], "fingerprint": "x"}
    status, err = post(url + "/api/apply", stale)
    assert status == 409 and "Preview again" in err["error"]

    status, run = post(
        url + "/api/apply", {**stale, "fingerprint": plan["fingerprint"]}
    )
    assert status == 200 and run["status"] == "done"
    rep = json.loads(get(url + "/api/scan?pair=synth")[1])  # rescanned after the write
    assert {m["name"]: m["status"] for m in rep["mods"]}["Delta"] == "identical"
    runs = json.loads(get(url + "/api/runs?pair=synth")[1])["runs"]
    assert [r["id"] for r in runs] == [run["id"]]

    status, undone = post(url + "/api/undo", {"pair": "synth", "run": run["id"]})
    assert status == 200 and undone["status"] == "undone"
    assert post(url + "/api/undo", {"pair": "synth", "run": "../../x"})[0] == 409


def test_pair_setup(running):
    url, wj = running
    status, synth = get(url + "/api/pairconfig?pair=synth")
    synth = json.loads(synth)
    cust, base = synth["custom"]["mo2_dir"], synth["baseline"]["mo2_dir"]
    assert status == 200 and synth["custom"]["profile"] == ""

    status, info = post(url + "/api/inspect", {"mo2_dir": f'"{cust}"'})
    assert status == 200
    assert info["profiles"] == ["Mine"] and info["selected_profile"] == "Mine"
    assert post(url + "/api/inspect", {"mo2_dir": str(wj.parent)})[0] == 400
    assert post(url + "/api/inspect", {"mo2_dir": "relative\\path"})[0] == 400
    status, info = post(url + "/api/inspect", {"wabbajack_file": str(wj)})
    assert status == 200 and info["version"] == "1.0.0"

    form = {
        "name": "second",
        "custom": {"mo2_dir": cust, "profile": "Mine"},
        "baseline": {"mo2_dir": base, "profile": "Main"},
        "wabbajack_file": str(wj),
        "backup_dir": str(wj.parent / "backups"),
    }
    bad = [
        {**form, "name": "../x"},
        {**form, "name": "synth"},  # exists
        {**form, "custom": {"mo2_dir": cust, "profile": "Nope"}},
        {**form, "baseline": {"mo2_dir": cust, "profile": "Mine"}},  # same folder
        {**form, "backup_dir": cust + "\\backups"},
        {**form, "wabbajack_file": cust},
    ]
    for f in bad:
        status, err = post(url + "/api/pairs/save", {"create": True, "pair": f})
        assert status == 400, f
    assert json.loads(get(url + "/api/pairs")[1])["pairs"] == ["synth"]

    assert post(url + "/api/pairs/save", {"create": True, "pair": form}) == (
        200,
        {"name": "second"},
    )
    assert json.loads(get(url + "/api/pairs")[1])["pairs"] == ["second", "synth"]
    assert get(url + "/api/scan?pair=second")[0] == 200

    # editing keeps what the form doesn't show
    edited = {
        **synth,
        "custom": {**synth["custom"], "profile": "Mine"},
        "baseline": {**synth["baseline"], "profile": ""},
    }
    status, err = post(url + "/api/pairs/save", {"create": False, "pair": edited})
    assert status == 400 and err["error"] == "Baseline instance: choose a profile."
    edited["baseline"]["profile"] = "Main"
    assert post(url + "/api/pairs/save", {"create": False, "pair": edited})[0] == 200
    status, again = get(url + "/api/pairconfig?pair=synth")
    assert json.loads(again)["custom"]["profile"] == "Mine"
    status, scan = get(url + "/api/scan?pair=synth")
    assert json.loads(scan)["custom"]["profile"] == "Mine"
    pairs_dir = server.PAIRS_DIR
    assert json.loads((pairs_dir / "synth.json").read_text())["rules_file"]

    assert post(url + "/api/pairs/delete", {"pair": "second"}) == (
        200,
        {"pairs": ["synth"]},
    )
    assert not (pairs_dir / "second.json").exists()
    assert post(url + "/api/pairs/delete", {"pair": "second"})[0] == 400
