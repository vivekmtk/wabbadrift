import yaml

from wabbadrift import rules
from wabbadrift.configs import KeyDiff
from wabbadrift.gameroot import GameRootFile
from wabbadrift.mods import (
    CHANGED,
    CHANGED_FILES,
    DIFFERENT_VERSION,
    ONLY_BASELINE,
    ONLY_CUSTOM,
    FileDiff,
    ModComparison,
    ModSide,
    NexusInfo,
)
from wabbadrift.plugins import (
    BOTH,
    ONLY_CUSTOM as P_ONLY_CUSTOM,
    PluginDiff,
    PluginReport,
)
from wabbadrift.profile import ModEntry


def side(name, separator=None):
    return ModSide(name, True, True, 0, separator, NexusInfo(), None)


def skyrim_rules():
    return rules.Rules.from_dict(rules.default_rules_dict("skyrimse"))


def test_matches_is_literal_except_star_and_question():
    assert rules.matches(["[Custom]*"], "[custom] Visuals")
    assert not rules.matches(["[Custom]*"], "C Visuals")
    assert rules.matches(["SKSE/Plugins/*.log"], "SKSE\\Plugins\\x.log")
    assert rules.matches(["file?.txt"], "FILE1.txt")


def test_game_key():
    assert rules.game_key("SkyrimSpecialEdition") == "skyrimse"
    assert rules.game_key("", "New Vegas") == "falloutnv"
    assert rules.game_key("Cyberpunk 2077") == "generic"


def test_defaults_merge_common_and_game():
    d = rules.default_rules_dict("fallout4")
    assert "Fallout4.exe" in d["game_version_files"]["game_root"]
    assert "installscript.vdf" in d["game_version_files"]["game_root"]  # from common
    assert d["game"] == "fallout4"
    for game in set(rules.GAME_KEYS.values()) | {"generic"}:
        assert rules.default_rules_dict(game)["generator_outputs"]["mods"]


def test_load_rules_creates_editable_file(tmp_path):
    path = tmp_path / "pair.rules.yaml"
    r = rules.load_rules(path, "skyrimse")
    assert path.is_file() and r.game == "skyrimse"
    data = yaml.safe_load(path.read_text())
    data["intentional_additions"]["separators"] = ["Mine*"]
    path.write_text(yaml.safe_dump(data))
    assert rules.load_rules(path, "skyrimse").intentional_separators == ["Mine*"]


def test_apply_to_mods():
    r = skyrim_rules()
    lod = ModComparison(
        CHANGED_FILES, side("MyList - TexGen Output"), side("MyList - TexGen Output")
    )
    skse = ModComparison(
        DIFFERENT_VERSION, side("Skyrim Script Extender (SKSE64)"), side("SKSE")
    )
    logs = ModComparison(
        CHANGED_FILES,
        side("A"),
        side("A"),
        files=[FileDiff("SKSE\\Plugins\\a.log", CHANGED)],
    )
    real = ModComparison(
        CHANGED_FILES,
        side("B"),
        side("B"),
        files=[FileDiff("b.log", CHANGED), FileDiff("b.esp", CHANGED)],
    )
    mine = ModComparison(ONLY_CUSTOM, side("Mine", "[Custom] Visuals"), None)
    other = ModComparison(ONLY_CUSTOM, side("Other", "Visuals"), None)
    gone = ModComparison(ONLY_BASELINE, None, side("Gone"))
    rules.apply_to_mods([lod, skse, logs, real, mine, other, gone], r)
    assert lod.expected == rules.GENERATOR
    assert skse.expected == rules.GAME_VERSION
    assert (
        logs.expected == rules.GENERATOR and logs.files[0].expected == rules.GENERATOR
    )
    assert real.expected == "" and real.files[0].expected == rules.GENERATOR
    assert mine.expected == rules.INTENTIONAL
    assert other.expected == "" and gone.expected == ""


def test_apply_to_plugins():
    r = skyrim_rules()
    report = PluginReport(
        plugins=[
            PluginDiff("DynDOLOD.esp", BOTH),
            PluginDiff("Grass.esp", BOTH),
            PluginDiff("Mine.esp", P_ONLY_CUSTOM),
            PluginDiff("Theirs.esp", P_ONLY_CUSTOM),
        ],
        locked_order=[],
        loot_groups=[],
    )
    owners = {
        "grass.esp": ModEntry("Grass Cache Output", True, 1, separator=None),
        "mine.esp": ModEntry("Mine", True, 2, separator="[Custom] Fixes_separator"),
        "theirs.esp": ModEntry("Theirs", True, 3, separator="Fixes_separator"),
    }
    rules.apply_to_plugins(report, r, owners, {})
    got = {p.name: p.expected for p in report.plugins}
    assert got == {
        "DynDOLOD.esp": rules.GENERATOR,
        "Grass.esp": rules.GENERATOR,
        "Mine.esp": rules.INTENTIONAL,
        "Theirs.esp": "",
    }


def test_apply_to_game_root_and_keys():
    r = skyrim_rules()
    files = [
        GameRootFile("SkyrimSE.exe", CHANGED),
        GameRootFile("Data\\Source\\Scripts\\x.psc", "added"),
        GameRootFile("Logs\\x.txt", "added"),
        GameRootFile("enbseries.ini", "removed"),
    ]
    rules.apply_to_game_root(files, r)
    assert [f.expected for f in files] == [
        rules.GAME_VERSION,
        rules.GAME_VERSION,
        rules.GENERATOR,
        "",
    ]
    keys = [
        KeyDiff("Display", "iSize W", CHANGED, "1920", "2560"),
        KeyDiff("General", "sPath", CHANGED, "C:\\A", "D:\\B"),
        KeyDiff("Display", "bSAOEnable", CHANGED, "0", "1"),
    ]
    rules.apply_to_keys(keys, r)
    assert [k.expected for k in keys] == [rules.MACHINE, rules.MACHINE, ""]


def test_plugin_dlls_expected_only_when_game_versions_differ():
    r = skyrim_rules()
    roots = [GameRootFile("SkyrimSE.exe", CHANGED)]
    assert rules.game_versions_differ(roots, r)
    assert not rules.game_versions_differ([GameRootFile("SkyrimSE.exe", "same")], r)

    def mod(*paths):
        files = [FileDiff(p, CHANGED) for p in paths]
        return ModComparison(
            DIFFERENT_VERSION, side("Fixes"), side("Fixes"), files=files
        )

    dll_only, dll_and_cfg, same_game = (
        mod("SKSE/Plugins/F.dll"),
        mod("SKSE/Plugins/F.dll", "SKSE/Plugins/F.toml"),
        mod("SKSE/Plugins/F.dll"),
    )
    rules.apply_to_mods([dll_only, dll_and_cfg], r, game_versions_differ=True)
    rules.apply_to_mods([same_game], r)
    assert dll_only.expected == rules.GAME_VERSION
    assert (
        dll_and_cfg.expected == ""
        and dll_and_cfg.files[0].expected == rules.GAME_VERSION
    )
    assert same_game.expected == ""
