"""PR最終化の順序と未完了判定。GitHubへの書込みはfake ghだけで行う。"""

from __future__ import annotations

import copy
import importlib.util
import json
from collections.abc import Sequence
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest

HELPER = Path(__file__).resolve().parents[1] / "scripts/finalize.py"
SPEC = importlib.util.spec_from_file_location("planning_finalize", HELPER)
assert SPEC is not None and SPEC.loader is not None
mod: Any = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)

REPO = "usaturn/xrift-poc"
HEAD = "1" * 40
PR = 900


def record() -> dict[str, Any]:
    return {
        "schema": "review-records/v1",
        "pr": {"repository": REPO, "number": PR},
        "round": 1,
        "reviewed_head": HEAD,
        "recorded_at": "2026-10-08T06:00:00Z",
        "planner": {"model": "fixture", "runtime": "pi", "agent_id": "fixture"},
        "seats": [
            {
                "review_file": "fixture",
                "model": "fixture",
                "runtime": "pi",
                "file_head": HEAD[:12],
                "mode": "Initial",
                "rerun": True,
                "merge_recommendation": "Approve",
            }
        ],
        "decisions": [],
        "outcome": "no_fix_needed",
    }


def checks() -> dict[str, Any]:
    return {"head": HEAD, "commands": [{"argv": ["./scripts/ci/verify-devenv.sh"], "exit_code": 0}]}


PUBLIC = "usaturn/example-world"
GREEN = {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}
FAILED = {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}
RUNNING = {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None}


def product_record() -> dict[str, Any]:
    value = record()
    value["pr"]["repository"] = PUBLIC
    return value


def status_comment(value: dict[str, Any]) -> dict[str, Any]:
    """helperが投稿するのと同じ形の、このサイクルの状況コメント。"""
    body = mod.poster.records.compose_comment(value, "状況")
    return {"id": 99, "body": body, "author_association": "OWNER", "user": {"login": "usaturn"}}


class FakeGh:
    def __init__(self, *, state: str = "OPEN", repo: str = REPO) -> None:
        self.repo = repo
        self.view: dict[str, Any] = {
            "url": f"https://github.com/{repo}/pull/{PR}",
            "number": PR,
            "headRefOid": HEAD,
            "state": state,
            "isDraft": False,
            "mergeable": "MERGEABLE",
            "mergedAt": "2026-10-08T06:05:00Z" if state == "MERGED" else None,
            "statusCheckRollup": [{"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}],
            "closingIssuesReferences": [{"number": 901, "url": f"https://github.com/{repo}/issues/901"}],
        }
        self.calls: list[tuple[str, ...]] = []
        self.comments: list[dict[str, Any]] = []
        self.issue_state = "OPEN"
        self.post_fails = False
        self.head_after_post: str | None = None
        self.issue_fails = False
        self.merge_stays_open = False
        self.list_fails = False

    def __call__(self, args: Sequence[str], *, input_text: str | None = None) -> CompletedProcess[str]:
        argv = tuple(args)
        self.calls.append(argv)
        value: Any = None
        code = 0
        if argv[1:3] == ("pr", "view"):
            value = self.view
        elif argv[1:4] == ("api", "--paginate", "--slurp"):
            code = 1 if self.list_fails else 0
            value = self.comments
        elif argv[1:3] == ("api", "--method"):
            if self.post_fails:
                code = 1
            else:
                assert input_text is not None
                body = json.loads(input_text)["body"]
                self.comments = [{"id": 99, "body": body, "author_association": "OWNER", "user": {"login": "usaturn"}}]
                value = {"id": 99, "html_url": self.view["url"] + "#issuecomment-99"}
                if self.head_after_post is not None:
                    self.view["headRefOid"] = self.head_after_post
        elif argv[1:3] == ("pr", "merge"):
            if not self.merge_stays_open:
                self.view.update(state="MERGED", mergedAt="2026-10-08T06:05:00Z")
            value = {}
        elif argv[1:3] == ("issue", "view"):
            value = {"url": f"https://github.com/{self.repo}/issues/901", "number": 901, "state": self.issue_state}
        elif argv[1:3] == ("issue", "close"):
            if self.issue_fails:
                code = 1
            else:
                self.issue_state = "CLOSED"
            value = {}
        else:
            raise AssertionError(argv)
        return CompletedProcess(list(args), code, json.dumps(copy.deepcopy(value)), "fake failure" if code else "")

    def writes(self) -> list[tuple[str, ...]]:
        return [
            call for call in self.calls if "--method" in call or call[1:3] in (("pr", "merge"), ("issue", "close"))
        ]


def test_no_fix_result_requires_post_merge_confirmation_and_issue_update() -> None:
    """--match-head-commit に検証した HEAD を渡さないと、確認の後に動いた HEAD を検証せずに黙ってマージする。"""
    gh = FakeGh()
    result = mod.finalize(record(), "## マージ前の状況\n検証済み。追加修正なし。最終化を開始。", checks(), runner=gh)
    assert result["outcome"] == "no_fix_needed" and result["issues"] == [901]
    post, merge, issue = gh.writes()
    assert "POST" in post
    assert merge[merge.index("--match-head-commit") + 1] == HEAD
    assert issue[1:3] == ("issue", "close")
    assert gh.calls[-1][1:3] == ("issue", "view")


@pytest.mark.parametrize(
    ("field", "value"),
    [pytest.param("isDraft", True, id="draft"), pytest.param("mergeable", "CONFLICTING", id="conflicting")],
)
def test_draft_or_conflicting_pr_stops_before_any_write(field: str, value: Any) -> None:
    """draft・競合中の PR を通すと、未確定の状況を投稿し、マージや関連 Issue の更新まで進めてしまう。"""
    gh = FakeGh()
    gh.view[field] = value
    result = mod.finalize(record(), "状況", checks(), runner=gh)
    assert gh.writes() == []
    assert result["outcome"] == "stopped" and result["merged"] is False


def test_head_changed_after_status_post_stops_before_merge_or_issue_update() -> None:
    """投稿中に動いた HEAD を再確認しないと、検証していない HEAD のマージや関連 Issue の更新を試みる。"""
    gh = FakeGh()
    gh.head_after_post = "2" * 40
    result = mod.finalize(record(), "## 状況\n検証済み。\n\t投稿後の HEAD を再確認する。 ", checks(), runner=gh)
    writes = gh.writes()
    assert len(writes) == 1 and writes[0][3] == "POST"
    assert mod.poster.records.extract_record(gh.comments[0]["body"]) == record()
    assert result["outcome"] == "stopped" and result["merged"] is False and result["issues"] == []


@pytest.mark.parametrize("mutation", ["head", "missing", "failed"])
def test_missing_or_wrong_verification_stops_before_network(mutation: str) -> None:
    """別の HEAD・失敗・空の検証記録を通すと、検証していない（または失敗した）HEAD を黙ってマージする。"""
    evidence = checks()
    if mutation == "head":
        evidence["head"] = "2" * 40
    elif mutation == "missing":
        evidence["commands"] = []
    else:
        evidence["commands"][0]["exit_code"] = 1
    gh = FakeGh()
    result = mod.finalize(record(), "状況", evidence, runner=gh)
    assert result["outcome"] == "stopped" and gh.calls == []


@pytest.mark.parametrize(
    "rollup",
    [
        pytest.param([GREEN, RUNNING], id="success-and-running"),
        pytest.param([{"__typename": "StatusContext", "state": "PENDING"}], id="status-context-pending"),
    ],
)
def test_product_ci_still_running_waits_and_the_rerun_finalizes(rollup: list[Any]) -> None:
    """実行中の CI を成功と扱うと、後で失敗する PR を黙ってマージする。"""
    gh = FakeGh(repo=PUBLIC)
    gh.view["statusCheckRollup"] = rollup
    first = mod.finalize(product_record(), "状況", checks(), runner=gh)
    assert first["outcome"] == "stopped" and gh.writes() == []
    # 対照: CI が終われば同じ記録でマージに進む（止まったのは CI の状態だけによる）
    gh.view["statusCheckRollup"] = [GREEN]
    second = mod.finalize(product_record(), "状況", checks(), runner=gh)
    assert second["outcome"] == "no_fix_needed"


@pytest.mark.parametrize(
    "rollup",
    [
        pytest.param([], id="no-ci"),
        pytest.param([GREEN, FAILED], id="success-and-failure"),
        pytest.param([GREEN, {"__typename": "StatusContext", "state": "ERROR"}], id="success-and-status-error"),
    ],
)
def test_product_ci_failure_or_absence_stops_without_waiting(rollup: list[Any]) -> None:
    """失敗した CI・CI の無い PR を、他の検査の成功だけで黙ってマージする。"""
    gh = FakeGh(repo=PUBLIC)
    gh.view["statusCheckRollup"] = rollup
    result = mod.finalize(product_record(), "状況", checks(), runner=gh)
    assert result["outcome"] == "stopped"
    assert gh.writes() == []


def test_comment_rejection_cannot_be_bypassed_with_manual_post() -> None:
    """状況投稿の失敗を無視してマージすると、マージ後は投稿できず、そのサイクルの記録が PR から永久に欠ける。"""
    gh = FakeGh()
    gh.post_fails = True
    result = mod.finalize(record(), "状況", checks(), runner=gh)
    assert result["outcome"] == "stopped"
    assert len(gh.writes()) == 1


def test_merge_command_success_without_merged_state_is_not_completion() -> None:
    """gh pr merge の終了コード 0 だけで完了とすると、マージされていない PR の関連 Issue を黙って close する。"""
    gh = FakeGh()
    gh.merge_stays_open = True
    assert mod.finalize(record(), "状況", checks(), runner=gh)["outcome"] == "stopped"
    assert all(call[1:3] != ("issue", "close") for call in gh.calls)


def test_merged_without_this_no_fix_record_is_stopped_but_merged() -> None:
    """内容の違う記録のまま手動でマージされた PR を完了とし、関連 Issue を黙って close する。"""
    gh = FakeGh(state="MERGED")
    # 記録ファイルを作り直した等で、PR上のno_fix_needed記録と内容が違う。
    other = record()
    other["recorded_at"] = "2026-10-08T05:59:00Z"
    gh.comments = [status_comment(other)]
    result = mod.finalize(record(), "状況", checks(), runner=gh)
    assert result["outcome"] == "stopped" and result["merged"] is True and result["issues"] == []
    assert gh.writes() == []


def test_manual_merge_after_a_stop_before_posting_is_not_no_fix_needed() -> None:
    """#597: 投稿の前に止まった後の手動マージを no_fix_needed と報告し、関連 Issue を黙って close した。"""
    gh = FakeGh()
    gh.view["mergeable"] = "UNKNOWN"
    assert mod.finalize(record(), "状況", checks(), runner=gh)["outcome"] == "stopped"
    gh.view.update(state="MERGED", mergedAt="2026-10-08T06:05:00Z")
    result = mod.finalize(record(), "状況", checks(), runner=gh)
    assert result["outcome"] == "stopped" and result["merged"] is True and result["issues"] == []
    assert gh.writes() == []


def test_issue_update_failure_is_reported_after_real_merge() -> None:
    """マージの後の失敗で merged を false に戻すと、取り消せないマージを未マージと報告し、loop の記録も誤る。"""
    gh = FakeGh()
    gh.issue_fails = True
    result = mod.finalize(record(), "状況", checks(), runner=gh)
    assert result["outcome"] == "stopped" and result["merged"] is True


def cli_args(tmp_path: Path, result: Path) -> list[str]:
    record_file = tmp_path / "record.json"
    record_file.write_text(json.dumps(record()))
    body_file = tmp_path / "body.md"
    body_file.write_text("マージ前の状況")
    checks_file = tmp_path / "checks.json"
    checks_file.write_text(json.dumps(checks()))
    return [
        "--record-file",
        str(record_file),
        "--body-file",
        str(body_file),
        "--checks-file",
        str(checks_file),
        "--result-file",
        str(result),
    ]


@pytest.mark.parametrize("destination_kind", ["symlink", "directory", "missing-parent"])
def test_invalid_result_destination_stops_before_any_gh_call(
    destination_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """結果を保存できないまま外部操作すると、マージ済みの記録が欠け、既存データも置き換えられうる。"""
    existing = tmp_path / "existing"
    existing.mkdir()
    preserved = existing / "prior.txt"
    prior = "前の結果\n\t保存したデータ "
    preserved.write_text(prior, encoding="utf-8")
    destination = tmp_path / "result.json"
    if destination_kind == "symlink":
        destination.symlink_to(preserved)
    elif destination_kind == "directory":
        destination = existing
    else:
        destination = tmp_path / "missing" / "result.json"
    gh = FakeGh()
    monkeypatch.setattr(mod.poster, "GhRunner", lambda: gh)

    status = mod.main(cli_args(tmp_path, destination))

    assert gh.calls == []
    assert status != 0
    actual = json.loads(capsys.readouterr().out)
    assert actual["outcome"] == "stopped" and actual["merged"] is False and actual["issues"] == []
    assert preserved.read_text(encoding="utf-8") == prior
    assert existing.is_dir()
    if destination_kind == "symlink":
        assert destination.is_symlink() and destination.resolve() == preserved
    elif destination_kind == "missing-parent":
        assert not destination.parent.exists()


def test_post_operation_save_failure_preserves_observed_effects_in_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """結果の保存の失敗で結果を初期値に戻すと、取り消せないマージを stdout から消す。"""
    result = tmp_path / "result.json"
    result.write_text("prior result")
    gh = FakeGh()
    monkeypatch.setattr(mod.poster, "GhRunner", lambda: gh)

    def failed_replace(source: Any, destination: Any) -> None:
        raise OSError("simulated result persistence failure")

    monkeypatch.setattr("os.replace", failed_replace)
    assert mod.main(cli_args(tmp_path, result)) == 2
    actual = json.loads(capsys.readouterr().out)
    assert actual["outcome"] == "stopped" and actual["merged"] is True and actual["issues"] == [901]
    assert result.read_text() == "prior result"


def test_known_manual_merge_is_not_lost_on_later_read_failure() -> None:
    """観測した MERGED を後の読み取りの失敗で失うと、マージ済みの PR を未マージと黙って報告する。"""
    gh = FakeGh(state="MERGED")
    gh.list_fails = True
    actual = mod.finalize(record(), "状況", checks(), runner=gh)
    assert actual["outcome"] == "stopped" and actual["merged"] is True
    assert gh.writes() == []


def test_foreign_or_malformed_closing_issue_is_never_closed() -> None:
    """別のリポジトリの閉鎖指定を同じ番号で close すると、無関係な Issue を黙って閉じる。"""
    gh = FakeGh()
    gh.view["closingIssuesReferences"] = [{"number": 901, "url": "https://github.com/other/repo/issues/901"}]
    result = mod.finalize(record(), "状況", checks(), runner=gh)
    assert result["outcome"] == "stopped"
    assert all(call[1:3] != ("issue", "close") for call in gh.calls)
