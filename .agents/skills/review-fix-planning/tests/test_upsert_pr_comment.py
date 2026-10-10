from __future__ import annotations

import importlib.util
import json
from collections.abc import Sequence
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

HELPER = Path(__file__).resolve().parents[1] / "scripts" / "upsert_pr_comment.py"
REPO_ROOT = Path(__file__).resolve().parents[4]
RECORDS_MODULE = REPO_ROOT / ".agents/scripts/review_records.py"


def load_helper() -> Any:
    spec = importlib.util.spec_from_file_location("upsert_pr_comment", HELPER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = load_helper()
records: Any = mod.records

MODEL = "claude-opus-5-5"
REPO = "usaturn/maatlog-poc"
PR = 485
HEAD_1 = "1" * 40
HEAD_2 = "2" * 40
COMMIT = "3" * 40
PR_URL = f"https://github.com/{REPO}/pull/{PR}"
BODY = (
    "## review-fix-planning（claude-opus-5-5）\n\n### レビュー状況\n- ok\n\n### 作業判断\n- ok\n\n### 作業結果\n- ok"
)


class FakeGh:
    def __init__(
        self,
        *,
        pr_view: dict[str, Any],
        comments: Any,
        upsert_response: dict[str, Any] | None = None,
    ) -> None:
        self.pr_view = pr_view
        self.comments = comments
        self.upsert_response = upsert_response or {"id": 99, "html_url": f"{PR_URL}#issuecomment-99"}
        self.calls: list[tuple[tuple[str, ...], str | None]] = []

    def __call__(
        self,
        args: Sequence[str],
        *,
        input_text: str | None = None,
    ) -> CompletedProcess[str]:
        argv = tuple(args)
        self.calls.append((argv, input_text))
        if argv[1:3] == ("pr", "view"):
            return CompletedProcess(list(args), 0, json.dumps(self.pr_view), "")
        if argv[1:4] == ("api", "--paginate", "--slurp"):
            return CompletedProcess(list(args), 0, json.dumps(self.comments), "")
        if argv[1:3] == ("api", "--method"):
            return CompletedProcess(list(args), 0, json.dumps(self.upsert_response), "")
        return CompletedProcess(list(args), 1, "", f"unexpected gh args: {argv}")

    def writes(self) -> list[tuple[tuple[str, ...], str | None]]:
        return [call for call in self.calls if "--method" in call[0]]

    def view_fields(self) -> str:
        argv = next(call[0] for call in self.calls if call[0][1:3] == ("pr", "view"))
        return argv[argv.index("--json") + 1]


def pr_view(*, head: str = HEAD_1, state: str = "OPEN") -> dict[str, Any]:
    return {"url": PR_URL, "number": PR, "headRefOid": head, "state": state}


def comment(comment_id: int, body: str, association: str = "OWNER", login: str = "usaturn") -> dict[str, Any]:
    return {"id": comment_id, "body": body, "user": {"login": login}, "author_association": association}


def seat(head: str) -> dict[str, Any]:
    return {
        "review_file": MODEL,
        "model": MODEL,
        "runtime": "claude-code",
        "file_head": head[:12],
        "mode": "Initial",
        "rerun": True,
        "merge_recommendation": "Request changes",
    }


def decision(ref: str, state: str, change: str = "new", **extra: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": ref,
        "sources": [ref],
        "summary": "空の入力で export が落ちる",
        "locations": ["src/export.py:10"],
        "state": state,
        "change": change,
        "severity": "High",
        "pr_relation": "introduced",
        "reason": "H: PR が持ち込んだ検証済みの High",
    }
    if state == "skip":
        value["revisit_when"] = "利用者が指定したとき"
    value.update(extra)
    return value


def record(
    round_number: int = 1,
    *,
    head: str = HEAD_1,
    outcome: str = "no_fix_needed",
    decisions: list[dict[str, Any]] | None = None,
    model: str = MODEL,
    **extra: Any,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": "review-records/v1",
        "pr": {"repository": REPO, "number": PR},
        "round": round_number,
        "reviewed_head": head,
        "recorded_at": f"2026-10-03T0{round_number}:00:00Z",
        "planner": {"model": model, "runtime": "claude-code", "agent_id": records.agent_id_for(model)},
        "seats": [seat(head)],
        "decisions": decisions or [],
        "outcome": outcome,
    }
    value.update(extra)
    return value


FIX_REF = f"{MODEL}#H-1@{HEAD_1[:12]}"


def awaiting_record() -> dict[str, Any]:
    return record(outcome="awaiting_approval", decisions=[decision(FIX_REF, "fix")])


def pushed_record() -> dict[str, Any]:
    return record(outcome="fixes_pushed", decisions=[decision(FIX_REF, "fix")], commit=COMMIT)


def status_comment(comment_id: int, value: dict[str, Any], association: str = "OWNER") -> dict[str, Any]:
    return comment(comment_id, records.compose_comment(value, BODY), association)


# --- record-less posting (the original behaviour and the loop controller) ---


def test_sanitize_replaces_slash_space_and_double_hyphen() -> None:
    assert mod.sanitize_agent_id("Cursor Grok 4.6") == "Cursor-Grok-4.6"
    assert mod.sanitize_agent_id("opencode/deepseek-v4-flash-free") == ("opencode-deepseek-v4-flash-free")
    assert mod.sanitize_agent_id("foo--bar") == "foo-bar"


def test_agent_id_comes_from_the_shared_module() -> None:
    assert Path(records.__file__).resolve() == RECORDS_MODULE.resolve()
    for model in ("Cursor Grok 4.6", "opencode/deepseek-v4-flash-free", "foo--bar", MODEL):
        assert mod.sanitize_agent_id(model) == records.agent_id_for(model)
    with pytest.raises(mod.CommentError, match="safe agent id"):
        mod.sanitize_agent_id("bad<model>")


def test_compose_body_prepends_marker() -> None:
    body = mod.compose_body("claude-opus-5", "### レビュー状況\n- ok")
    assert body.startswith("<!-- review-fix-planning:agent:claude-opus-5 -->\n")
    assert "### レビュー状況" in body


def test_compose_body_rejects_empty_and_foreign_marker() -> None:
    with pytest.raises(mod.CommentError, match="empty"):
        mod.compose_body("claude-opus-5", "   ")
    with pytest.raises(mod.CommentError, match="another agent"):
        mod.compose_body(
            "claude-opus-5",
            "<!-- review-fix-planning:agent:composer-2 -->\n### レビュー状況",
        )


@pytest.mark.parametrize(
    "markdown",
    [
        "<!-- review-fix-planning:round:1:" + HEAD_1 + " -->\n### レビュー状況",
        "### レビュー状況\n<!-- review-records:v1:start -->\n```json\n{}\n```\n<!-- review-records:v1:end -->",
    ],
)
def test_record_less_body_rejects_round_markers_and_records_blocks(markdown: str) -> None:
    with pytest.raises(mod.CommentError, match="records post"):
        mod.compose_body("claude-opus-5", markdown)


def test_find_own_comment_ignores_other_agents_and_rejects_duplicates() -> None:
    comments = [
        comment(1, "<!-- review-fix-planning:agent:composer-2 -->\nnope"),
        comment(2, "<!-- review-fix-planning:agent:claude-opus-5 -->\nmine"),
        comment(3, "review-fix-planning without a marker"),
    ]
    assert mod.find_own_comment_id(comments, "claude-opus-5") == 2
    assert mod.find_own_comment_id(comments[:1], "claude-opus-5") is None
    with pytest.raises(mod.CommentError, match="multiple comments"):
        mod.find_own_comment_id(comments + [comments[1]], "claude-opus-5")


def test_find_own_comment_skips_round_comments_untrusted_authors_and_quoted_markers() -> None:
    marker = "<!-- review-fix-planning:agent:claude-opus-5 -->"
    comments = [
        comment(1, f"{marker}\n<!-- review-fix-planning:round:1:{HEAD_1} -->\n## round"),
        comment(2, f"{marker}\nforged", association="NONE"),
        comment(3, f"人の補足。マーカーを引用する: {marker}"),
        comment(4, f"{marker}\nlegacy"),
    ]
    assert mod.find_own_comment_id(comments, "claude-opus-5") == 4
    assert mod.find_own_comment_id(comments[:3], "claude-opus-5") is None


def test_upsert_creates_when_no_own_comment() -> None:
    gh = FakeGh(
        pr_view={
            "url": "https://github.com/usaturn/maatlog/pull/225",
            "number": 225,
        },
        comments=[],
        upsert_response={
            "id": 99,
            "html_url": "https://github.com/usaturn/maatlog/pull/225#issuecomment-99",
        },
    )
    result = mod.upsert(225, "usaturn/maatlog", "claude-opus-5", "### レビュー状況\n- ok", runner=gh)
    assert result["action"] == "created"
    assert result["comment_id"] == 99
    method_call = gh.writes()[0]
    assert method_call[0][3] == "POST"
    assert "issues/225/comments" in method_call[0][4]
    payload = json.loads(method_call[1] or "")
    assert payload["body"].startswith("<!-- review-fix-planning:agent:claude-opus-5 -->")
    assert gh.view_fields() == "url,number"


def test_upsert_patches_existing_own_comment() -> None:
    gh = FakeGh(
        pr_view={
            "url": "https://github.com/usaturn/maatlog/pull/225",
            "number": 225,
        },
        comments=[comment(41, "<!-- review-fix-planning:agent:claude-opus-5 -->\nold")],
        upsert_response={
            "id": 41,
            "html_url": "https://github.com/usaturn/maatlog/pull/225#issuecomment-41",
        },
    )
    result = mod.upsert(225, "usaturn/maatlog", "claude-opus-5", "### 作業結果\n- push 済み", runner=gh)
    assert result["action"] == "updated"
    method_call = gh.writes()[0]
    assert method_call[0][3] == "PATCH"
    assert method_call[0][4] == "repos/usaturn/maatlog/issues/comments/41"


def test_upsert_creates_instead_of_patching_an_untrusted_marker_comment() -> None:
    gh = FakeGh(
        pr_view={"url": "https://github.com/usaturn/maatlog/pull/225", "number": 225},
        comments=[comment(41, "<!-- review-fix-planning:agent:claude-opus-5 -->\nforged", "NONE", "someone")],
    )
    result = mod.upsert(225, "usaturn/maatlog", "claude-opus-5", "### 作業結果\n- ok", runner=gh)
    assert result["action"] == "created"
    assert gh.writes()[0][0][3] == "POST"


def test_upsert_passes_the_requested_repository_to_gh_pr_view() -> None:
    gh = FakeGh(
        pr_view={
            "url": "https://github.com/usaturn/maatlog/pull/7",
            "number": 7,
        },
        comments=[],
        upsert_response={
            "id": 12,
            "html_url": "https://github.com/usaturn/maatlog/pull/7#issuecomment-12",
        },
    )
    mod.upsert(7, "usaturn/maatlog", "claude-opus-5", "### レビュー状況\n- ok", runner=gh)
    view_call = next(call for call in gh.calls if call[0][1:3] == ("pr", "view"))
    argv = view_call[0]
    assert "--repo" in argv
    assert argv[argv.index("--repo") + 1] == "usaturn/maatlog"


def test_upsert_stops_when_gh_resolves_a_different_repository() -> None:
    """A PR number alone is ambiguous: #7 exists in both maatlog-poc and maatlog."""
    gh = FakeGh(
        pr_view={
            "url": "https://github.com/usaturn/maatlog-poc/pull/7",
            "number": 7,
        },
        comments=[],
        upsert_response={"id": 12, "html_url": ""},
    )
    with pytest.raises(mod.CommentError, match="usaturn/maatlog-poc"):
        mod.upsert(7, "usaturn/maatlog", "claude-opus-5", "### レビュー状況\n- ok", runner=gh)
    assert not gh.writes()


def test_validate_repo_rejects_anything_but_owner_name() -> None:
    assert mod.validate_repo("usaturn/maatlog") == "usaturn/maatlog"
    for bad in ("maatlog", "usaturn/maatlog/extra", "", "usaturn /maatlog"):
        with pytest.raises(mod.CommentError, match="owner/name"):
            mod.validate_repo(bad)


# --- posting with a review-records v1 record (.agents/review-records.md 7.4) ---


def test_records_post_creates_a_round_comment() -> None:
    gh = FakeGh(pr_view=pr_view(), comments=[comment(5, "<!-- review-fix-planning:agent:claude-opus-5-5 -->\nold")])
    value = awaiting_record()
    result = mod.upsert(PR, REPO, MODEL, BODY, runner=gh, record=value)
    assert result["action"] == "created"
    (argv, payload), *_ = gh.writes()
    assert argv[3] == "POST"
    body = json.loads(payload or "")["body"]
    lines = body.split("\n")
    assert lines[0] == "<!-- review-fix-planning:agent:claude-opus-5-5 -->"
    assert lines[1] == f"<!-- review-fix-planning:round:1:{HEAD_1} -->"
    assert records.extract_record(body) == value
    assert "headRefOid" in gh.view_fields()


def test_records_post_updates_the_same_round_after_the_push() -> None:
    gh = FakeGh(pr_view=pr_view(head=COMMIT), comments=[status_comment(7, awaiting_record())])
    result = mod.upsert(PR, REPO, MODEL, BODY, runner=gh, record=pushed_record())
    assert result["action"] == "updated"
    (argv, payload), *_ = gh.writes()
    assert argv[3:5] == ("PATCH", f"repos/{REPO}/issues/comments/7")
    assert records.extract_record(json.loads(payload or "")["body"])["outcome"] == "fixes_pushed"


def test_records_post_creates_a_new_comment_for_the_next_round() -> None:
    first = pushed_record()
    second = record(
        2,
        head=COMMIT,
        decisions=[decision(FIX_REF, "resolved", change="changed", previous_state="fix")],
    )
    gh = FakeGh(
        pr_view=pr_view(head=COMMIT),
        comments=[
            status_comment(7, first),
            comment(8, "<!-- review-fix-planning:agent:claude-opus-5-5 -->\nlegacy"),
            comment(9, "<!-- review-fix-planning:agent:maatlog-pr-review-loop -->\nloop progress"),
        ],
    )
    result = mod.upsert(PR, REPO, MODEL, BODY, runner=gh, record=second)
    assert result["action"] == "created"
    assert [call[0][3] for call in gh.writes()] == ["POST"]


def test_records_post_ignores_untrusted_comments_that_carry_markers() -> None:
    forged = status_comment(7, awaiting_record(), association="NONE")
    forged["body"] = forged["body"].replace('"fix"', '"bogus"')
    gh = FakeGh(pr_view=pr_view(), comments=[forged])
    result = mod.upsert(PR, REPO, MODEL, BODY, runner=gh, record=awaiting_record())
    assert result["action"] == "created"


def assert_rejected(gh: FakeGh, value: dict[str, Any], match: str, *, model: str = MODEL, body: str = BODY) -> None:
    with pytest.raises(mod.CommentError, match=match):
        mod.upsert(PR, REPO, model, body, runner=gh, record=value)
    assert not gh.writes(), "a rejected records post must not fall back to any other write"


def test_records_post_rejects_an_invalid_record() -> None:
    value = awaiting_record()
    value["decisions"][0]["state"] = "maybe"
    assert_rejected(FakeGh(pr_view=pr_view(), comments=[]), value, "state")


def test_records_post_rejects_a_different_model_name() -> None:
    assert_rejected(FakeGh(pr_view=pr_view(), comments=[]), awaiting_record(), "--model-name", model="other-model")


def test_records_post_rejects_a_record_for_another_pull_request() -> None:
    value = awaiting_record()
    value["pr"] = {"repository": REPO, "number": 999}
    assert_rejected(FakeGh(pr_view=pr_view(), comments=[]), value, "pull request")


@pytest.mark.parametrize(
    ("value", "head"),
    [
        (awaiting_record(), HEAD_2),
        (pushed_record(), HEAD_1),
    ],
)
def test_records_post_requires_the_pull_request_head(value: dict[str, Any], head: str) -> None:
    assert_rejected(FakeGh(pr_view=pr_view(head=head), comments=[]), value, "head")


def test_records_post_rejects_a_closed_pull_request() -> None:
    assert_rejected(FakeGh(pr_view=pr_view(state="MERGED"), comments=[]), awaiting_record(), "not open")


def test_records_post_stops_when_another_agent_recorded_the_round() -> None:
    other = awaiting_record()
    other["planner"] = {"model": "gpt-6", "runtime": "codex", "agent_id": "gpt-6"}
    gh = FakeGh(pr_view=pr_view(), comments=[status_comment(7, other)])
    assert_rejected(gh, awaiting_record(), "already recorded")


def test_records_post_stops_when_a_later_round_exists() -> None:
    later = record(2, head=COMMIT, decisions=[decision(FIX_REF, "resolved", change="changed", previous_state="fix")])
    gh = FakeGh(pr_view=pr_view(head=COMMIT), comments=[status_comment(7, pushed_record()), status_comment(8, later)])
    assert_rejected(gh, pushed_record(), "can no longer be updated")


def test_records_post_requires_earlier_fix_decisions() -> None:
    gh = FakeGh(pr_view=pr_view(head=COMMIT), comments=[status_comment(7, pushed_record())])
    assert_rejected(gh, record(2, head=COMMIT), "earlier state is fix")


def test_records_post_rejects_no_fix_needed_with_pending_decisions() -> None:
    held = record(outcome="awaiting_approval", decisions=[decision(FIX_REF, "hold", severity="Medium")])
    gh = FakeGh(pr_view=pr_view(head=HEAD_2), comments=[status_comment(7, held)])
    assert_rejected(gh, record(2, head=HEAD_2), "pending")


def test_records_post_stops_on_a_broken_record_and_names_the_comment() -> None:
    broken = status_comment(7, awaiting_record())
    broken["body"] = broken["body"].replace('"schema": "review-records/v1"', '"schema": "review-records/v9"')
    gh = FakeGh(pr_view=pr_view(head=HEAD_2), comments=[broken])
    assert_rejected(gh, record(2, head=HEAD_2), "comment 7")


def test_records_post_stops_on_duplicate_round_comments() -> None:
    duplicates = [status_comment(7, awaiting_record()), status_comment(8, awaiting_record())]
    gh = FakeGh(pr_view=pr_view(), comments=duplicates)
    assert_rejected(gh, awaiting_record(), "multiple comments")


def test_records_post_rejects_markers_in_the_body() -> None:
    body = BODY + "\n<!-- review-fix-planning:round:1:" + HEAD_1 + " -->"
    assert_rejected(FakeGh(pr_view=pr_view(), comments=[]), awaiting_record(), "round marker", body=body)


def test_records_post_rejects_a_comment_over_the_github_limit() -> None:
    body = BODY + "\n" + "あ" * records.COMMENT_BODY_LIMIT
    assert_rejected(FakeGh(pr_view=pr_view(), comments=[]), awaiting_record(), "exceed the limit", body=body)


def test_records_post_accepts_multi_line_free_text() -> None:
    value = record(
        outcome="awaiting_approval",
        decisions=[decision(FIX_REF, "fix", reason="H: 1 行目\n2 行目の根拠\n\tタブ付きの行")],
    )
    gh = FakeGh(pr_view=pr_view(), comments=[])
    mod.upsert(PR, REPO, MODEL, BODY, runner=gh, record=value)
    posted = json.loads(gh.writes()[0][1] or "")["body"]
    assert records.extract_record(posted)["decisions"][0]["reason"] == "H: 1 行目\n2 行目の根拠\n\tタブ付きの行"


# --- CLI ---


def test_cli_posts_with_a_records_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    gh = FakeGh(pr_view=pr_view(), comments=[])
    monkeypatch.setattr(mod, "GhRunner", lambda: gh)
    record_file = tmp_path / "round.json"
    record_file.write_text(json.dumps(awaiting_record(), ensure_ascii=False), encoding="utf-8")
    body_file = tmp_path / "body.md"
    body_file.write_text(BODY, encoding="utf-8")
    argv = ["--pr", str(PR), "--repo", REPO, "--model-name", MODEL]
    code = mod.main([*argv, "--body-file", str(body_file), "--records-file", str(record_file)])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["action"] == "created"
    assert records.extract_record(json.loads(gh.writes()[0][1] or "")["body"]) == awaiting_record()


def test_cli_reports_every_problem_on_its_own_line_and_exits_64(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    gh = FakeGh(pr_view=pr_view(), comments=[])
    monkeypatch.setattr(mod, "GhRunner", lambda: gh)
    value = awaiting_record()
    value["decisions"][0]["state"] = "maybe"
    value["round"] = 0
    record_file = tmp_path / "round.json"
    record_file.write_text(json.dumps(value), encoding="utf-8")
    body_file = tmp_path / "body.md"
    body_file.write_text(BODY, encoding="utf-8")
    argv = ["--pr", str(PR), "--repo", REPO, "--model-name", MODEL, "--body-file", str(body_file)]
    code = mod.main([*argv, "--records-file", str(record_file)])
    assert code == 64
    errors = capsys.readouterr().err.strip().split("\n")
    assert any(line.startswith("round:") for line in errors)
    assert any("state" in line for line in errors)
    assert not gh.writes()


def test_cli_rejects_an_unreadable_records_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gh = FakeGh(pr_view=pr_view(), comments=[])
    monkeypatch.setattr(mod, "GhRunner", lambda: gh)
    body_file = tmp_path / "body.md"
    body_file.write_text(BODY, encoding="utf-8")
    bad = tmp_path / "round.json"
    bad.write_text("{not json", encoding="utf-8")
    argv = ["--pr", str(PR), "--repo", REPO, "--model-name", MODEL, "--body-file", str(body_file)]
    assert mod.main([*argv, "--records-file", str(bad)]) == 64
    assert mod.main([*argv, "--records-file", str(tmp_path / "missing.json")]) == 64
    assert not gh.writes()
