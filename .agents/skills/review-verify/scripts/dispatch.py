"""修正の確認だけを、設定の fix-check 割り当てで同期実行する。

runner の success は応答の回収成功であり、修正や確認済み一覧の合格ではない。
self は入力を返すだけで、司令塔が実際に確認して validate する必要がある。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from tools.agent_runner import (  # noqa: E402
    Config,
    ConfigError,
    RunnerError,
    Workflow,
    load_config,
    resolve_config_path,
    run_workflow,
)
from tools.agent_runner.adapters import Adapter, load_adapter  # noqa: E402
from tools.agent_runner.config import validate_config  # noqa: E402
from tools.agent_runner.models import BatchResult  # noqa: E402
from tools.agent_runner.workspace import WorkspaceError, resolve_revision  # noqa: E402

SKILL = Path(__file__).resolve().parents[1] / "SKILL.md"
REVIEW_FINDINGS = ROOT / ".agents/scripts/review_findings.py"


class FixInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    repository: str
    from_: str = Field(alias="from")
    to: str
    fix_ids: list[str] = Field(min_length=1)
    fix_sections: str
    verified_file: str
    review_dir: str

    @field_validator("from_", "to")
    @classmethod
    def commit(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None:
            raise ValueError("from/to must be full commit hashes")
        return value

    @field_validator("fix_sections")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("fix_sections must contain the adopted changes and verification")
        return value

    @field_validator("fix_ids")
    @classmethod
    def distinct_fixes(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values) or len(set(values)) != len(values):
            raise ValueError("fix_ids must be distinct nonblank references")
        return values

    @field_validator("repository", "verified_file", "review_dir")
    @classmethod
    def absolute(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("paths must be absolute")
        return value


def select_fix_check(config: Config) -> Config:
    """固定した設定を変更せず、他の役割を起動対象から外す。"""
    config = validate_config(config, "fix")
    role = config.roles.get("fix-check")
    assignment = config.workflows["fix"].assignments.get("fix-check")
    if role is None or assignment is None:
        raise ConfigError("invalid_reference")
    if role.writes or role.result_shape != "json" or len(assignment.agents) != 1:
        raise ConfigError("invalid_config")
    projected = config.model_copy(deep=True)
    projected.roles["fix-check"].deadline_seconds = min(role.deadline_seconds, 1800)
    projected.workflows = {"fix": Workflow(assignments={"fix-check": assignment})}
    return validate_config(projected, "fix")


def observed_verifier(batch: BatchResult, config: Config, skill: Mapping[str, str]) -> dict[str, Any] | None:
    """受領した実測値だけをv2へ変換する。self/失敗に担当の成功記録を作らない。"""
    if batch.state != "success":
        return None
    result = batch.results[0]
    attempt = result.attempts[-1]
    identity = attempt.identity
    by_path: dict[str, str] = {}
    for name, version in identity.skill_versions.items():
        digest = version.removeprefix("sha256:")
        # Piはskill名→sha256:<digest>を返す。名前だけから保存パスを推測しない。
        path = (
            name
            if name.endswith("/SKILL.md") and not Path(name).is_absolute() and ".." not in Path(name).parts
            else "unknown"
        )
        digest = digest if re.fullmatch(r"[0-9a-f]{64}", digest) else "unknown"
        # v2はパスを一意にする。不明パスに複数のhashがあるときは1つを選ばない。
        # 元の名前/hashはbatch内のidentityに全件保持している。
        by_path[path] = digest if path not in by_path or by_path[path] == digest else "unknown"
    # これはdispatcher自身が読んでpromptに渡したbytes。native discoveryとは区別する。
    # 実測が別のhashを報告していたら、どちらかを選ばずunknownにする。
    observed = by_path.get(skill["path"], skill["sha256"])
    by_path[skill["path"]] = skill["sha256"] if observed == skill["sha256"] else "unknown"
    skills = [{"path": path, "sha256": digest} for path, digest in by_path.items()]
    return {
        "model": identity.model or "unknown",
        "runtime": config.agents[result.actual_agent].runtime,
        "provenance": {
            "agent_id": result.actual_agent,
            "role": "fix_check",
            "provider": identity.provider or "unknown",
            "effort": identity.effort or "unknown",
            "session_id": identity.session_id or "unknown",
            "cli_version": identity.cli_version or "unknown",
            "run_id": batch.run_id,
            "request_id": result.request_id,
            "skills": skills,
        },
    }


def time_limits(started: datetime, deadline_seconds: int) -> dict[str, Any]:
    """評価担当が新しい確認を始めてよい時刻と、結果を返す期限を求める。"""
    reserve = min(300, deadline_seconds // 2)

    def stamp(seconds: int) -> str:
        return (started + timedelta(seconds=seconds)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "started_at": stamp(0),
        "stop_new_checks_at": stamp(deadline_seconds - reserve),
        "deadline_at": stamp(deadline_seconds),
        "deadline_seconds": deadline_seconds,
        "reserve_seconds": reserve,
    }


def check_review_directories(root: Path) -> None:
    """保存入口そのもののリンクは、呼び出し側の保存先外へ書くので拒否する。"""
    if root.is_symlink() or (root / "verified").is_symlink():
        raise ValueError("review_dir and verified must not be symlinks")


def snapshot_review(root: Path) -> dict[str, str]:
    """Gitignore や repository の境界によらず保存先を観測する。内容とリンク参照先は保存しない。"""

    def inaccessible(error: OSError) -> None:
        raise error

    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    entries: dict[str, str] = {}
    try:
        for directory, dirs, files, directory_fd in os.fwalk(
            ".", dir_fd=root_fd, follow_symlinks=False, onerror=inaccessible
        ):
            relative = Path(directory)
            entries[str(relative)] = str(os.fstat(directory_fd).st_mode)
            for name in dirs + files:
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                value = ""
                if stat.S_ISLNK(info.st_mode):
                    value = hashlib.sha256(os.fsencode(os.readlink(name, dir_fd=directory_fd))).hexdigest()
                elif stat.S_ISREG(info.st_mode):
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
                    with os.fdopen(fd, "rb") as stream:
                        current = os.fstat(stream.fileno())
                        if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (
                            info.st_dev,
                            info.st_ino,
                        ):
                            raise OSError("review entry changed during snapshot")
                        digest = hashlib.sha256()
                        while chunk := stream.read(65536):
                            digest.update(chunk)
                        value = digest.hexdigest()
                entries[str(relative / name)] = f"{info.st_mode}:{value}"
    finally:
        os.close(root_fd)
    return entries


def review_changes(root: Path, before: dict[str, str]) -> list[str]:
    """監査不能も無変更とはしない。観測値から書込み元を断定せず、後片付けもしない。"""
    try:
        after = snapshot_review(root)
    except OSError:
        return ["<review snapshot unavailable>"]
    return sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))


def dispatch(
    config: Config,
    request: Mapping[str, Any],
    output_dir: Path,
    *,
    adapter_loader: Callable[[str], Adapter] = load_adapter,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    config = select_fix_check(config)
    limits = time_limits(clock(), int(config.roles["fix-check"].deadline_seconds))
    data = FixInput.model_validate(request)
    repository = Path(data.repository)
    try:
        for revision in (data.from_, data.to):
            if resolve_revision(repository, revision) != revision:
                raise ValueError("revision is not a commit")
    except WorkspaceError:
        raise ValueError("repository/from/to cannot be resolved") from None
    try:
        raw: object = json.loads(Path(data.verified_file).read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ValueError("verified_file is not valid JSON") from None
    verified = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
    if verified.get("kind") != "verified":
        raise ValueError("verified_file must contain a verified document")
    review_dir = Path(data.review_dir)
    check_review_directories(review_dir)
    review_snapshot = snapshot_review(review_dir)
    # sha256sumと一致するよう、ファイルのバイト列からhashを求め、promptにはそのバイト列を読んだ本文を入れる。
    raw_skill = SKILL.read_bytes()
    body = raw_skill.decode("utf-8")
    skill = {"path": ".agents/skills/review-verify/SKILL.md", "sha256": hashlib.sha256(raw_skill).hexdigest()}
    prompt = (
        "Use review-verify in mode fix_check under the following complete input. "
        "For external execution the caller attaches observed verifier metadata after the response; "
        "leave unobservable identity unknown rather than pretending this is self. "
        "For self the caller executes these instructions. The provenance role is fix_check, not fix-check. "
        "Do not invoke another agent. Use only committed from/to and isolated execution. "
        "writes=False prohibits modifying the provided checkout, NOT your own temporary probes. "
        "You are explicitly authorized to create unique /tmp directories, extract committed archives there, "
        "build/test/lint them and save generated artifacts, candidate JSON and validation logs there. "
        "Do not downgrade executable checks to unchecked merely because they need temporary output. "
        "Use Python 3.14 or later for uv; never install an older interpreter. "
        "Credential/environment examples may run only with fake commands and one-shot temporary HOME. "
        "No GitHub writes, network probes, push, commit, or repairs. "
        "Check time_limits with `date -u` between checks; once stop_new_checks_at has passed, start no new check, "
        "record the remaining ones as unchecked and in coverage.remaining, and return before deadline_at. "
        "Return one JSON object with keys verified (fix_check document) and inventory (candidate sidecar). "
        "Write the candidate sidecar with the statement-candidates tool into your own unique /tmp directory "
        "and return its content unchanged as inventory; the caller saves it. "
        "Do not remove or rewrite the tool's candidates, changed_files, claims or terms: "
        "only append terms and candidates you found yourself after the tool's. "
        "Set statement_candidates.file in verified to <first 12 characters of to>.statement-candidates.json. "
        "tool_root and skill_dir identify the exact current helper/rule definitions; read only those from there. "
        "Do not read target implementation, evaluation records or other task data from tool_root. "
        "Use the runner worktree as TARGET_REPO_ROOT. Use /tmp unique directories for probes and output; "
        "do not read the original repository checkout for target code. "
        "Input paths identify provenance, their contents are already supplied below. "
        "Do not write to the caller's review directory; the caller validates and publishes your payload.\n\n"
        + json.dumps(
            {
                "request": data.model_dump(by_alias=True),
                "verified": verified,
                "skill": skill,
                "time_limits": limits,
                "tool_root": str(ROOT),
                "skill_dir": str(SKILL.parent),
            },
            ensure_ascii=False,
        )
        + "\n\nApplied skill:\n"
        + body
    )
    batch = run_workflow(
        config,
        "fix",
        prompt,
        repository,
        revision=data.to,
        concurrency=1,
        output_dir=output_dir,
        adapter_loader=adapter_loader,
    )
    result: dict[str, Any] = {
        "batch": batch.model_dump(mode="json"),
        "request": data.model_dump(by_alias=True),
        "verified": verified,
        "skill": skill,
        "time_limits": limits,
        "verifier": observed_verifier(batch, config, skill),
        "review_snapshot": review_snapshot,
        "review_changes": (
            review_changes(review_dir, review_snapshot)
            if all(attempt.stop_confirmed for result in batch.results for attempt in result.attempts)
            else None
        ),
    }
    # selfにも実際の作業指示を渡す。検証結果や実測identityを捏造しない。
    if batch.state == "self":
        result["prompt"] = prompt
    (batch.artifact_dir / "dispatch.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return result


REQUEST_ERRORS = frozenset(
    {
        "revision is not a commit",
        "repository/from/to cannot be resolved",
        "verified_file is not valid JSON",
        "verified_file must contain a verified document",
    }
)


def failure_category(error: Exception) -> str:
    """失敗の分類だけを返す。値・パス・内部の詳細は含めない。"""
    if isinstance(error, ConfigError | RunnerError):
        return f"{type(error).__name__}: {error.category}"
    if isinstance(error, ValidationError):
        fields = sorted({".".join(str(part) for part in item["loc"]) or "request" for item in error.errors()})
        return f"invalid request fields: {', '.join(fields)}"
    if isinstance(error, ValueError) and str(error) in REQUEST_ERRORS:
        return str(error)
    return type(error).__name__


class AcceptError(Exception):
    """受け取らない理由（秘密を含まない 1 行）と終了コード。"""

    def __init__(self, reason: str, code: int = 1) -> None:
        super().__init__(reason)
        self.code = code


def load_review_findings() -> Any:
    spec = importlib.util.spec_from_file_location("review_findings", REVIEW_FINDINGS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, module)
    spec.loader.exec_module(module)
    return module


def read_dispatch(run_dir: Path) -> dict[str, Any]:
    try:
        raw: object = json.loads((run_dir / "dispatch.json").read_text(encoding="utf-8"))
    except OSError, ValueError:
        raise AcceptError("dispatch.json cannot be read", 2) from None
    if not isinstance(raw, dict):
        raise AcceptError("dispatch.json is not an object", 2)
    return cast(dict[str, Any], raw)


def check_batch(batch: Mapping[str, Any]) -> None:
    """停止確認前には保存先も読み直さない。"""
    if batch.get("state") != "success":
        raise AcceptError(f"batch state is {batch.get('state')}, not success")
    attempts = [attempt for result in batch["results"] for attempt in result["attempts"]]
    if not attempts or not all(attempt.get("stop_confirmed") is True for attempt in attempts):
        raise AcceptError("an attempt did not confirm its stop")
    if batch.get("outside_changes"):
        raise AcceptError("a checkout registered before the run was changed")


def final_payload(batch: Mapping[str, Any]) -> dict[str, Any]:
    """検査済み batch の最後の payload の形式を確かめる。"""
    payload: object = batch["results"][0]["attempts"][-1].get("payload")
    if not isinstance(payload, dict):
        raise AcceptError("payload must be an object with exactly verified and inventory objects")
    members = cast(dict[str, object], payload)
    if set(members) != {"verified", "inventory"} or not all(isinstance(value, dict) for value in members.values()):
        raise AcceptError("payload must be an object with exactly verified and inventory objects")
    return cast(dict[str, Any], members)


def check_inventory(findings: Any, data: FixInput, inventory: Mapping[str, Any]) -> None:
    """担当の inventory が、道具の出力を消さず書き換えず、後ろに足しただけであることを確かめる。"""
    try:
        expected = findings.statement_candidates(
            Path(data.repository),
            data.from_,
            data.to,
            generated_at=inventory["generated_at"],
            claims={"fix_ids": inventory["fix_ids"], "claims": inventory["claims"]},
        )
    except KeyError, TypeError:
        raise AcceptError("inventory lacks generated_at, fix_ids or claims") from None
    except findings.FindingsError as error:
        raise AcceptError(f"inventory cannot be regenerated: {error.problems[0]}") from None
    for key in ("schema", "kind", "from", "to", "fix_ids", "claims", "changed_files", "common_terms"):
        if inventory.get(key) != expected[key]:
            raise AcceptError(f"inventory {key} differs from the tool output")
    terms = inventory.get("terms")
    if not isinstance(terms, list) or any(term not in terms for term in expected["terms"]):
        raise AcceptError("inventory terms do not include the tool terms")
    found = inventory.get("candidates")
    if not isinstance(found, list) or found[: len(expected["candidates"])] != expected["candidates"]:
        raise AcceptError("inventory candidates do not start with the tool candidates")
    if set(inventory["fix_ids"]) != set(data.fix_ids):
        raise AcceptError("inventory fix_ids differ from the request fix_ids")


def accept(run_dir: Path) -> dict[str, str]:
    """外部担当の結果を検査し、通ったものだけ ``<review_dir>/verified/`` へ保存する。"""
    saved = read_dispatch(run_dir)
    try:
        batch = cast(Mapping[str, Any], saved["batch"])
        data = FixInput.model_validate(saved["request"])
        observed = saved["verifier"]
        before = saved["review_snapshot"]
    except KeyError, ValidationError:
        raise AcceptError("dispatch.json lacks batch, request, verifier or review_snapshot", 2) from None
    check_batch(batch)
    review_dir = Path(data.review_dir)
    if not review_dir.is_dir():
        raise AcceptError("review_dir does not exist", 2)
    if not isinstance(before, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in cast(dict[object, object], before).items()
    ):
        raise AcceptError("dispatch.json has an invalid review_snapshot", 2)
    changes = review_changes(review_dir, cast(dict[str, str], before))
    if changes:
        raise AcceptError(
            "review_dir changed; do not treat these paths as previously accepted results: "
            + json.dumps(changes, ensure_ascii=True)
        )
    try:
        check_review_directories(review_dir)
    except ValueError:
        raise AcceptError("review_dir and verified must not be symlinks") from None
    payload = final_payload(batch)
    findings = load_review_findings()
    inventory = cast(dict[str, Any], payload["inventory"])
    check_inventory(findings, data, inventory)
    document = cast(dict[str, Any], payload["verified"])
    document["verifier"] = observed
    stem = data.to[:12]
    verified_dir = review_dir / "verified"
    verified_dir.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".accept-", dir=verified_dir))
    try:
        sidecar = work / f"{stem}.statement-candidates.json"
        target = work / f"{stem}.fix-check.json"
        sidecar.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        target.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            checked = findings.validate_document(document)
            if checked.get("mode") != "fix_check":
                raise AcceptError("verified is not a fix_check document")
            findings.check_statement_judgments(checked, cast(dict[str, Any], json.loads(sidecar.read_text("utf-8"))))
        except findings.FindingsError as error:
            raise AcceptError(f"validate failed: {error.problems[0]} (+{len(error.problems) - 1} more)") from None
        # sidecar を先に置き、fix-check が候補ファイルを欠いた状態で見えないようにする。
        os.replace(sidecar, verified_dir / sidecar.name)
        os.replace(target, verified_dir / target.name)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return {"statement_candidates": str(verified_dir / sidecar.name), "fix_check": str(verified_dir / target.name)}


def main(argv: list[str] | None = None, *, adapter_loader: Callable[[str], Adapter] = load_adapter) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments[:1] == ["accept"]:
        accepting = argparse.ArgumentParser(prog="dispatch.py accept", description=accept.__doc__)
        accepting.add_argument("--run-dir", required=True, type=Path)
        try:
            saved = accept(accepting.parse_args(arguments[1:]).run_dir)
        except AcceptError as error:
            print(f"fix-check accept: not accepted: {error}", file=sys.stderr)
            return error.code
        print(json.dumps(saved, ensure_ascii=False))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, default=ROOT / ".devcontainer/agent-runner.toml")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--assign", help="fix-checkの担当agent_idかself。fallbackは設定のまま")
    args = parser.parse_args(arguments)
    personal = args.config
    if personal is None:
        candidate = resolve_config_path()
        personal = candidate if candidate.exists() else None
    try:
        config = load_config(
            args.template, personal, assignments={"fix-check": [args.assign]} if args.assign else None, workflow="fix"
        )
        request = json.loads(args.request.read_text(encoding="utf-8"))
        result = dispatch(config, request, args.output_dir, adapter_loader=adapter_loader)
    except (ConfigError, RunnerError, OSError, ValueError) as error:
        print(
            f"fix-check: configuration/input/run failed ({failure_category(error)}); "
            "no verification success is implied",
            file=sys.stderr,
        )
        return 2
    batch = result["batch"]
    outside = batch["outside_changes"]
    changes = result["review_changes"]
    print(
        json.dumps(
            {
                "state": batch["state"],
                "artifact_dir": batch["artifact_dir"],
                "outside_changes": outside,
                "review_changes": changes,
            }
        )
    )
    return 0 if batch["state"] in ("self", "success") and not outside and changes == [] else 1


if __name__ == "__main__":
    raise SystemExit(main())
