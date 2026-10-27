"""Skill-source resolution shared by the adapter launch prompts and run identities."""

import hashlib
import os
import re
import stat
from collections.abc import Iterable
from pathlib import Path

# An absolute SKILL.md under a ``skills`` directory, as codex/cursor read evidence names it.
SKILL_MD_PATH = re.compile(r"(/[^\s\"'\\]+/skills/[^\s\"'\\]+/SKILL\.md)")
# Any SKILL.md path mentioned inside tool-call text (devin/pi).
SKILL_MD_MENTION = re.compile(r"[^\s\"'`]*SKILL\.md")
_SKILL_FILE = "SKILL.md"
_SKILL_ROOTS = (".claude/skills", ".agents/skills", ".devin/skills")


def configured_skill_paths(work_dir: Path, sources: list[str], names: list[str]) -> dict[str, Path]:
    """Map each skill name to the first existing ``<source>/<name>/SKILL.md``.

    Sources are tried in the configured order; work-dir-relative sources are
    made absolute against ``work_dir``. Absolute names, names with ``..``
    components, and candidates whose resolved location leaves the source
    directory are never adopted. Names without a configured file stay absent so
    prompts keep the native-discovery fallback.
    """
    resolved: dict[str, Path] = {}
    for name in names:
        name_path = Path(name)
        if name_path.is_absolute() or ".." in name_path.parts:
            continue
        for raw in sources:
            source = Path(raw)
            source = source if source.is_absolute() else work_dir / source
            candidate = source / name / "SKILL.md"
            # A relative work_dir must still yield an absolute prompt path,
            # resolved against the current directory without canonicalizing
            # the requested skill name away.
            candidate = candidate if candidate.is_absolute() else Path.cwd() / candidate
            try:
                if not candidate.is_file():
                    continue
                boundary = source.resolve()
                target = candidate.resolve()
            except OSError:
                continue
            if not target.is_relative_to(boundary):
                continue
            resolved[name] = candidate
            break
    return resolved


def prompt_lines(work_dir: Path, sources: list[str], skills: list[str], prefix: str = "") -> list[str]:
    """Build the skill prompt block as one line per instruction.

    ``prefix`` keeps each runtime's native reference form (``$name`` for codex).
    """
    if not skills:
        return []
    resolved = configured_skill_paths(work_dir, sources, skills)
    lines = [f"Use the following skills: {', '.join(f'{prefix}{name}' for name in skills)}."]
    for name in skills:
        path = resolved.get(name)
        if path is not None:
            lines.append(
                f'Read the configured source of skill "{name}" before using it: {path}. '
                "This absolute path takes priority over native discovery."
            )
    return lines


def verified_versions(evidence: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Hash present SKILL.md bytes only when the observed content matches exactly.

    Each item is an absolute skill path and the content a successful read
    returned. The present raw bytes must decode strictly as UTF-8 and equal the
    observed content; missing, changed, truncated, or non-UTF-8 files stay
    unknown instead of being recorded with an unverified hash.
    """
    versions: dict[str, str] = {}
    for path_text, observed in evidence:
        path = Path(path_text)
        try:
            # A read-then-edit task may replace SKILL.md with a FIFO. Open without
            # blocking, then check the opened file rather than racing a path stat.
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK), "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    continue
                raw = stream.read()
            content = raw.decode("utf-8")
        except OSError, UnicodeDecodeError:
            continue
        if observed != content:
            continue
        versions.setdefault(path.parent.name, hashlib.sha256(raw).hexdigest())
    return versions


def locate_skill_md(target: str, work_dir: Path, sources: Iterable[str], *, from_work_dir: bool) -> Path | None:
    """The first existing SKILL.md a skill name or path points to.

    ``from_work_dir`` also tries the target as a path relative to ``work_dir``
    (devin/pi report observed paths); claude-code passes skill names only.
    """
    text = target.strip().strip("'\"")
    if not text:
        return None
    candidates: list[Path] = []
    direct = Path(text)
    if direct.is_absolute():
        candidates.extend((direct, direct / _SKILL_FILE))
    else:
        if from_work_dir and direct.name == _SKILL_FILE:
            candidates.append(work_dir / direct)
        for source in sources:
            base = Path(source)
            candidates.extend((base / direct, base / direct / _SKILL_FILE, base / direct.name / _SKILL_FILE))
        candidates.extend(work_dir / root / text / _SKILL_FILE for root in _SKILL_ROOTS)
        if from_work_dir:
            candidates.append(work_dir / direct)
            if direct.name != _SKILL_FILE:
                candidates.append(work_dir / direct / _SKILL_FILE)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def located_versions(
    targets: Iterable[str], work_dir: Path, sources: Iterable[str], *, from_work_dir: bool
) -> dict[str, str]:
    """``sha256:``-prefixed hashes of the SKILL.md files the targets locate now.

    Unlike ``verified_versions``, there is no observed content to compare: the
    present bytes are recorded as they are.
    """
    versions: dict[str, str] = {}
    for target in dict.fromkeys(targets):
        path = locate_skill_md(target, work_dir, sources, from_work_dir=from_work_dir)
        if path is None:
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        versions.setdefault(path.parent.name, f"sha256:{digest}")
    return versions
