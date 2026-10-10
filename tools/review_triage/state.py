"""送信対象 state の組み立て（#383 input）。

1 指摘または重複候補 1 組ごとに ``PreparedState`` を 1 件作る純粋関数。
ファイル・ネットワーク・環境変数を読まず、committed 内容は呼び出し側が渡す。
``allowed_source_hashes`` の要素は ``PreparedState.content_sha256`` である。
つまり人間の送信許可は、実際に送る節の全文（``canonical_sections``）に結び付く。
秘密の検出は補助であり、一致しないことを送信許可の根拠にしない。
"""

import hashlib
import re
import unicodedata
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from typing import Literal, NamedTuple

from tools.review_triage.models import (
    STATE_CHAR_LIMIT,
    FindingStatus,
    PreparedState,
    ReviewBatch,
    ReviewFinding,
    StateSource,
    state_digest,
)

_CONTEXT_KEY = re.compile(
    r"(?P<kind>blob|diff):(?P<path>[^#]+)(?:#L(?P<start>[1-9][0-9]*)(?:-L(?P<end>[1-9][0-9]*))?)?"
)
_DRIVE = re.compile(r"[A-Za-z]:")
# パスに含まれていればパス逸脱とする Unicode の一般カテゴリ（R-16・R-34）。
# 制御文字（Cc）・書式文字（Cf: 双方向制御・ゼロ幅文字）・行区切り（Zl）・段落区切り（Zp）は、人間が承認する
# evidence の `--- <key>` 見出しで改行や表示順を偽装でき、見た目が実際に送る全文と変わるため。
# 対になっていないサロゲート（Cs）は、UTF-8 に符号化できず StateSource の検証で例外になるため
_PATH_REJECTED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
# 検出器はどれも線形時間で走る形に限る。キーワードの前に長さ無制限の識別子文字を置くと、同じ文字の長い連続で
# 二乗時間になるため、名前の前後は長さに上限のある選択肢だけで広げる（R-33）
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----")),
    ("aws_access_key_id", re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![0-9A-Za-z])")),
    ("github_token", re.compile(r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("slack_token", re.compile(r"(?<![A-Za-z0-9])xox[abposr]-[A-Za-z0-9-]{10,}")),
    ("sk_api_key", re.compile(r"(?<![A-Za-z0-9_-])sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}")),
    ("google_api_key", re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}")),
    ("jwt", re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("bearer_token", re.compile(r"(?i)authorization[\"']?\s*:\s*[\"']?bearer\s+[A-Za-z0-9._~+/-]{16,}")),
    (
        "assigned_secret",
        re.compile(
            r"(?i)(?:api[_-]?key|secret(?:[_-]?(?:access[_-]?)?key)?|token|passw(?:or)?d)"
            r"[\"']?\s*[:=]\s*([\"'])[^\"'\s]{12,}\1"
        ),
    ),
)


class _ContextEntry(NamedTuple):
    key: str
    kind: Literal["blob", "diff"]
    path: str
    line_start: int | None
    line_end: int | None
    value: str


def prepare_state(
    batch: ReviewBatch,
    *,
    finding_ids: Sequence[str],
    committed_context: Mapping[str, str],
    allowed_source_hashes: AbstractSet[str],
) -> PreparedState:
    """active な 1 指摘（finding）または 2 指摘（pair）の送信候補を作る。

    ``committed_context`` のキーは ``blob:<path>[#L<a>[-L<b>]]`` か ``diff:<path>``、値は抜粋の本文。
    呼び出し側の誤りは ``ValueError``（未知の ID は ``KeyError``）にする。
    送れない内容は ``allowed=False`` とし、値を含まない理由を ``blocked_reasons`` に残す。
    """
    findings = _active_findings(batch, finding_ids)
    entries = _parse_context(committed_context)
    reasons: list[str] = []
    purpose: Literal["finding", "pair"]
    if len(findings) == 1:
        purpose, ordered = "finding", findings
        usable, escaped = _split_path_escapes(entries)
        reasons.extend(f"path_escape:{ascii(key)}" for key in sorted(entry.key for entry in escaped))
        sections, sources = _finding_sections(batch, findings[0], usable)
        if "evidence" not in sections:
            reasons.append("missing_evidence")
    else:
        if entries:
            raise ValueError("pair states take no committed_context in question set v1")
        purpose = "pair"
        ordered = sorted(findings, key=lambda finding: finding.finding_key)
        sections = {"finding_a": ordered[0].body, "finding_b": ordered[1].body}
        sources = [_review_source(batch, finding) for finding in ordered]
    content_sha256, char_count, byte_count = state_digest(sections)
    reasons.extend(_secret_reasons(sections))
    if char_count > STATE_CHAR_LIMIT:
        reasons.append(f"too_large:{char_count}>{STATE_CHAR_LIMIT}")
    approved = content_sha256 in allowed_source_hashes
    if not approved:
        reasons.append("not_approved")
    return PreparedState(
        purpose=purpose,
        finding_ids=tuple(finding.finding_id for finding in ordered),
        sections=sections,
        sources=tuple(sources),
        content_sha256=content_sha256,
        char_count=char_count,
        byte_count=byte_count,
        allowed=not reasons,
        allowed_by=(content_sha256,) if approved else (),
        blocked_reasons=tuple(reasons),
    )


def _active_findings(batch: ReviewBatch, finding_ids: Sequence[str]) -> list[ReviewFinding]:
    # str も Sequence[str] なので型検査を通る。文字ごとに分解すると原因の分かりにくい KeyError などになるため先に止める
    if isinstance(finding_ids, str):
        raise ValueError(f"finding_ids must be a sequence of finding ids, not a single str: {finding_ids!r}")
    ids = list(finding_ids)
    if len(ids) not in (1, 2) or len(set(ids)) != len(ids):
        raise ValueError(f"finding_ids must be one id or two distinct ids: {ids!r}")
    findings = [batch.finding(finding_id) for finding_id in ids]
    for finding in findings:
        if finding.status is not FindingStatus.ACTIVE:
            raise ValueError(f"{finding.finding_id} is {finding.status.value}; only active findings can be sent")
    return findings


def _parse_context(committed_context: Mapping[str, str]) -> list[_ContextEntry]:
    entries: list[_ContextEntry] = []
    for key, value in committed_context.items():
        match = _CONTEXT_KEY.fullmatch(key)
        if match is None:
            raise ValueError(f"committed_context key must be blob:<path>[#L<a>[-L<b>]] or diff:<path>: {key!r}")
        kind: Literal["blob", "diff"] = "blob" if match.group("kind") == "blob" else "diff"
        start: int | None = None
        end: int | None = None
        # 正規表現では end の群が start の群の内側にあるので、end だけがある一致は無い
        if (start_text := match.group("start")) is not None:
            if kind == "diff":
                raise ValueError(f"committed_context diff keys take no line range: {key!r}")
            start = int(start_text)
            end = int(match.group("end") or start_text)
            if end < start:
                raise ValueError(f"committed_context key has an invalid line range: {key!r}")
        entries.append(_ContextEntry(key, kind, match.group("path"), start, end, value))
    return sorted(
        entries, key=lambda entry: (entry.kind, entry.path, entry.line_start or 0, entry.line_end or 0, entry.key)
    )


def _split_path_escapes(entries: Sequence[_ContextEntry]) -> tuple[list[_ContextEntry], list[_ContextEntry]]:
    """(state に入れてよい項目, パス逸脱の項目) に分ける。項目ごとにパスを 1 回だけ判定する。"""
    usable: list[_ContextEntry] = []
    escaped: list[_ContextEntry] = []
    for entry in entries:
        (usable if _is_repository_path(entry.path) else escaped).append(entry)
    return usable, escaped


def _is_repository_path(path: str) -> bool:
    """正規化されたリポジトリ相対の POSIX パスか。偽ならパス逸脱として扱う。"""
    if path.startswith(("/", "~")) or "\\" in path or _DRIVE.match(path):
        return False
    if any(unicodedata.category(char) in _PATH_REJECTED_CATEGORIES for char in path):
        return False
    return all(part not in ("", ".", "..") for part in path.split("/"))


def _finding_sections(
    batch: ReviewBatch, finding: ReviewFinding, entries: Sequence[_ContextEntry]
) -> tuple[dict[str, str], list[StateSource]]:
    sections = {"finding": finding.body}
    sources = [_review_source(batch, finding)]
    for section, kind in (("evidence", "blob"), ("diff", "diff")):
        selected = [entry for entry in entries if entry.kind == kind]
        if selected:
            sections[section] = "\n".join(f"--- {entry.key}\n{entry.value}" for entry in selected)
            sources.extend(_context_source(entry) for entry in selected)
    return sections, sources


def _secret_reasons(sections: Mapping[str, str]) -> list[str]:
    """節ごとに検出器を順に当てる。理由には一致した文字列を入れない。"""
    return [
        f"secret_suspected:{key}:{name}"
        for key, text in sections.items()
        for name, pattern in _SECRET_PATTERNS
        if pattern.search(text)
    ]


def _review_source(batch: ReviewBatch, finding: ReviewFinding) -> StateSource:
    data = finding.body.encode("utf-8")
    return StateSource(
        kind="review",
        path=f"{batch.review_dir}/{finding.source_file}",
        sha256=hashlib.sha256(data).hexdigest(),
        line_start=finding.line_start,
        line_end=finding.line_end,
        byte_count=len(data),
    )


def _context_source(entry: _ContextEntry) -> StateSource:
    data = entry.value.encode("utf-8")
    return StateSource(
        kind=entry.kind,
        path=entry.path,
        sha256=hashlib.sha256(data).hexdigest(),
        line_start=entry.line_start,
        line_end=entry.line_end,
        byte_count=len(data),
    )
