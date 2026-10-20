"""FOMOD choice recovery (reference only; installers are never re-run).

The source archive is read with 7z, read-only: a technical listing (path, size, CRC) and the
``fomod/ModuleConfig.xml`` member streamed to memory. Nothing is extracted to disk.

Every option, required install and conditional install maps archive files to mod-folder paths.
A file is identified by its content (size + CRC from the listing), so an installed mod tells
which options put files where:

- baseline: the .wabbajack directives name the archive path each file came from;
- custom: the mod folder's files are matched by size, then CRC32.

Each option gets selected / not selected / unknown, a confidence (exact / likely / ambiguous)
and its evidence. Flags, conditional installs and step conditions refine the answer.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import re
import subprocess
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from .mods import walk_files
from .wabbajack import Modlist

EXACT = "exact"
LIKELY = "likely"
AMBIGUOUS = "ambiguous"

MODULE_CONFIG = "fomod/moduleconfig.xml"
_7Z_TIMEOUT = 600


def norm(path: str) -> str:
    """Archive/mod path key: lower-case, ``/`` separators, no leading ``/`` or ``./``."""
    p = re.sub(r"/+", "/", path.replace("\\", "/")).lower()
    while p.startswith("./"):
        p = p[2:]
    return p.strip("/")


# --- archive access ------------------------------------------------------------------------


@dataclass
class Entry:
    path: str  # as listed
    size: int
    crc: str  # upper-case hex, "" if the format has none

    @property
    def key(self) -> tuple[int, str]:
        return self.size, self.crc


@dataclass
class ArchiveIndex:
    entries: dict[str, Entry]  # norm(path) -> entry (files only)
    config_path: str | None = None  # ModuleConfig.xml as listed
    config: bytes | None = None

    @property
    def root(self) -> str:
        """Folder holding ``fomod/`` (FOMOD sources are relative to it), with trailing ``/``."""
        if not self.config_path:
            return ""
        p = norm(self.config_path)
        return p[: -len(MODULE_CONFIG)]


def _run_7z(sevenzip: Path, args: list[str]) -> bytes:
    r = subprocess.run(
        [str(sevenzip), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=_7Z_TIMEOUT,
    )
    if r.returncode != 0:
        msg = r.stderr.decode("utf-8", "replace").strip() or f"exit {r.returncode}"
        raise RuntimeError(f"7z failed: {msg}")
    return r.stdout


def parse_listing(text: str) -> list[Entry]:
    """Files from ``7z l -slt -ba`` output (folders skipped)."""
    out = []
    for block in re.split(r"\r?\n\r?\n", text):
        f = {}
        for line in block.splitlines():
            k, sep, v = line.partition(" = ")
            if sep:
                f[k.strip()] = v.strip()
            elif line.endswith(" ="):
                f[line[:-2].strip()] = ""
        if "Path" not in f:
            continue
        if f.get("Folder") == "+" or "D" in f.get("Attributes", "").split("_")[0]:
            continue
        try:
            size = int(f.get("Size") or 0)
        except ValueError:
            size = 0
        out.append(Entry(f["Path"], size, f.get("CRC", "").upper()))
    return out


def list_archive(sevenzip: Path, archive: Path) -> list[Entry]:
    out = _run_7z(sevenzip, ["l", "-slt", "-ba", "-sccUTF-8", str(archive)])
    return parse_listing(out.decode("utf-8", "replace"))


def read_member(sevenzip: Path, archive: Path, member: str) -> bytes:
    """One archive member, streamed to memory (``-so``); nothing is written to disk."""
    return _run_7z(sevenzip, ["x", "-so", "-sccUTF-8", str(archive), member])


def archive_index(
    sevenzip: Path, archive: Path, cache_dir: Path | None
) -> ArchiveIndex:
    """Listing + ModuleConfig.xml, cached by archive path, size and mtime."""
    st = archive.stat()
    cache_file = None
    if cache_dir:
        name = hashlib.sha1(str(archive).lower().encode("utf-8")).hexdigest()
        cache_file = cache_dir / f"{name}.json"
        if cache_file.is_file():
            c = json.loads(cache_file.read_text(encoding="utf-8"))
            if c.get("size") == st.st_size and c.get("mtime_ns") == st.st_mtime_ns:
                return ArchiveIndex(
                    {norm(p): Entry(p, s, crc) for p, s, crc in c["entries"]},
                    c["config_path"],
                    base64.b64decode(c["config"]) if c["config"] else None,
                )
    entries = list_archive(sevenzip, archive)
    configs = [e.path for e in entries if norm(e.path).endswith(MODULE_CONFIG)]
    config_path = min(configs, key=len) if configs else None
    config = read_member(sevenzip, archive, config_path) if config_path else None
    if cache_file:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps(
                {
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                    "entries": [[e.path, e.size, e.crc] for e in entries],
                    "config_path": config_path,
                    "config": base64.b64encode(config).decode() if config else None,
                }
            ),
            encoding="utf-8",
        )
    return ArchiveIndex({norm(e.path): e for e in entries}, config_path, config)


# --- ModuleConfig.xml ----------------------------------------------------------------------


@dataclass
class Dependency:
    operator: str = "And"
    flags: list[tuple[str, str]] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)  # (file, state)
    nested: list["Dependency"] = field(default_factory=list)
    other: list[str] = field(
        default_factory=list
    )  # game/script-extender/manager versions

    def describe(self) -> str:
        parts = [f"{f}={v!r}" for f, v in self.flags]
        parts += [f"file {f} is {s}" for f, s in self.files]
        parts += [f"({d.describe()})" for d in self.nested]
        parts += self.other
        return f" {self.operator.upper()} ".join(parts) or "always"


@dataclass
class Install:
    source: str
    destination: str
    folder: bool
    priority: int = 0


@dataclass
class Option:
    name: str
    description: str = ""
    type: str = "Optional"
    installs: list[Install] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)


@dataclass
class Group:
    name: str
    type: str
    options: list[Option] = field(default_factory=list)


@dataclass
class Step:
    name: str
    visible: Dependency | None = None
    groups: list[Group] = field(default_factory=list)


@dataclass
class Pattern:
    dependency: Dependency
    installs: list[Install] = field(default_factory=list)


@dataclass
class ModuleConfig:
    name: str = ""
    required: list[Install] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    patterns: list[Pattern] = field(default_factory=list)

    def options(self):
        for step in self.steps:
            for group in step.groups:
                for opt in group.options:
                    yield step, group, opt


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _kids(el, name: str) -> list:
    return [c for c in el if _local(c.tag) == name] if el is not None else []


def _kid(el, name: str):
    k = _kids(el, name)
    return k[0] if k else None


def _ordered(items: list, el, key) -> list:
    order = (el.get("order") if el is not None else None) or "Ascending"
    if order == "Explicit":
        return items
    return sorted(items, key=lambda i: key(i).lower(), reverse=order == "Descending")


def _parse_xml(data: bytes):
    if b"<!ENTITY" in data or b"<\x00!\x00E\x00N\x00T\x00I\x00T\x00Y" in data:
        raise ValueError("ModuleConfig.xml declares entities; refusing to parse it")
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        # declared encoding often doesn't match the bytes
        if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = data.decode("utf-16")
        else:
            text = data.decode("utf-8-sig", "replace")
        return ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text))


def _dependency(el) -> Dependency:
    dep = Dependency(operator=el.get("operator") or "And")
    for c in el:
        tag = _local(c.tag)
        if tag == "flagDependency":
            dep.flags.append((c.get("flag", ""), c.get("value", "")))
        elif tag == "fileDependency":
            dep.files.append((c.get("file", ""), c.get("state", "")))
        elif tag == "dependencies":
            dep.nested.append(_dependency(c))
        else:
            dep.other.append(f"{tag} {c.get('version', '')}".strip())
    return dep


def _installs(el) -> list[Install]:
    out = []
    for c in el if el is not None else []:
        tag = _local(c.tag)
        if tag not in ("file", "folder"):
            continue
        source = c.get("source", "")
        dest = c.get("destination")
        if dest is None:
            dest = source
        elif tag == "file" and (dest == "" or dest.endswith(("/", "\\"))):
            dest = dest + norm(source).rsplit("/", 1)[-1]
        try:
            priority = int(c.get("priority") or 0)
        except ValueError:
            priority = 0
        out.append(Install(source, dest, tag == "folder", priority))
    return out


def _plugin_type(el) -> str:
    """The fixed type; a ``dependencyType`` depends on the user's setup, so it counts as
    Optional (its default is often NotUsable, overridden when a plugin is present)."""
    t = _kid(_kid(el, "typeDescriptor"), "type")
    return t.get("name", "Optional") if t is not None else "Optional"


def parse_module_config(data: bytes) -> ModuleConfig:
    root = _parse_xml(data)
    cfg = ModuleConfig(name=(_kid(root, "moduleName").text or "").strip())
    cfg.required = _installs(_kid(root, "requiredInstallFiles"))
    steps_el = _kid(root, "installSteps")
    for s in _kids(steps_el, "installStep"):
        vis = _kid(s, "visible")
        step = Step(s.get("name", ""), _dependency(vis) if vis is not None else None)
        groups_el = _kid(s, "optionalFileGroups")
        for g in _kids(groups_el, "group"):
            group = Group(g.get("name", ""), g.get("type", "SelectAny"))
            plugins_el = _kid(g, "plugins")
            for p in _kids(plugins_el, "plugin"):
                desc = _kid(p, "description")
                group.options.append(
                    Option(
                        name=p.get("name", ""),
                        description=(
                            (desc.text or "").strip() if desc is not None else ""
                        ),
                        type=_plugin_type(p),
                        installs=_installs(_kid(p, "files")),
                        flags={
                            f.get("name", ""): (f.text or "")
                            for f in _kids(_kid(p, "conditionFlags"), "flag")
                        },
                    )
                )
            group.options = _ordered(group.options, plugins_el, lambda o: o.name)
            step.groups.append(group)
        step.groups = _ordered(step.groups, groups_el, lambda g: g.name)
        cfg.steps.append(step)
    cfg.steps = _ordered(cfg.steps, steps_el, lambda s: s.name)
    for p in _kids(_kid(_kid(root, "conditionalFileInstalls"), "patterns"), "pattern"):
        deps = _kid(p, "dependencies")
        cfg.patterns.append(
            Pattern(
                _dependency(deps) if deps is not None else Dependency(),
                _installs(_kid(p, "files")),
            )
        )
    return cfg


# --- inference -----------------------------------------------------------------------------

Key = tuple[int, str]  # (size, crc) of a file's content


def expand(installs: list[Install], index: ArchiveIndex) -> set[tuple[str, Key]]:
    """(mod-folder path, content) pairs the installs produce from this archive. Where several
    write one path, the highest priority wins, then the later entry."""
    out: dict[str, Key] = {}
    root = index.root
    for ins in sorted(installs, key=lambda i: i.priority):
        src, dest = root + norm(ins.source), norm(ins.destination)
        if not ins.folder:
            e = index.entries.get(src)
            if e:
                out[dest] = e.key
            continue
        prefix = src + "/" if src else ""
        for k, e in index.entries.items():
            if k.startswith(prefix):
                rest = k[len(prefix) :]
                out[f"{dest}/{rest}" if dest else rest] = e.key
    return set(out.items())


@dataclass
class Choice:
    step: str
    group: str
    option: str
    selected: bool | None
    confidence: str
    evidence: list[str] = field(default_factory=list)
    flags: dict[str, str] = field(default_factory=dict)


@dataclass
class PatternCheck:
    condition: str
    files: list[str]  # sample of the mod-folder paths it installs
    installed: bool | None  # from the files
    expected: bool | None  # from the inferred flags


@dataclass
class Inference:
    choices: list[Choice]
    patterns: list[PatternCheck]
    flags: dict[str, str | None]  # None: depends on an unknown choice
    # (installed files from this archive that selected or undecided options account for, all)
    coverage: tuple[int, int] = (0, 0)


def _sample(paths, n=3) -> str:
    paths = sorted(paths)
    more = f" (+{len(paths) - n} more)" if len(paths) > n else ""
    return ", ".join(paths[:n]) + more


def _flags(cfg: ModuleConfig, choices: list[Choice]) -> dict[str, str | None]:
    """Flag values after the installer: the last selected setter wins; unknown if an
    undecided option could still set it."""
    out: dict[str, str | None] = {}
    for c in choices:
        for name, value in c.flags.items():
            out.setdefault(name, "")
            if c.selected is None:
                out[name] = None
            elif c.selected and out[name] is not None:
                out[name] = value
    return out


def evaluate(dep: Dependency, flags: dict[str, str | None]) -> bool | None:
    """Three-valued: ``None`` when it depends on something unknown."""
    results: list[bool | None] = []
    for name, value in dep.flags:
        cur = flags.get(name, "")
        results.append(None if cur is None else cur == value)
    results += [None] * (len(dep.files) + len(dep.other))
    results += [evaluate(d, flags) for d in dep.nested]
    if not results:
        return True
    if dep.operator.lower() == "or":
        if True in results:
            return True
        return None if None in results else False
    if False in results:
        return False
    return None if None in results else True


def _observe(pairs: set[tuple[str, Key]], observed: dict[str, Key | None]):
    """(pairs installed as-is, paths with nothing installed, paths holding other content).
    A file of unknown origin (``None``) counts as neither."""
    hits = {(d, k) for d, k in pairs if observed.get(d) == k}
    absent = {d for d, _ in pairs if d not in observed}
    other = {d for d, k in pairs if observed.get(d, k) not in (k, None)}
    return hits, absent, other


def _and_flags(dep: Dependency) -> set[tuple[str, str]]:
    """Flag values the dependency needs whatever else holds."""
    if dep.operator.lower() != "and":
        return set()
    out = set(dep.flags)
    for d in dep.nested:
        out |= _and_flags(d)
    return out


def _decide(c: Choice, selected: bool, confidence: str, why: str) -> None:
    c.selected, c.confidence = selected, confidence
    c.evidence.append(why)


class _Solver:
    """Rules over one installer and one installed mod. A state is a list of ``Choice``; rules
    decide undecided choices in place."""

    def __init__(self, cfg: ModuleConfig, index: ArchiveIndex, observed):
        self.cfg, self.observed = cfg, observed
        opts = list(cfg.options())
        self.opts = opts
        self.pairs = [expand(o.installs, index) for _, _, o in opts]
        self.obs = [_observe(ps, observed) for ps in self.pairs]
        self.pat_pairs = [expand(p.installs, index) for p in cfg.patterns]
        self.pat_obs = [_observe(ps, observed) for ps in self.pat_pairs]
        # conditional installs run last, so one that ran left its files in place, unless
        # another conditional install wrote the same path
        by_pattern: dict[str, list[tuple[int, Key]]] = {}
        for i, ps in enumerate(self.pat_pairs):
            for d, k in ps:
                by_pattern.setdefault(d, []).append((i, k))
        self.pat_not_run = [
            bool(absent)
            or (
                not hits
                and bool(other)
                and not any(
                    j != i and k == observed.get(d)
                    for d in other
                    for j, k in by_pattern[d]
                )
            )
            for i, (hits, absent, other) in enumerate(self.pat_obs)
        ]
        self.required = expand(cfg.required, index)
        self.groups = [(s, g) for s in cfg.steps for g in s.groups]
        self.in_group = {
            id(g): [i for i, (_, gg, _) in enumerate(opts) if gg is g]
            for _, g in self.groups
        }
        self.in_step = {
            id(s): [i for i, (ss, _, _) in enumerate(opts) if ss is s]
            for s in cfg.steps
        }
        self.from_archive = {(d, k) for d, k in observed.items() if k is not None}

    def initial(self) -> list[Choice]:
        cs = []
        for (s, g, o), pairs, (hits, absent, other) in zip(
            self.opts, self.pairs, self.obs
        ):
            c = Choice(s.name, g.name, o.name, None, AMBIGUOUS, flags=o.flags)
            if not pairs:
                c.evidence.append("installs no files")
            elif not hits and absent:
                _decide(
                    c,
                    False,
                    EXACT,
                    f"none of its {len(pairs)} files are in the mod: "
                    f"{_sample(absent)}",
                )
            elif not hits and len(other) == len(pairs):
                _decide(
                    c,
                    False,
                    LIKELY,
                    f"every path it writes holds other content: {_sample(other)}",
                )
            elif len(absent) > len(hits):
                _decide(
                    c,
                    False,
                    LIKELY,
                    f"most of its files aren't in the mod ({len(absent)} of "
                    f"{len(pairs)}): {_sample(absent)}",
                )
            if c.selected is None and o.type == "Required":
                _decide(c, True, EXACT, "the installer marks it Required")
            elif c.selected is None and o.type == "NotUsable":
                _decide(c, False, EXACT, "the installer marks it NotUsable")
            cs.append(c)
        return cs

    # --- helpers ---

    def flags(self, cs):
        return _flags(self.cfg, cs)

    def shown(self, step: Step, cs) -> bool | None:
        return True if step.visible is None else evaluate(step.visible, self.flags(cs))

    def live_patterns(self, cs) -> list[int]:
        """Conditional installs that may have run: nothing missing, condition not false."""
        fl = self.flags(cs)
        return [
            i
            for i, p in enumerate(self.cfg.patterns)
            if self.pat_pairs[i]
            and not self.pat_not_run[i]
            and evaluate(p.dependency, fl) is not False
        ]

    def produced(self, cs) -> set:
        out = self.required.union(
            *(ps for c, ps in zip(cs, self.pairs) if c.selected is not False)
        )
        return out.union(*(self.pat_pairs[i] for i in self.live_patterns(cs)))

    def require(self, cs, name: str, value: str, why: str) -> None:
        if value == "" or any(c.selected and c.flags.get(name) == value for c in cs):
            return
        open_ = [c for c in cs if c.selected is None and c.flags.get(name) == value]
        if len(open_) == 1:
            _decide(
                open_[0],
                True,
                LIKELY,
                f"{why}; it needs {name}={value!r}, which only " "this option sets",
            )

    # --- rules ---

    def explain(self, cs) -> None:
        """An option is selected when it alone explains installed files."""
        live = [
            i for i, c in enumerate(cs) if c.selected is not False and self.obs[i][0]
        ]
        explainers = Counter()
        for i in live:
            explainers.update(self.obs[i][0])
        fixed = self.required.union(
            *(self.pat_pairs[i] for i in self.live_patterns(cs))
        )
        for i in live:
            c = cs[i]
            if c.selected is not None:
                continue
            hits, absent, other = self.obs[i]
            own = {x for x in hits if explainers[x] == 1 and x not in fixed}
            if not own:
                continue
            replaced = len(other)
            _decide(
                c,
                True,
                LIKELY if replaced or absent else EXACT,
                f"only option that installs {len(own)} of the mod's files: "
                f"{_sample(d for d, _ in own)}",
            )
            if replaced:
                c.evidence.append(
                    f"{replaced} of its files were replaced by later installs"
                )
            if absent:
                c.evidence.append(
                    f"{len(absent)} of its files aren't in the mod (removed after "
                    f"the install?): {_sample(absent)}"
                )

    def group_rules(self, cs) -> None:
        for step, g in self.groups:
            members = [cs[i] for i in self.in_group[id(g)]]
            chosen = [c for c in members if c.selected]
            open_ = [c for c in members if c.selected is None]
            if g.type in ("SelectExactlyOne", "SelectAtMostOne") and chosen:
                for c in open_:
                    _decide(
                        c,
                        False,
                        LIKELY,
                        f"its group allows one option and "
                        f"'{chosen[0].option}' is selected",
                    )
            elif self.shown(step, cs) is not True:
                continue  # the rules below hold only for a step the user saw
            elif g.type == "SelectAll":
                for c in open_:
                    _decide(c, True, EXACT, "its group installs every option")
            elif g.type in ("SelectExactlyOne", "SelectAtLeastOne") and len(open_) == 1:
                if not chosen:
                    _decide(
                        open_[0],
                        True,
                        LIKELY,
                        "its group needs a selection and every other option is ruled out",
                    )

    def pattern_rules(self, cs) -> None:
        # installed files that only conditional installs can explain
        explained = self.required.union(
            *(ps for c, ps in zip(cs, self.pairs) if c.selected is not False)
        )
        live = self.live_patterns(cs)
        sources: dict[tuple[int, ...], set[str]] = {}
        for d, k in self.from_archive - explained:
            src = tuple(i for i in live if (d, k) in self.pat_pairs[i])
            if src:
                sources.setdefault(src, set()).add(d)
        for src, files in sources.items():
            needed = set.intersection(
                *(_and_flags(self.cfg.patterns[i].dependency) for i in src)
            )
            for name, value in sorted(needed):
                self.require(
                    cs,
                    name,
                    value,
                    f"conditional install of {_sample(files, 2)} is in the mod",
                )
        # a conditional install that didn't run: when its other conditions hold, the one
        # unknown flag can't have had the value it needs
        fl = self.flags(cs)
        for i, p in enumerate(self.cfg.patterns):
            dep = p.dependency
            if not self.pat_not_run[i] or dep.files or dep.other or dep.nested:
                continue
            if dep.operator.lower() != "and":
                continue
            unknown = [(n, v) for n, v in dep.flags if fl.get(n, "") is None]
            if len(unknown) != 1 or any(
                fl.get(n, "") != v for n, v in dep.flags if (n, v) not in unknown
            ):
                continue
            name, value = unknown[0]
            hits, absent, other = self.pat_obs[i]
            why = (
                f"isn't in the mod: {_sample(absent, 2)}"
                if absent
                else f"was replaced: {_sample(other, 2)}"
            )
            for c in cs:
                if c.selected is None and c.flags.get(name) == value:
                    _decide(
                        c,
                        False,
                        LIKELY,
                        f"the conditional install for {dep.describe()} didn't run (its "
                        f"file {why}); this option sets {name}={value!r}, which would "
                        "have run it",
                    )

    def step_rules(self, cs) -> None:
        for step in self.cfg.steps:
            if step.visible is None:
                continue
            members = [cs[i] for i in self.in_step[id(step)]]
            shown = self.shown(step, cs)
            if shown is False:
                for c in members:
                    if c.selected is None:
                        _decide(
                            c,
                            False,
                            EXACT,
                            f"its step is hidden: {step.visible.describe()}",
                        )
            elif any(c.selected for c in members):
                for name, value in sorted(_and_flags(step.visible)):
                    self.require(cs, name, value, f"step '{step.name}' was shown")

    def settle(self, cs) -> None:
        while True:
            before = [(c.selected, c.confidence) for c in cs]
            self.explain(cs)
            self.group_rules(cs)
            self.pattern_rules(cs)
            self.group_rules(cs)
            self.step_rules(cs)
            if before == [(c.selected, c.confidence) for c in cs]:
                return

    # --- last resort: try each option of a group that needs a selection ---

    def cost(self, cs) -> tuple[int, int, int]:
        """(installed files the state can't explain + files it needs that aren't there,
        files of selected options that something else overwrote, selected options)."""
        unexplained = len(self.from_archive - self.produced(cs))
        chosen = [i for i, c in enumerate(cs) if c.selected]
        missing = sum(len(self.obs[i][1]) for i in chosen)
        overwritten = sum(len(self.obs[i][2]) for i in chosen)
        return unexplained + missing, overwritten, len(chosen)

    def hypothesis(self, cs) -> bool:
        for step, g in self.groups:
            members = self.in_group[id(g)]
            if g.type not in ("SelectExactlyOne", "SelectAtLeastOne"):
                continue
            if any(cs[i].selected for i in members) or self.shown(step, cs) is not True:
                continue
            open_ = [i for i in members if cs[i].selected is None]
            if len(open_) < 2:
                continue
            trials = []
            for i in open_:
                trial = [dataclasses.replace(c, evidence=list(c.evidence)) for c in cs]
                _decide(trial[i], True, LIKELY, "")
                self.settle(trial)
                trials.append((self.cost(trial), i, trial))
            trials.sort(key=lambda t: t[0])
            (best, i, trial), (nxt, j, _) = trials[0], trials[1]
            if best == nxt:
                continue
            trial[i].evidence[-1] = (
                "its group needs a selection; trying each option, this one explains the mod "
                f"best: {best[0]} files unexplained or missing, {best[1]} overwritten, "
                f"{best[2]} options selected (next best, '{cs[j].option}': {nxt[0]}, "
                f"{nxt[1]}, {nxt[2]})"
            )
            # keep the evidence the trial produced for the other options too
            cs[:] = trial
            return True
        return False

    def run(self) -> list[Choice]:
        cs = self.initial()
        self.settle(cs)
        while self.hypothesis(cs):
            self.settle(cs)
        return cs


def infer(
    cfg: ModuleConfig, index: ArchiveIndex, observed: dict[str, Key | None]
) -> Inference:
    """``observed``: mod-folder path -> content key of the installed file (``None`` if the file
    is there but didn't come from this archive).

    Rules run until nothing changes: an option is selected when it alone explains installed
    files; group types, conditional installs and step conditions decide the rest. Where that
    isn't enough, a group that needs a selection tries each option and keeps the one that
    explains the mod best."""
    sv = _Solver(cfg, index, observed)
    cs = sv.run()

    # still undecided: say why
    selected = sv.required.union(*(ps for c, ps in zip(cs, sv.pairs) if c.selected))
    for i, c in enumerate(cs):
        hits, absent, _ = sv.obs[i]
        if c.selected is None and sv.pairs[i] and not hits and not absent:
            c.evidence.append(
                "its paths hold files whose origin is unknown (taken from another archive)"
            )
        if c.selected is not None or not hits:
            continue
        others = [
            o.option
            for j, o in enumerate(cs)
            if j != i and o.selected is not False and sv.obs[j][0] & hits
        ]
        if absent:
            _decide(
                c,
                False,
                LIKELY,
                f"{len(absent)} of its files aren't in the mod: "
                f"{_sample(absent)}; the {len(hits)} that are could come from "
                f"{', '.join(others[:3]) or 'elsewhere'}",
            )
        elif hits <= selected:
            c.evidence.append(
                "all its files are installed, but selected options or required "
                f"files install the same ({', '.join(others[:3]) or 'required'})"
            )
        else:
            c.evidence.append(
                "all its files are installed; other options could have "
                f"installed them too: {', '.join(others[:3]) or 'none'}"
            )

    flags = sv.flags(cs)
    for step in cfg.steps:
        if sv.shown(step, cs) is False:
            for i in sv.in_step[id(step)]:
                if cs[i].selected:
                    cs[i].confidence = AMBIGUOUS
                    cs[i].evidence.append(
                        f"its step should be hidden "
                        f"({step.visible.describe()}), but it is selected"
                    )

    live = set(sv.live_patterns(cs))
    checks = []
    for i, p in enumerate(cfg.patterns):
        expected = evaluate(p.dependency, flags)
        installed = (
            False if sv.pat_not_run[i] else (True if expected and i in live else None)
        )
        checks.append(
            PatternCheck(
                p.dependency.describe(),
                sorted({d for d, _ in sv.pat_pairs[i]})[:5],
                installed,
                expected,
            )
        )
    covered = sv.from_archive & sv.produced(cs)
    return Inference(cs, checks, flags, (len(covered), len(sv.from_archive)))


# --- observing an installed mod ------------------------------------------------------------


HIDDEN = ".mohidden"  # MO2 hides a file by renaming it


def _unhide(path: str) -> str:
    return path[: -len(HIDDEN)] if path.endswith(HIDDEN) else path


def _candidates(cfg: ModuleConfig, index: ArchiveIndex) -> dict[str, set[Key]]:
    """Mod-folder path -> every content the installer could put there."""
    out: dict[str, set[Key]] = {}
    lists = [cfg.required] + [p.installs for p in cfg.patterns]
    lists += [o.installs for _, _, o in cfg.options()]
    for installs in lists:
        for dest, key in expand(installs, index):
            out.setdefault(dest, set()).add(key)
    return out


def baseline_observed(
    ml: Modlist, mod: str, archive_hash: str, index: ArchiveIndex, cfg: ModuleConfig
) -> dict[str, Key | None]:
    """From the directives: each file's source path in the archive. A file Wabbajack took
    from another archive counts when its size matches exactly one candidate."""
    candidates = _candidates(cfg, index)
    out: dict[str, Key | None] = {}
    for f in ml.mods.get(mod, []):
        dest = _unhide(norm(f.path))
        key = None
        if f.archive_hash == archive_hash and len(f.archive_path) == 1:
            e = index.entries.get(norm(f.archive_path[0]))
            key = e.key if e else None
        if key is None:
            sized = [k for k in candidates.get(dest, ()) if k[0] == f.size]
            key = sized[0] if len(sized) == 1 else None
        out[dest] = key
    return out


def _crc32(path: str) -> str:
    crc = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            crc = zlib.crc32(chunk, crc)
    return f"{crc:08X}"


def folder_observed(
    folder: Path, cfg: ModuleConfig, index: ArchiveIndex
) -> dict[str, Key | None]:
    """From an installed mod folder: a file counts as an archive file when size and CRC32
    match one the installer could have put there."""
    candidates = _candidates(cfg, index)
    out: dict[str, Key | None] = {}
    for rel, (abs_path, size, _) in walk_files(folder).items():
        rel = _unhide(norm(rel))
        keys = [k for k in candidates.get(rel, ()) if k[0] == size]
        out[rel] = None
        if keys:
            crc = _crc32(abs_path)
            out[rel] = next((k for k in keys if k[1] == crc), None)
    return out


# --- per mod -------------------------------------------------------------------------------


@dataclass
class ChoiceDifference:
    step: str
    group: str
    option: str
    baseline: bool | None
    custom: bool | None
    custom_evidence: list[str]


@dataclass
class FomodReport:
    mod: str
    archive: str = ""
    module: str = ""
    baseline: Inference | None = None
    custom: Inference | None = None
    custom_archive: str = ""
    differences: list[ChoiceDifference] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def differences(baseline: Inference, custom: Inference) -> list[ChoiceDifference]:
    """Options the custom files answer differently (both sides decided)."""
    out = []
    cust = {(c.step, c.group, c.option): c for c in custom.choices}
    for b in baseline.choices:
        c = cust.get((b.step, b.group, b.option))
        if c and b.selected is not None and c.selected is not None:
            if b.selected != c.selected:
                out.append(
                    ChoiceDifference(
                        b.step, b.group, b.option, b.selected, c.selected, c.evidence
                    )
                )
    return out


def _find_index(pair, ml, mod, cache_dir, notes):
    """The mod's main archive (most files) and its index, if it has a FOMOD installer. Other
    archives only lend files that happen to match by content."""
    main = next(iter(ml.mod_archives(mod)), None)
    arc = ml.archives.get(main) if main else None
    if not arc:
        return None, None
    path = pair.downloads_dir / arc.name
    if not path.is_file():
        notes.append(f"source archive not in the downloads folder: {arc.name}")
        return None, None
    if path.stat().st_size != arc.size:
        notes.append(f"{arc.name} has a different size than the .wabbajack expects")
        return None, None
    index = archive_index(pair.sevenzip_exe, path, cache_dir)
    return (arc, index) if index.config else (None, None)


def fomod_report(
    pair,
    ml: Modlist,
    baseline_mod: str,
    custom_folder: Path | None,
    custom_installation_file: str = "",
    cache_dir: Path | None = None,
) -> FomodReport:
    """FOMOD choices of a baseline mod, and of its custom counterpart when there is one."""
    rep = FomodReport(baseline_mod)
    if pair.sevenzip_exe is None:
        rep.notes.append("7z.exe not found; set sevenzip_exe in the pair config")
        return rep
    arc, index = _find_index(pair, ml, baseline_mod, cache_dir, rep.notes)
    if not arc:
        rep.notes.append("its main source archive has no fomod/ModuleConfig.xml")
        return rep
    rep.archive = arc.name
    cfg = parse_module_config(index.config)
    rep.module = cfg.name
    rep.baseline = infer(
        cfg, index, baseline_observed(ml, baseline_mod, arc.hash, index, cfg)
    )
    if not any(c.selected for c in rep.baseline.choices):
        rep.notes.append(
            "no installer option matches the installed files; the mod probably only "
            "reuses some files of this archive"
        )
    if custom_folder is None or not custom_folder.is_dir():
        return rep

    c_cfg, c_index = cfg, index
    rep.custom_archive = arc.name
    if (
        custom_installation_file
        and custom_installation_file.lower() != arc.name.lower()
    ):
        path = pair.downloads_dir / custom_installation_file
        why = "isn't in the downloads folder"
        if path.is_file():
            why = "has no FOMOD installer"
            idx = archive_index(pair.sevenzip_exe, path, cache_dir)
            if idx.config:
                c_cfg, c_index = parse_module_config(idx.config), idx
                rep.custom_archive = path.name
        if rep.custom_archive == arc.name:
            rep.notes.append(
                f"custom was installed from {custom_installation_file}, which {why}; "
                "reading the custom files against the baseline's installer"
            )
    rep.custom = infer(c_cfg, c_index, folder_observed(custom_folder, c_cfg, c_index))
    rep.differences = differences(rep.baseline, rep.custom)
    return rep
