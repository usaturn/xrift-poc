"""Configured skill-source resolution shared by the adapter launch prompts."""

import hashlib
import os
import stat
from collections.abc import Iterable
from pathlib import Path


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
