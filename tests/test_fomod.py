import zipfile
import zlib
from types import SimpleNamespace

import pytest

from synth import WabbajackBuilder, write_files
from wabbadrift import fomod, wabbajack
from wabbadrift.config import find_7z

# A small installer exercising every kind of evidence:
# - Main: Full (superset) vs Lite, plus a flag that unlocks the "Extras" step
# - Color: Red / Blue (distinct files)
# - Walkways: Default / Alt UVs, no files; Alt UVs only shows through a conditional install
# - Extras (visible only with extras=on): Glow
CONFIG = b"""<?xml version="1.0" encoding="utf-8"?>
<config>
  <moduleName>Synth Mod</moduleName>
  <requiredInstallFiles>
    <folder source="00 Core" destination="" />
  </requiredInstallFiles>
  <installSteps order="Explicit">
    <installStep name="Main">
      <optionalFileGroups order="Explicit">
        <group name="Version" type="SelectExactlyOne">
          <plugins order="Explicit">
            <plugin name="Full">
              <description>everything</description>
              <files><folder source="10 Base" destination="" />
                     <folder source="11 Full" destination="" /></files>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
            <plugin name="Lite">
              <files><folder source="10 Base" destination="" /></files>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
          </plugins>
        </group>
        <group name="Color" type="SelectExactlyOne">
          <plugins order="Explicit">
            <plugin name="Red">
              <files><file source="20 Red\\textures\\color.dds"
                           destination="textures\\color.dds" /></files>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
            <plugin name="Blue">
              <files><file source="21 Blue\\textures\\color.dds"
                           destination="textures\\color.dds" /></files>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
          </plugins>
        </group>
        <group name="Walkways" type="SelectExactlyOne">
          <plugins order="Explicit">
            <plugin name="Default UVs">
              <conditionFlags><flag name="uvs">off</flag></conditionFlags>
              <typeDescriptor><type name="Recommended"/></typeDescriptor>
            </plugin>
            <plugin name="Alt UVs">
              <conditionFlags><flag name="uvs">on</flag></conditionFlags>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
          </plugins>
        </group>
        <group name="Extras" type="SelectExactlyOne">
          <plugins order="Explicit">
            <plugin name="No extras">
              <conditionFlags><flag name="extras">off</flag></conditionFlags>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
            <plugin name="Show extras">
              <conditionFlags><flag name="extras">on</flag></conditionFlags>
              <typeDescriptor>
                <dependencyType><defaultType name="NotUsable"/><patterns/></dependencyType>
              </typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
    <installStep name="Extras">
      <visible><flagDependency flag="extras" value="on"/></visible>
      <optionalFileGroups>
        <group name="Glow" type="SelectAny">
          <plugins>
            <plugin name="Glow">
              <files><file source="30 Glow\\glow.dds" destination="textures\\glow.dds" /></files>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
  <conditionalFileInstalls>
    <patterns>
      <pattern>
        <dependencies operator="And"><flagDependency flag="uvs" value="on"/></dependencies>
        <files><file source="40 UVs\\synth.esp" destination="synth.esp" /></files>
      </pattern>
    </patterns>
  </conditionalFileInstalls>
</config>
"""

ROOT = "Synth Mod v1/"
FILES = {
    "00 Core/synth.esp": b"core plugin",
    "10 Base/meshes/a.nif": b"mesh a",
    "11 Full/meshes/b.nif": b"mesh b (full only)",
    "20 Red/textures/color.dds": b"red!",
    "21 Blue/textures/color.dds": b"blue",
    "30 Glow/glow.dds": b"glow",
    "40 UVs/synth.esp": b"plugin with other UVs",
}


def key(data: bytes):
    return len(data), f"{zlib.crc32(data):08X}"


def make_index() -> fomod.ArchiveIndex:
    entries = {
        fomod.norm(ROOT + p): fomod.Entry(ROOT + p, *key(d)) for p, d in FILES.items()
    }
    entries[fomod.norm(ROOT + "fomod/ModuleConfig.xml")] = fomod.Entry(
        ROOT + "fomod/ModuleConfig.xml", *key(CONFIG)
    )
    return fomod.ArchiveIndex(entries, ROOT + "fomod/ModuleConfig.xml", CONFIG)


def observed(**files: bytes):
    """Installed mod-folder path -> content key."""
    return {fomod.norm(p.replace("__", "/")): key(d) for p, d in files.items()}


def choices(inf: fomod.Inference) -> dict[str, tuple]:
    return {c.option: (c.selected, c.confidence) for c in inf.choices}


def test_parse_listing():
    text = (
        "Path = mod\nSize = 0\nAttributes = D\nCRC = \n\n"
        "Path = mod\\fomod\\ModuleConfig.xml\nSize = 120\nAttributes = A\nCRC = 1a2b3c4d\n\n"
        "Path = other\nFolder = +\nSize = 0\n"
    )
    entries = fomod.parse_listing(text)
    assert [(e.path, e.size, e.crc) for e in entries] == [
        ("mod\\fomod\\ModuleConfig.xml", 120, "1A2B3C4D")
    ]


def test_parse_module_config():
    cfg = fomod.parse_module_config(CONFIG)
    assert cfg.name == "Synth Mod"
    assert [s.name for s in cfg.steps] == ["Main", "Extras"]
    groups = cfg.steps[0].groups
    assert [g.name for g in groups] == ["Version", "Color", "Walkways", "Extras"]
    assert [o.name for o in groups[0].options] == ["Full", "Lite"]
    red = groups[1].options[0]
    assert (red.installs[0].source, red.installs[0].destination) == (
        "20 Red\\textures\\color.dds",
        "textures\\color.dds",
    )
    assert groups[2].options[1].flags == {"uvs": "on"}
    # a dependencyType default isn't a fixed type
    assert groups[3].options[1].type == "Optional"
    assert cfg.steps[1].visible.flags == [("extras", "on")]
    assert cfg.patterns[0].dependency.describe() == "uvs='on'"


def test_parse_refuses_entities():
    with pytest.raises(ValueError):
        fomod.parse_module_config(b'<!DOCTYPE x [<!ENTITY a "b">]><config/>')


def test_ascending_order_is_applied():
    xml = b"""<config><moduleName>m</moduleName><installSteps><installStep name="s">
      <optionalFileGroups><group name="g" type="SelectAny"><plugins>
        <plugin name="b"/><plugin name="a"/></plugins></group></optionalFileGroups>
    </installStep></installSteps></config>"""
    cfg = fomod.parse_module_config(xml)
    assert [o.name for o in cfg.steps[0].groups[0].options] == ["a", "b"]


def test_expand_resolves_priority_and_folders():
    index = make_index()
    cfg = fomod.parse_module_config(CONFIG)
    full = cfg.steps[0].groups[0].options[0]
    assert fomod.expand(full.installs, index) == {
        ("meshes/a.nif", key(FILES["10 Base/meshes/a.nif"])),
        ("meshes/b.nif", key(FILES["11 Full/meshes/b.nif"])),
    }


def test_infer_full_install():
    index = make_index()
    cfg = fomod.parse_module_config(CONFIG)
    inf = fomod.infer(
        cfg,
        index,
        observed(
            **{
                "synth.esp": FILES["00 Core/synth.esp"],
                "meshes__a.nif": FILES["10 Base/meshes/a.nif"],
                "meshes__b.nif": FILES["11 Full/meshes/b.nif"],
                "textures__color.dds": FILES["21 Blue/textures/color.dds"],
            }
        ),
    )
    got = choices(inf)
    assert got["Full"] == (True, fomod.EXACT)  # only it installs b.nif
    assert got["Lite"] == (False, fomod.LIKELY)  # one per group
    assert got["Blue"] == (True, fomod.EXACT)
    assert got["Red"][0] is False
    # the UV plugin wasn't installed, so the flag that runs it wasn't set
    assert got["Alt UVs"][0] is False and got["Default UVs"][0] is True
    # Glow's file isn't there: either the Extras step was hidden, or it was shown and Glow
    # left unticked. The files can't tell, so both stay undecided.
    assert got["Glow"] == (False, fomod.EXACT)
    assert got["No extras"][0] is None and got["Show extras"][0] is None
    assert inf.coverage == (4, 4)


def test_infer_conditional_install_and_hidden_file():
    index = make_index()
    cfg = fomod.parse_module_config(CONFIG)
    inf = fomod.infer(
        cfg,
        index,
        {
            "synth.esp": key(FILES["40 UVs/synth.esp"]),  # the conditional install ran
            "meshes/a.nif": key(FILES["10 Base/meshes/a.nif"]),
            "textures/color.dds": key(FILES["20 Red/textures/color.dds"]),
            "textures/glow.dds": key(FILES["30 Glow/glow.dds"]),
        },
    )
    got = choices(inf)
    assert got["Lite"] == (True, fomod.LIKELY)  # Full's b.nif is missing
    assert got["Alt UVs"] == (True, fomod.LIKELY)
    assert got["Red"] == (True, fomod.EXACT)
    # Glow's file is there, so its step was shown, which needs extras=on
    assert got["Glow"] == (True, fomod.EXACT)
    assert got["Show extras"] == (True, fomod.LIKELY)
    assert inf.patterns[0].installed is True and inf.patterns[0].expected is True


def test_unknown_origin_stays_undecided():
    index = make_index()
    cfg = fomod.parse_module_config(CONFIG)
    inf = fomod.infer(
        cfg,
        index,
        {
            "synth.esp": key(FILES["00 Core/synth.esp"]),
            "meshes/a.nif": key(FILES["10 Base/meshes/a.nif"]),
            "textures/color.dds": None,  # from another archive: unknown
        },
    )
    got = choices(inf)
    assert got["Red"][0] is None and got["Blue"][0] is None
    assert got["Glow"] == (False, fomod.EXACT)


def test_folder_observed_uses_size_then_crc_and_unhides(tmp_path):
    index = make_index()
    cfg = fomod.parse_module_config(CONFIG)
    write_files(
        tmp_path,
        {
            "meshes/a.nif.mohidden": FILES["10 Base/meshes/a.nif"],
            "textures/color.dds": b"BLUE",  # same size as the archive's, other content
            "readme.txt": b"not from the installer",
        },
    )
    obs = fomod.folder_observed(tmp_path, cfg, index)
    assert obs == {
        "meshes/a.nif": key(FILES["10 Base/meshes/a.nif"]),
        "textures/color.dds": None,
        "readme.txt": None,
    }


def test_differences():
    index = make_index()
    cfg = fomod.parse_module_config(CONFIG)
    common = {
        "synth.esp": key(FILES["00 Core/synth.esp"]),
        "meshes/a.nif": key(FILES["10 Base/meshes/a.nif"]),
    }
    red = fomod.infer(
        cfg,
        index,
        {**common, "textures/color.dds": key(FILES["20 Red/textures/color.dds"])},
    )
    blue = fomod.infer(
        cfg,
        index,
        {**common, "textures/color.dds": key(FILES["21 Blue/textures/color.dds"])},
    )
    diffs = {(d.option, d.baseline, d.custom) for d in fomod.differences(red, blue)}
    assert diffs == {("Red", True, False), ("Blue", False, True)}


sevenzip = find_7z(None, None)


@pytest.mark.skipif(sevenzip is None, reason="7z.exe not installed")
def test_fomod_report_end_to_end(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    archive = downloads / "Synth Mod-1-1-0.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr(ROOT + "fomod/ModuleConfig.xml", CONFIG)
        for p, d in FILES.items():
            z.writestr(ROOT + p, d)

    wj = WabbajackBuilder()
    ahash = wj.nexus_archive(archive.name, 1, 2, size=archive.stat().st_size)
    for to, src in [
        ("synth.esp", "00 Core/synth.esp"),
        ("meshes\\a.nif", "10 Base/meshes/a.nif"),
        ("meshes\\b.nif", "11 Full/meshes/b.nif"),
        ("textures\\color.dds", "21 Blue/textures/color.dds"),
    ]:
        inner = (ROOT + src).replace("/", "\\")
        wj.from_archive(f"mods\\Synth Mod\\{to}", FILES[src], ahash, inner)
    wj.inline_file("mods\\Synth Mod\\meta.ini", "[General]\nmodid=1\n")
    ml = wabbajack.load_modlist(wj.write(tmp_path / "list.wabbajack"))

    custom = tmp_path / "custom" / "Synth Mod"
    write_files(
        custom,
        {
            "synth.esp": FILES["00 Core/synth.esp"],
            "meshes/a.nif": FILES["10 Base/meshes/a.nif"],
            "meshes/b.nif": FILES["11 Full/meshes/b.nif"],
            "textures/color.dds": FILES["20 Red/textures/color.dds"],
        },
    )
    pair = SimpleNamespace(sevenzip_exe=sevenzip, downloads_dir=downloads)
    cache = tmp_path / "cache"
    rep = fomod.fomod_report(pair, ml, "Synth Mod", custom, archive.name, cache)
    assert rep.archive == archive.name and rep.module == "Synth Mod"
    assert choices(rep.baseline)["Blue"] == (True, fomod.EXACT)
    assert choices(rep.custom)["Red"] == (True, fomod.EXACT)
    assert {(d.option, d.custom) for d in rep.differences} == {
        ("Red", True),
        ("Blue", False),
    }
    # the listing is cached; a second report doesn't need 7z
    assert len(list(cache.glob("*.json"))) == 1
    pair.sevenzip_exe = tmp_path / "missing-7z.exe"
    again = fomod.fomod_report(pair, ml, "Synth Mod", None, "", cache)
    assert choices(again.baseline) == choices(rep.baseline)


def test_hidden_step_rules_out_its_options():
    xml = b"""<config><moduleName>m</moduleName><installSteps order="Explicit">
      <installStep name="One"><optionalFileGroups><group name="g" type="SelectExactlyOne">
        <plugins order="Explicit">
          <plugin name="A"><files><file source="a.txt" destination="a.txt"/></files>
            <conditionFlags><flag name="x">on</flag></conditionFlags></plugin>
          <plugin name="B"><files><file source="b.txt" destination="b.txt"/></files>
            <conditionFlags><flag name="x">off</flag></conditionFlags></plugin>
        </plugins></group></optionalFileGroups></installStep>
      <installStep name="Two"><visible><flagDependency flag="x" value="on"/></visible>
        <optionalFileGroups><group name="h" type="SelectAny"><plugins>
          <plugin name="C"><files><file source="b.txt" destination="b.txt"/></files></plugin>
        </plugins></group></optionalFileGroups></installStep>
    </installSteps></config>"""
    cfg = fomod.parse_module_config(xml)
    index = fomod.ArchiveIndex(
        {
            "a.txt": fomod.Entry("a.txt", 1, "AAAAAAAA"),
            "b.txt": fomod.Entry("b.txt", 1, "BBBBBBBB"),
        },
        "fomod/ModuleConfig.xml",
        xml,
    )
    got = choices(fomod.infer(cfg, index, {"b.txt": (1, "BBBBBBBB")}))
    assert got["A"] == (False, fomod.EXACT)
    assert got["B"][0] is True
    assert got["C"] == (False, fomod.EXACT)  # step Two needs x=on
