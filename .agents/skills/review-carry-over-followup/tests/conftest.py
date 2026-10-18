"""review-carry-over-followup のテストで使う fixture。

PR のコメントは GitHub の issue comment の API の形（``gh api --paginate --slurp`` のページの配列）で作り、
判断の記録は ``review_records.compose_comment`` で本物の状況コメントにする。``fold`` と判定フィルタは
実物（``review_records.py`` の CLI と ``jq``）を実行する。PR はすべて架空の usaturn/xrift-poc#900 である。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
FILTER = SKILL_DIR / "scripts/previous_followups.jq"
RECORDS_SCRIPT = REPO_ROOT / ".agents/scripts/review_records.py"
REPOSITORY = "usaturn/xrift-poc"
NUMBER = 900
REVIEWED_HEAD = "2222bbbb3333cccc4444dddd5555eeee6666ffff"
SHORT_HEAD = REVIEWED_HEAD[:12]
COMMIT = "7777aaaa8888bbbb9999cccc0000dddd1111eeee"

Page = list[dict[str, Any]]


def _load_records() -> Any:
    spec = importlib.util.spec_from_file_location("carry_over_review_records", RECORDS_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


RECORDS: Any = _load_records()


@dataclass
class CarryOverKit:
    """コメントの作成、``fold`` と判定フィルタの実行。入出力のファイルは ``tmp_path`` に置く。"""

    tmp_path: Path
    short_head: str = SHORT_HEAD

    @staticmethod
    def decision(decision_id: str, state: str, **extra: Any) -> dict[str, Any]:
        """1 回目の記録に書ける判断。``state`` ごとに必須の欄を足す。"""
        value: dict[str, Any] = {
            "id": decision_id,
            "sources": [decision_id.split("/")[0]],
            "summary": f"{decision_id} の要約",
            "locations": ["tools/example.py:1"],
            "state": state,
            "change": "new",
            "severity": "Low",
            "pr_relation": "introduced",
            "reason": "架空の理由",
        }
        if state == "skip":
            value["revisit_when"] = "新しい根拠が出たとき"
        if state == "unverified":
            value["unverified_reason"] = "足りない確認"
        value.update(extra)
        return value

    def comment(
        self, comment_id: int, body: str | None, *, association: str = "OWNER", login: str = "usaturn"
    ) -> dict[str, Any]:
        return {
            "id": comment_id,
            "html_url": f"https://github.com/{REPOSITORY}/pull/{NUMBER}#issuecomment-{comment_id}",
            "body": body,
            "user": {"login": login},
            "author_association": association,
            "created_at": "2026-10-18T00:00:00Z",
        }

    def status_comment(self, comment_id: int, decisions: Sequence[dict[str, Any]]) -> dict[str, Any]:
        """判断の記録を持つ 1 回目の状況コメント。``fix`` があれば ``fixes_pushed``、無ければ ``stopped``。"""
        record: dict[str, Any] = {
            "schema": "review-records/v1",
            "pr": {"repository": REPOSITORY, "number": NUMBER},
            "round": 1,
            "reviewed_head": REVIEWED_HEAD,
            "recorded_at": "2026-10-17T00:00:00Z",
            "planner": {"model": "claude-opus-5-5", "runtime": "claude-code", "agent_id": "claude-opus-5-5"},
            "seats": [
                {
                    "review_file": "claude-opus-5-5",
                    "model": "claude-opus-5-5",
                    "runtime": "claude-code",
                    "file_head": SHORT_HEAD,
                    "mode": "Initial",
                    "rerun": True,
                    "merge_recommendation": "Approve",
                }
            ],
            "decisions": list(decisions),
        }
        if any(entry["state"] == "fix" for entry in decisions):
            record.update(outcome="fixes_pushed", commit=COMMIT)
        else:
            record.update(outcome="stopped", stop_reason="external: 架空の停止")
        body = RECORDS.compose_comment(record, "## review-fix-planning（claude-opus-5-5）\n\n架空の状況コメント")
        return self.comment(comment_id, body)

    def _write(self, name: str, value: object) -> Path:
        path = self.tmp_path / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def fold(self, pages: Sequence[Page]) -> dict[str, Any]:
        """``gh api --paginate --slurp`` の出力を ``review_records.py fold`` に渡した結果。"""
        result = subprocess.run(
            [sys.executable, str(RECORDS_SCRIPT), "fold", "--repo", REPOSITORY, "--pr", str(NUMBER)],
            input=json.dumps(list(pages), ensure_ascii=False),
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    def run_filter(self, comments: object, state: dict[str, Any]) -> dict[str, Any]:
        """SKILL.md と同じ引数で判定フィルタを実行する。``comments`` は ``--slurp`` の出力（平らな配列も可）。"""
        result = subprocess.run(
            [
                "jq",
                "-n",
                "--slurpfile",
                "comments",
                str(self._write("comments.json", comments)),
                "--slurpfile",
                "state",
                str(self._write("state.json", state)),
                "-f",
                str(FILTER),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    def classify(self, pages: Sequence[Page]) -> dict[str, Any]:
        """同じコメントを ``fold`` と判定フィルタの両方に通す（スキルの手順と同じ順）。"""
        return self.run_filter(list(pages), self.fold(pages))


@pytest.fixture
def carry_over_kit(tmp_path: Path) -> CarryOverKit:
    return CarryOverKit(tmp_path)
