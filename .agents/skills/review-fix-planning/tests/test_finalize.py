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

REPO = "usaturn/maatlog-poc"
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


PUBLIC = "usaturn/maatlog"
GREEN = {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "SUCCESS"}
FAILED = {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}
RUNNING = {"__typename": "CheckRun", "status": "IN_PROGRESS", "conclusion": None}


def public_record() -> dict[str, Any]:
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
        self.issue_fails = False
        self.after_post: dict[str, Any] = {}
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
                self.view.update(self.after_post)
                self.after_post = {}
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


@pytest.mark.parametrize("repo", [REPO, "usaturn/maatlog"])
def test_no_fix_result_requires_post_merge_confirmation_and_issue_update(repo: str) -> None:
    gh = FakeGh(repo=repo)
    value = record()
    value["pr"]["repository"] = repo
    result = mod.finalize(value, "## マージ前の状況\n検証済み。追加修正なし。最終化を開始。", checks(), runner=gh)
    assert result["outcome"] == "no_fix_needed" and result["issues"] == [901]
    post, merge, issue = gh.writes()
    assert "POST" in post
    assert merge[merge.index("--match-head-commit") + 1] == HEAD
    # merge commitに固定する。repo-cleanup-after-mergeは祖先関係でマージ済みを判定し、
    # loopの最終化もmerge commitを使う。方式を変えるときはこのテストも合わせて更新する。
    assert "--merge" in merge and not {"--squash", "--rebase"} & set(merge)
    assert not {"--admin", "--auto", "--delete-branch"} & set(merge)
    assert issue[1:3] == ("issue", "close")
    assert gh.calls[-1][1:3] == ("issue", "view")


@pytest.mark.parametrize("mutation", ["head", "missing", "failed", "bool"])
def test_missing_or_wrong_verification_stops_before_network(mutation: str) -> None:
    evidence = checks()
    if mutation == "head":
        evidence["head"] = "2" * 40
    elif mutation == "missing":
        evidence["commands"] = []
    else:
        evidence["commands"][0]["exit_code"] = True if mutation == "bool" else 1
    gh = FakeGh()
    result = mod.finalize(record(), "状況", evidence, runner=gh)
    assert result["outcome"] == "stopped" and gh.calls == []


@pytest.mark.parametrize("condition", ["draft", "moving", "mergeable", "state"])
def test_invalid_pr_state_does_not_post_or_merge(condition: str) -> None:
    gh = FakeGh()
    key, value = {
        "draft": ("isDraft", True),
        "moving": ("headRefOid", "2" * 40),
        "mergeable": ("mergeable", "CONFLICTING"),
        "state": ("state", "CLOSED"),
    }[condition]
    gh.view[key] = value
    result = mod.finalize(record(), "状況", checks(), runner=gh)
    assert result["outcome"] == "stopped" and "wait_for" not in result
    assert gh.writes() == []


@pytest.mark.parametrize(
    "rollup",
    [
        [RUNNING],
        [{"__typename": "CheckRun", "status": "QUEUED", "conclusion": None}],
        [{"__typename": "StatusContext", "state": "PENDING"}],
        [{"__typename": "StatusContext", "state": "EXPECTED"}],
        [GREEN, RUNNING],
    ],
)
def test_public_ci_still_running_waits_and_the_rerun_finalizes(rollup: list[Any]) -> None:
    gh = FakeGh(repo=PUBLIC)
    gh.view["statusCheckRollup"] = rollup
    first = mod.finalize(public_record(), "状況", checks(), runner=gh)
    assert first["outcome"] == "stopped" and first["wait_for"] == "ci"
    assert first["stop_reason"].startswith("external:") and gh.writes() == []
    gh.view["statusCheckRollup"] = [GREEN]
    second = mod.finalize(public_record(), "状況", checks(), runner=gh)
    assert second["outcome"] == "no_fix_needed" and "wait_for" not in second
    assert [call[1:3] for call in gh.writes()] == [("api", "--method"), ("pr", "merge"), ("issue", "close")]


@pytest.mark.parametrize(
    "rollup",
    [
        [],
        [FAILED],
        [RUNNING, FAILED],
        [FAILED, RUNNING],
        [{"__typename": "StatusContext", "state": "ERROR"}],
    ],
)
def test_public_ci_failure_or_absence_stops_without_waiting(rollup: list[Any]) -> None:
    gh = FakeGh(repo=PUBLIC)
    gh.view["statusCheckRollup"] = rollup
    result = mod.finalize(public_record(), "状況", checks(), runner=gh)
    assert result["outcome"] == "stopped" and "wait_for" not in result
    assert gh.writes() == []


def test_unknown_mergeable_waits_and_the_rerun_finalizes() -> None:
    gh = FakeGh()
    gh.view["mergeable"] = "UNKNOWN"
    first = mod.finalize(record(), "状況", checks(), runner=gh)
    assert first["outcome"] == "stopped" and first["wait_for"] == "mergeable" and gh.writes() == []
    gh.view["mergeable"] = "MERGEABLE"
    second = mod.finalize(record(), "状況", checks(), runner=gh)
    assert second["outcome"] == "no_fix_needed" and second["issues"] == [901]


def test_wait_after_post_reruns_by_updating_the_same_comment() -> None:
    gh = FakeGh()
    gh.after_post = {"mergeable": "UNKNOWN"}
    first = mod.finalize(record(), "状況", checks(), runner=gh)
    assert first["outcome"] == "stopped" and first["wait_for"] == "mergeable"
    assert [call[3] for call in gh.writes()] == ["POST"]
    gh.view["mergeable"] = "MERGEABLE"
    second = mod.finalize(record(), "状況", checks(), runner=gh)
    assert second["outcome"] == "no_fix_needed" and second["comment"]["action"] == "updated"
    assert [call[3] if call[1] == "api" else call[1:3] for call in gh.writes()] == [
        "POST",
        "PATCH",
        ("pr", "merge"),
        ("issue", "close"),
    ]


def test_parent_ci_is_not_required_but_actual_verification_is() -> None:
    gh = FakeGh()
    gh.view["statusCheckRollup"] = []
    assert mod.finalize(record(), "状況", checks(), runner=gh)["outcome"] == "no_fix_needed"


def test_comment_rejection_cannot_be_bypassed_with_manual_post() -> None:
    gh = FakeGh()
    gh.post_fails = True
    result = mod.finalize(record(), "状況", checks(), runner=gh)
    assert result["outcome"] == "stopped"
    assert len(gh.writes()) == 1


def test_moving_head_after_post_is_not_merged() -> None:
    gh = FakeGh()
    gh.after_post = {"headRefOid": "2" * 40}
    assert mod.finalize(record(), "状況", checks(), runner=gh)["outcome"] == "stopped"
    assert len(gh.writes()) == 1 and "POST" in gh.writes()[0]


def test_merge_command_success_without_merged_state_is_not_completion() -> None:
    gh = FakeGh()
    gh.merge_stays_open = True
    assert mod.finalize(record(), "状況", checks(), runner=gh)["outcome"] == "stopped"
    assert all(call[1:3] != ("issue", "close") for call in gh.calls)


def test_already_merged_with_this_record_posted_only_resumes_issue_updates() -> None:
    gh = FakeGh(state="MERGED")
    gh.comments = [status_comment(record())]
    assert mod.finalize(record(), "状況", checks(), runner=gh)["outcome"] == "no_fix_needed"
    assert gh.writes() == [("gh", "issue", "close", "901", "--repo", REPO)]


@pytest.mark.parametrize("posted", ["none", "stopped", "different"])
def test_merged_without_this_no_fix_record_is_stopped_but_merged(posted: str) -> None:
    gh = FakeGh(state="MERGED")
    if posted == "stopped":
        stopped = record()
        stopped.update(outcome="stopped", stop_reason="external: PRがdraftになっている")
        gh.comments = [status_comment(stopped)]
    elif posted == "different":
        # 記録ファイルを作り直した等で、PR上のno_fix_needed記録と内容が違う。
        other = record()
        other["recorded_at"] = "2026-10-08T05:59:00Z"
        gh.comments = [status_comment(other)]
    result = mod.finalize(record(), "状況", checks(), runner=gh)
    assert result["outcome"] == "stopped" and result["merged"] is True and result["issues"] == []
    assert result["stop_reason"].startswith("external:") and "wait_for" not in result
    assert gh.writes() == []


@pytest.mark.parametrize("first_stop", ["mergeable", "draft", "public_ci"])
def test_manual_merge_after_a_stop_before_posting_is_not_no_fix_needed(first_stop: str) -> None:
    """#597 指摘2の再現: 投稿前に止まり、その間に同じHEADで手動マージされた。"""
    gh = FakeGh(repo=PUBLIC if first_stop == "public_ci" else REPO)
    value = public_record() if first_stop == "public_ci" else record()
    key, state = {
        "mergeable": ("mergeable", "UNKNOWN"),
        "draft": ("isDraft", True),
        "public_ci": ("statusCheckRollup", [RUNNING]),
    }[first_stop]
    gh.view[key] = state
    assert mod.finalize(value, "状況", checks(), runner=gh)["outcome"] == "stopped"
    gh.view.update(state="MERGED", mergedAt="2026-10-08T06:05:00Z")
    result = mod.finalize(value, "状況", checks(), runner=gh)
    assert result["outcome"] == "stopped" and result["merged"] is True and result["issues"] == []
    assert gh.writes() == []


def test_issue_update_failure_is_reported_after_real_merge() -> None:
    gh = FakeGh()
    gh.issue_fails = True
    result = mod.finalize(record(), "状況", checks(), runner=gh)
    assert result["outcome"] == "stopped" and result["merged"] is True
    assert "external:" in result["stop_reason"]


@pytest.mark.parametrize("bad_planner", [None, [], "invalid"])
def test_malformed_record_is_stopped_before_network(bad_planner: Any) -> None:
    value = record()
    value["planner"] = bad_planner
    gh = FakeGh()
    result = mod.finalize(value, "状況", checks(), runner=gh)
    assert result["outcome"] == "stopped" and gh.calls == []


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


@pytest.mark.parametrize("bad", ["missing", "directory", "symlink"])
def test_unusable_result_path_stops_before_gh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad: str,
) -> None:
    result = tmp_path / "result.json"
    target = tmp_path / "other-session.json"
    if bad == "missing":
        result = tmp_path / "missing" / "result.json"
    elif bad == "directory":
        result.mkdir()
    else:
        target.write_text("preserve")
        result.symlink_to(target)
    gh = FakeGh()
    monkeypatch.setattr(mod.poster, "GhRunner", lambda: gh)
    assert mod.main(cli_args(tmp_path, result)) == 2
    assert gh.calls == []
    if bad == "symlink":
        assert target.read_text() == "preserve"


def test_cli_saves_and_reports_actual_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = tmp_path / "result.json"
    result.write_text("prior result")
    gh = FakeGh()
    monkeypatch.setattr(mod.poster, "GhRunner", lambda: gh)
    assert mod.main(cli_args(tmp_path, result)) == 0
    actual = json.loads(capsys.readouterr().out)
    assert json.loads(result.read_text()) == actual
    assert actual["merged"] is True and actual["issues"] == [901]


def test_post_operation_save_failure_preserves_observed_effects_in_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
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
    assert actual["stop_reason"].startswith("environment:")
    assert result.read_text() == "prior result"


def test_known_manual_merge_is_not_lost_on_later_read_failure() -> None:
    gh = FakeGh(state="MERGED")
    gh.list_fails = True
    actual = mod.finalize(record(), "状況", checks(), runner=gh)
    assert actual["outcome"] == "stopped" and actual["merged"] is True
    assert actual["stop_reason"].startswith("external:")
    assert gh.writes() == []


OLD_HEAD = "0" * 40
FIX_ID = f"fixture#H-1@{OLD_HEAD[:12]}"


def earlier_fix_record() -> dict[str, Any]:
    """前のサイクルでfixにした判断を持つ記録。"""
    value = record()
    value.update(reviewed_head=OLD_HEAD, outcome="fixes_pushed", commit=HEAD, recorded_at="2026-10-08T05:00:00Z")
    value["seats"][0]["file_head"] = OLD_HEAD[:12]
    value["decisions"] = [
        {
            "id": FIX_ID,
            "sources": [FIX_ID],
            "summary": "空の入力で落ちる",
            "locations": ["scripts/x.py:1"],
            "state": "fix",
            "change": "new",
            "severity": "High",
            "pr_relation": "introduced",
            "reason": "H: PR が持ち込んだ検証済みの High",
        }
    ]
    return value


def test_blank_status_body_is_an_input_problem_found_before_network() -> None:
    gh = FakeGh()
    result = mod.finalize(record(), "  ", checks(), runner=gh)
    assert result["outcome"] == "stopped" and result["stop_reason"].startswith("environment:")
    assert gh.calls == []


def test_conflict_with_earlier_records_is_environment_and_names_the_decision() -> None:
    gh = FakeGh()
    earlier = status_comment(earlier_fix_record())
    earlier["id"] = 98
    gh.comments = [earlier]
    value = record()
    value["round"] = 2
    result = mod.finalize(value, "状況", checks(), runner=gh)
    assert result["outcome"] == "stopped" and result["stop_reason"].startswith("environment:")
    assert FIX_ID in result["stop_reason"]
    assert gh.writes() == []


def test_duplicate_round_comments_do_not_leak_markers_into_the_reason() -> None:
    gh = FakeGh()
    first, second = status_comment(record()), status_comment(record())
    second["id"] = 100
    gh.comments = [first, second]
    result = mod.finalize(record(), "状況", checks(), runner=gh)
    assert result["stop_reason"].startswith("environment:")
    assert "<!--" not in result["stop_reason"] and "-->" not in result["stop_reason"]
    assert gh.writes() == []


def test_foreign_or_malformed_closing_issue_is_never_closed() -> None:
    gh = FakeGh()
    gh.view["closingIssuesReferences"] = [{"number": 901, "url": "https://github.com/other/repo/issues/901"}]
    result = mod.finalize(record(), "状況", checks(), runner=gh)
    assert result["outcome"] == "stopped"
    assert all(call[1:3] != ("issue", "close") for call in gh.calls)
