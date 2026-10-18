#!/usr/bin/env python3
"""レビュー対象の PR と、レビュー成果物の保存先・Git 操作の対象を解決する（Issue #483）。

サブコマンド:

- ``resolve <PR>``: PR を GitHub に照会し、保存先と Git 操作の対象を JSON で出す（pr-review が使う）
- ``review-dir <PR|パス> [--for-planning]``: 保存済みのレビューディレクトリを探す（review-fix-planning・review-verify）
- ``clone --target <JSON> --dest <パス> [--checkout-branch]``: 対象の PR のリポジトリを一時 clone する
- ``scan-context --target <JSON> --checkout <パス>``: push 前に PR と checkout を照合し base/head ref を返す

PR の指定は、番号（``3``・``#3``。親の origin のリポジトリの PR）か PR の URL（そのリポジトリの PR）。
保存先は親の main worktree（``agent_tmp.py root`` と同じ）の
``reviews/<owner>/<repo>/#<PR 番号>-<変換後の head ブランチ名>/`` である。``owner/repo`` は PR が存在する側
（base）の正式名で、fork の head 側は使わない。旧配置（親の ``reviews/#<N>-*`` と、登録済み submodule の
``reviews/#<N>-*``）は、明示したパスでだけ読む。

終了コード: 0 成功、2 引数の誤り、3 PR が存在しない、4 リポジトリが存在しないかアクセス権が無い、
5 認証・権限の問題、6 通信・API の失敗、7 親の main worktree・origin・submodule を使えない、
8 PR はあるが保存済みレビューが無い、9 候補が複数、10 オーナーが対象外、
11 パスから PR を決められない・食い違う、12 clone・fetch の失敗。

標準ライブラリだけで書く（``agent_tmp.py``・``review_records.py`` と同じく、スキルの外にある依存として扱う）。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple, cast

EXIT_USAGE = 2
EXIT_PR_NOT_FOUND = 3
EXIT_REPOSITORY_NOT_FOUND = 4
EXIT_AUTH = 5
EXIT_API = 6
EXIT_PARENT = 7
EXIT_NO_REVIEW = 8
EXIT_AMBIGUOUS = 9
EXIT_OWNER = 10
EXIT_PATH = 11
EXIT_CLONE = 12

# review-fix-planning と review-verify が扱う PR のオーナー（利用者の決定。Issue #483）。pr-review は使わない
PLANNING_OWNERS = frozenset({"usaturn"})
REVIEWS_DIR = "reviews"

_OWNER = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*")
_REPO = re.compile(r"[A-Za-z0-9._-]+")
_MODEL_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# Path separators, Windows-illegal name chars, and C0 controls.
_UNSAFE_DIRECTORY_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_SHA = re.compile(r"[0-9a-f]{40}")
_NUMBER = r"(?P<number>[1-9][0-9]*)"
_BARE = re.compile(rf"#?{_NUMBER}")
_PULL_URL = re.compile(rf"https://github\.com/(?P<owner>[^/?#\s]+)/(?P<repo>[^/?#\s]+)/pull/{_NUMBER}(?:[/?#]\S*)?")
_GITHUB_REPOSITORY_PATH = r"(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?"
# git clone に渡してよい GitHub の URL（resolve が書く clone_url はこの形）
_CANONICAL_URLS = (
    re.compile(rf"https://github\.com/{_GITHUB_REPOSITORY_PATH}"),
    re.compile(rf"(?:ssh://)?git@github\.com[:/]{_GITHUB_REPOSITORY_PATH}"),
)
# remote の設定値として読む書き方。正規形に加えて http・ユーザ情報付き・大文字のホスト名（usaturn/xrift-poc#7）。
# ホスト名が github.com と読み取れるものに限り、SSH のホスト別名は読まない
_REMOTE_URLS = (
    re.compile(rf"https?://(?:[^@/\s]+@)?(?i:github\.com)/{_GITHUB_REPOSITORY_PATH}"),
    re.compile(rf"(?:ssh://)?git@(?i:github\.com)[:/]{_GITHUB_REPOSITORY_PATH}"),
)
_STATUS_LINE = re.compile(r"HTTP/[0-9.]+ (?P<status>[0-9]{3})\b")
# 自分で作って消す一時のリポジトリに、作った時点で書く設定（#64）。git は fetch・commit の後に
# ``git maintenance run --auto --detach`` を切り離して起動し、その子が objects/ に書き込む。
# 削除と重なると rmtree が ENOTEMPTY で失敗し、作りかけの残骸も残るので、以後の操作で起動させない
NO_AUTO_MAINTENANCE = ("maintenance.auto", "false")
_REVIEW_DIR_NAME = re.compile(rf"#{_NUMBER}-.+")


class TargetError(Exception):
    """解決できない。``code`` は終了コード。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), check=False, capture_output=True, text=True)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(cwd), *args], check=False, capture_output=True, text=True)


# 名前の規則（pr-review の helper もここを使う）


def validate_repository(value: str) -> str:
    """``owner/repo`` を GitHub の名前に使える文字だけに限る。"""
    owner, separator, name = value.partition("/")
    if not separator or _OWNER.fullmatch(owner) is None or _REPO.fullmatch(name) is None or name in {".", ".."}:
        raise TargetError(EXIT_USAGE, f"GitHub の owner/repo として使えない名前です: {value!r}")
    return value


def same_repository(left: str | None, right: str | None) -> bool:
    """GitHub の名前は大文字小文字を区別しない。"""
    return left is not None and right is not None and left.lower() == right.lower()


def normalize_head_branch(value: str) -> str:
    if not value or value.startswith("/") or value.endswith("/") or value in {".", ".."}:
        raise TargetError(EXIT_USAGE, "head branch cannot produce a safe review directory")
    normalized = _UNSAFE_DIRECTORY_CHARS.sub("-", value).rstrip(" .")
    if not normalized or normalized in {".", ".."}:
        raise TargetError(EXIT_USAGE, "head branch cannot produce a safe review directory")
    return normalized


def review_directory_name(pr_number: int, head_branch: str) -> str:
    if type(pr_number) is not int or pr_number < 1:
        raise TargetError(EXIT_USAGE, "PR number cannot produce a safe review directory")
    return f"#{pr_number}-{normalize_head_branch(head_branch)}"


def validate_model_name(value: str) -> str:
    """モデル識別子をファイル名にする。``/`` と空白の並びは ``-`` にし、端の空白は拒否する。"""
    if value != value.strip():
        raise TargetError(EXIT_USAGE, "model identifier is not a safe filename")
    sanitized = re.sub(r"\s+", "-", value.replace("/", "-"))
    if _MODEL_FILENAME.fullmatch(sanitized) is None:
        raise TargetError(EXIT_USAGE, "model identifier is not a safe filename")
    return sanitized


def repository_reviews_root(parent_root: Path, repository: str) -> Path:
    """``<親>/reviews/<owner>/<repo>``。"""
    owner, name = validate_repository(repository).split("/")
    return parent_root / REVIEWS_DIR / owner / name


def review_directory(parent_root: Path, repository: str, pr_number: int, head_branch: str) -> Path:
    return repository_reviews_root(parent_root, repository) / review_directory_name(pr_number, head_branch)


# 親の main worktree とリポジトリの識別


def _load_agent_tmp() -> ModuleType:
    path = Path(__file__).resolve().parent / "agent_tmp.py"
    spec = importlib.util.spec_from_file_location("agent_tmp", path)
    if spec is None or spec.loader is None:
        raise TargetError(EXIT_PARENT, f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require_parent_root(path: Path) -> Path:
    """親の main worktree（linked worktree・submodule でない）で、``reviews/`` が git に無視されていること。"""
    root = path.resolve()
    toplevel = _git(root, "rev-parse", "--show-toplevel")
    common = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if toplevel.returncode or common.returncode or Path(toplevel.stdout.strip()).resolve() != root:
        raise TargetError(EXIT_PARENT, f"親の main worktree ではありません: {root}")
    if Path(common.stdout.strip()).resolve() != (root / ".git").resolve():
        raise TargetError(EXIT_PARENT, f"親の main worktree ではありません（linked worktree か submodule）: {root}")
    if _git(root, "check-ignore", "-q", f"{REVIEWS_DIR}/.review-target-probe").returncode != 0:
        raise TargetError(EXIT_PARENT, f"{root} では {REVIEWS_DIR}/ が git に無視されていません")
    return root


def default_parent_root() -> Path:
    """このスクリプトの置き場所から親の main worktree を決める（``agent_tmp.py root`` と同じ規則）。"""
    agent_tmp = _load_agent_tmp()
    try:
        root = cast(Path, agent_tmp.resolve_main_root(Path(__file__).resolve().parent))
    except agent_tmp.AgentTmpError as error:
        raise TargetError(EXIT_PARENT, f"親の main worktree を解決できません: {error}") from error
    return require_parent_root(root)


def _match_repository(url: str, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    for pattern in patterns:
        match = pattern.fullmatch(url.strip())
        if match is None:
            continue
        try:
            return validate_repository(f"{match['owner']}/{match['repo']}")
        except TargetError:
            return None
    return None


def github_repository(url: str) -> str | None:
    """GitHub の remote URL から ``owner/repo`` を読む。それ以外は None。

    https・http・ユーザ情報付き・大文字のホスト名・ssh を読む。SSH のホスト別名は行き先を決められないので読まない。
    """
    return _match_repository(url, _REMOTE_URLS)


def canonical_github_repository(url: str) -> str | None:
    """``https://github.com/...``・``git@github.com:...`` の形のときだけ ``owner/repo`` を返す。

    ``git clone`` に渡す URL の検査に使う。平文の http・認証情報付きの URL は受け付けない。
    """
    return _match_repository(url, _CANONICAL_URLS)


def origin_repository(checkout: Path) -> str | None:
    """origin の URL の設定値から読む。``git remote get-url`` は insteadOf を展開するので使わない。"""
    result = _git(checkout, "config", "--get", "remote.origin.url")
    return github_repository(result.stdout.strip()) if result.returncode == 0 else None


def parent_repository(parent_root: Path) -> str:
    repository = origin_repository(parent_root)
    if repository is None:
        raise TargetError(
            EXIT_PARENT,
            f"親リポジトリ {parent_root} の origin を GitHub のリポジトリとして解決できません。"
            "PR URL を指定してください",
        )
    return repository


def registered_submodules(parent_root: Path) -> list[tuple[Path, str]]:
    """親の ``.gitmodules`` に登録された submodule の（checkout のパス、URL）。"""
    if not (parent_root / ".gitmodules").is_file():
        return []
    result = _git(parent_root, "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.(path|url)$")
    if result.returncode not in (0, 1):
        raise TargetError(EXIT_PARENT, f"{parent_root / '.gitmodules'} を読めません: {result.stderr.strip()}")
    entries: dict[str, dict[str, str]] = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        section, _, variable = key.rpartition(".")
        entries.setdefault(section, {})[variable] = value
    return [
        (parent_root / entry["path"], entry["url"]) for entry in entries.values() if {"path", "url"} <= entry.keys()
    ]


def _toplevel(path: Path) -> Path | None:
    result = _git(path, "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()).resolve() if result.returncode == 0 else None


def target_checkout(parent_root: Path, repository: str) -> Path | None:
    """Git 操作の対象にする既存の checkout。親か、登録済み submodule の checkout。無ければ None（一時 clone）。

    submodule に登録されているのに checkout が使えないときは、一時 clone へ切り替えずに止まる。
    """
    if same_repository(origin_repository(parent_root), repository):
        return parent_root
    for checkout, url in registered_submodules(parent_root):
        if not same_repository(github_repository(url), repository):
            continue
        if not checkout.is_dir() or _toplevel(checkout) != checkout.resolve():
            raise TargetError(
                EXIT_PARENT,
                f"{repository} は親の submodule {checkout} に登録されていますが、checkout がありません。"
                f"git -C {parent_root} submodule update --init -- {checkout.relative_to(parent_root)} "
                "で用意してください",
            )
        if not same_repository(origin_repository(checkout), repository):
            raise TargetError(
                EXIT_PARENT,
                f"submodule {checkout} の origin が {repository} と一致しません。submodule の remote を直してください",
            )
        return checkout.resolve()
    return None


# PR の指定と GitHub への照会


class PullRequestArgument(NamedTuple):
    repository: str | None
    number: int


def parse_pull_request_argument(text: str) -> PullRequestArgument:
    value = text.strip()
    bare = _BARE.fullmatch(value)
    if bare is not None:
        return PullRequestArgument(None, int(bare["number"]))
    url = _PULL_URL.fullmatch(value)
    if url is not None:
        return PullRequestArgument(validate_repository(f"{url['owner']}/{url['repo']}"), int(url["number"]))
    raise TargetError(EXIT_USAGE, f"PR の指定を解釈できません（番号、#番号、PR の URL で指定してください）: {text!r}")


class PullRequest(NamedTuple):
    repository: str
    number: int
    url: str
    head_ref: str
    head_sha: str
    head_repository: str | None
    base_ref: str
    state: str
    clone_url: str


def _api(runner: Runner, path: str) -> tuple[int | None, str, str]:
    """``gh api -i`` を呼び、最後の応答のステータス・本文と、標準エラーを返す（リダイレクトのヘッダは読み飛ばす）。"""
    result = runner(["gh", "api", "-i", path])
    text = (result.stdout or "").replace("\r\n", "\n")
    status: int | None = None
    while (match := _STATUS_LINE.match(text)) is not None:
        status = int(match["status"])
        text = text.partition("\n\n")[2]
    return status, text, (result.stderr or "").strip()


def _object(value: object) -> dict[str, Any] | None:
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def _parse_pull_request(body: str, repository: str, number: int) -> PullRequest:
    def broken(what: str) -> TargetError:
        return TargetError(EXIT_API, f"GitHub の応答を読めません（{repository} の PR #{number}: {what}）")

    try:
        data = _object(json.loads(body))
    except json.JSONDecodeError as error:
        raise broken("JSON ではありません") from error
    base = _object(data.get("base")) if data else None
    head = _object(data.get("head")) if data else None
    base_repo = _object(base.get("repo")) if base else None
    if data is None or base is None or head is None or base_repo is None:
        raise broken("base・head・base.repo がありません")

    def text(mapping: Mapping[str, Any], key: str, where: str) -> str:
        value = mapping.get(key)
        if not isinstance(value, str) or not value:
            raise broken(where)
        return value

    pr_number = data.get("number")
    if type(pr_number) is not int or pr_number != number:
        raise broken("number")
    head_sha = text(head, "sha", "head.sha")
    if _SHA.fullmatch(head_sha) is None:
        raise broken("head.sha")
    head_repo = _object(head.get("repo"))
    head_repository = head_repo.get("full_name") if head_repo else None
    try:
        canonical = validate_repository(text(base_repo, "full_name", "base.repo.full_name"))
    except TargetError as error:
        raise broken("base.repo.full_name") from error
    return PullRequest(
        repository=canonical,
        number=number,
        url=text(data, "html_url", "html_url"),
        head_ref=text(head, "ref", "head.ref"),
        head_sha=head_sha,
        head_repository=head_repository if isinstance(head_repository, str) else None,
        base_ref=text(base, "ref", "base.ref"),
        state=text(data, "state", "state"),
        clone_url=text(base_repo, "clone_url", "base.repo.clone_url"),
    )


def fetch_pull_request(repository: str, number: int, *, runner: Runner = _run) -> PullRequest:
    """PR を照会する。不存在・リポジトリの不在・認証・通信の失敗を区別する。"""
    status, body, detail = _api(runner, f"repos/{repository}/pulls/{number}")
    if status == 200:
        return _parse_pull_request(body, repository, number)
    if status == 404:
        status, _, detail = _api(runner, f"repos/{repository}")
        if status == 200:
            raise TargetError(
                EXIT_PR_NOT_FOUND,
                f"{repository} に PR #{number} は存在しません。"
                "別リポジトリの PR を指定する場合は、PR URL を指定してください",
            )
        if status == 404:
            raise TargetError(
                EXIT_REPOSITORY_NOT_FOUND,
                f"{repository} が存在しないか、アクセス権がありません（PR #{number} を確認できません）",
            )
    if status in {401, 403}:
        raise TargetError(
            EXIT_AUTH,
            f"GitHub の認証・権限の問題で {repository} の PR #{number} を確認できません（HTTP {status}）。"
            "gh auth status を確認してください",
        )
    reason = f"HTTP {status}" if status is not None else (detail or "応答がありません")
    raise TargetError(
        EXIT_API, f"GitHub への照会に失敗したため、{repository} の PR #{number} を確認できません（{reason}）"
    )


def require_planning_owner(repository: str) -> None:
    owner = repository.partition("/")[0]
    if owner.lower() not in PLANNING_OWNERS:
        allowed = "、".join(sorted(PLANNING_OWNERS))
        raise TargetError(
            EXIT_OWNER,
            f"{repository} は対象外です。review-fix-planning と review-verify が扱うのは {allowed} 配下の PR だけです",
        )


def describe(parent_root: Path, pr: PullRequest, review_dir: Path, target_root: Path | None) -> dict[str, Any]:
    return {
        "parent_root": str(parent_root),
        **pr._asdict(),
        "review_dir": str(review_dir),
        "target_root": str(target_root) if target_root is not None else None,
    }


def resolve(argument: str, *, parent_root: Path, runner: Runner = _run) -> dict[str, Any]:
    """pr-review 用。PR を照会し、保存先（存在しなくてよい）と Git 操作の対象を返す。"""
    parsed = parse_pull_request_argument(argument)
    repository = parsed.repository or parent_repository(parent_root)
    pr = fetch_pull_request(repository, parsed.number, runner=runner)
    target_root = target_checkout(parent_root, pr.repository)
    return describe(parent_root, pr, review_directory(parent_root, pr.repository, pr.number, pr.head_ref), target_root)


# レビューディレクトリの探索


def numbered_directories(root: Path, number: int) -> list[Path]:
    """``root`` の直下で ``#<number>-`` で始まるディレクトリ（symlink を除く）。"""
    prefix = f"#{number}-"
    try:
        entries = sorted(root.iterdir())
    except FileNotFoundError, NotADirectoryError:
        return []
    return [entry for entry in entries if entry.name.startswith(prefix) and entry.is_dir() and not entry.is_symlink()]


def legacy_review_directories(parent_root: Path, repository: str, number: int) -> list[Path]:
    """旧配置（親の ``reviews/#<N>-*`` と、登録済み submodule の ``reviews/#<N>-*``）にある同じ PR のディレクトリ。"""
    roots: list[Path] = []
    if same_repository(origin_repository(parent_root), repository):
        roots.append(parent_root / REVIEWS_DIR)
    for checkout, url in registered_submodules(parent_root):
        if same_repository(github_repository(url), repository):
            roots.append(checkout / REVIEWS_DIR)
    return [directory for root in roots for directory in numbered_directories(root, number)]


class DirectoryIdentity(NamedTuple):
    repository: str
    number: int
    check_name: bool


def _identity_from_layout(parent_root: Path, directory: Path) -> DirectoryIdentity | None:
    match = _REVIEW_DIR_NAME.fullmatch(directory.name)
    if match is None:
        return None
    number = int(match["number"])
    reviews = (parent_root / REVIEWS_DIR).resolve()
    if directory.parent.parent.parent == reviews:
        try:
            repository = validate_repository(f"{directory.parent.parent.name}/{directory.parent.name}")
        except TargetError:
            return None
        return DirectoryIdentity(repository, number, check_name=True)
    if directory.parent == reviews:
        return DirectoryIdentity(parent_repository(parent_root), number, check_name=True)
    for checkout, url in registered_submodules(parent_root):
        if directory.parent == (checkout / REVIEWS_DIR).resolve():
            repository = github_repository(url)
            if repository is None:
                raise TargetError(
                    EXIT_PARENT, f"submodule {checkout} の URL を GitHub のリポジトリとして解決できません: {url}"
                )
            return DirectoryIdentity(repository, number, check_name=True)
    return None


def _identity_from_findings(directory: Path) -> tuple[str, int] | None:
    """``findings/*.json`` の ``pr``（全ファイルで同じこと）。候補一覧が無ければ None。"""
    findings = directory / "findings"
    if not findings.is_dir():
        return None
    seen: dict[tuple[str, int], str] = {}
    for path in sorted(findings.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            document = _object(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TargetError(EXIT_PATH, f"{path} を読めません: {error}") from error
        pr = _object(document.get("pr")) if document else None
        repository = pr.get("repository") if pr else None
        number = pr.get("number") if pr else None
        if not isinstance(repository, str) or type(number) is not int:
            raise TargetError(EXIT_PATH, f"{path} に pr.repository・pr.number がありません")
        seen[(repository.lower(), number)] = repository
    if not seen:
        return None
    if len(seen) > 1:
        raise TargetError(EXIT_PATH, f"{findings} の候補一覧が複数の PR を指しています")
    ((_, number), repository) = next(iter(seen.items()))
    return validate_repository(repository), number


def identify_review_directory(parent_root: Path, directory: Path) -> DirectoryIdentity:
    """パスの形（新しい構成・旧配置）か ``findings/`` の ``pr`` から、対象の PR を決める。"""
    from_findings = _identity_from_findings(directory)
    identity = _identity_from_layout(parent_root, directory)
    if identity is None:
        if from_findings is None:
            raise TargetError(
                EXIT_PATH,
                f"{directory} から対象の PR を決められません"
                "（reviews/<owner>/<repo>/#<N>-*・旧配置・findings/ のどれでもない）",
            )
        return DirectoryIdentity(from_findings[0], from_findings[1], check_name=False)
    if from_findings is not None and (
        not same_repository(from_findings[0], identity.repository) or from_findings[1] != identity.number
    ):
        raise TargetError(
            EXIT_PATH,
            f"{directory} の findings/ は {from_findings[0]} の PR #{from_findings[1]} を指していて、"
            f"ディレクトリの場所（{identity.repository} の PR #{identity.number}）と食い違います",
        )
    return identity


def _from_path(argument: str, *, parent_root: Path, for_planning: bool, runner: Runner) -> dict[str, Any]:
    path = Path(argument)
    if not path.is_dir():
        raise TargetError(
            EXIT_USAGE, f"PR の指定（番号、#番号、PR の URL）でも、存在するディレクトリでもありません: {argument!r}"
        )
    directory = path.resolve()
    identity = identify_review_directory(parent_root, directory)
    if for_planning:
        require_planning_owner(identity.repository)
    pr = fetch_pull_request(identity.repository, identity.number, runner=runner)
    if for_planning:
        require_planning_owner(pr.repository)
    if identity.check_name and directory.name != review_directory_name(pr.number, pr.head_ref):
        raise TargetError(
            EXIT_PATH,
            f"ディレクトリ名 {directory.name} が {pr.repository} の PR #{pr.number}"
            f"（head ブランチ {pr.head_ref}）と一致しません",
        )
    return describe(parent_root, pr, directory, target_checkout(parent_root, pr.repository))


def find_review_directory(
    argument: str, *, parent_root: Path, for_planning: bool, runner: Runner = _run
) -> dict[str, Any]:
    """review-fix-planning・review-verify 用。番号・URL・パスから保存済みのレビューディレクトリを決める。

    ``for_planning`` では、PR のリポジトリが分かった時点でオーナーを確かめ、対象外なら照会もせずに止まる。
    """
    try:
        parsed = parse_pull_request_argument(argument)
    except TargetError:
        return _from_path(argument, parent_root=parent_root, for_planning=for_planning, runner=runner)
    repository = parsed.repository or parent_repository(parent_root)
    if for_planning:
        require_planning_owner(repository)
    pr = fetch_pull_request(repository, parsed.number, runner=runner)
    if for_planning:
        require_planning_owner(pr.repository)
    root = repository_reviews_root(parent_root, pr.repository)
    candidates = numbered_directories(root, pr.number)
    if not candidates:
        legacy = legacy_review_directories(parent_root, pr.repository, pr.number)
        hint = (
            "。旧配置に同じ PR のディレクトリがあります（使う場合はパスで指定してください）: "
            + ", ".join(map(str, legacy))
            if legacy
            else ""
        )
        raise TargetError(
            EXIT_NO_REVIEW,
            f"{pr.repository} の PR #{pr.number} は存在しますが、{root} に保存済みのレビューがありません{hint}",
        )
    if len(candidates) > 1:
        raise TargetError(
            EXIT_AMBIGUOUS,
            "候補が複数あります。レビューディレクトリのパスを指定してください: " + ", ".join(map(str, candidates)),
        )
    return describe(parent_root, pr, candidates[0], target_checkout(parent_root, pr.repository))


# 一時 clone


class CloneTarget(NamedTuple):
    repository: str
    number: int
    head_ref: str
    head_sha: str
    head_repository: str | None
    clone_url: str

    @classmethod
    def from_json(cls, value: object) -> CloneTarget:
        """``resolve``・``review-dir`` の JSON から読む。``clone_url`` は対象リポジトリの GitHub の URL に限る。"""
        data = _object(value)
        if data is None:
            raise TargetError(EXIT_USAGE, "--target は resolve・review-dir の JSON オブジェクトにしてください")
        repository = data.get("repository")
        number = data.get("number")
        head_ref = data.get("head_ref")
        head_sha = data.get("head_sha")
        head_repository = data.get("head_repository")
        clone_url = data.get("clone_url")
        if (
            not isinstance(repository, str)
            or type(number) is not int
            or number < 1
            or not isinstance(head_ref, str)
            or not head_ref
            or not isinstance(head_sha, str)
            or _SHA.fullmatch(head_sha) is None
            or (head_repository is not None and not isinstance(head_repository, str))
            or not isinstance(clone_url, str)
        ):
            raise TargetError(EXIT_USAGE, "--target に repository・number・head_ref・head_sha・clone_url がありません")
        validate_repository(repository)
        if not same_repository(canonical_github_repository(clone_url), repository):
            raise TargetError(EXIT_USAGE, f"clone_url {clone_url} は {repository} の GitHub の URL ではありません")
        return cls(repository, number, head_ref, head_sha, head_repository, clone_url)


def fetch_pull_head(checkout: Path, number: int, head_sha: str) -> str:
    """``pull/<N>/head`` を ``refs/pr-review/<N>`` に固定し、``head_sha`` と一致することを確かめる。

    他のセッションの cleanup が ref を消した場合に限り、1 回だけ fetch し直す。
    """
    ref = f"refs/pr-review/{number}"
    current: subprocess.CompletedProcess[str] | None = None
    for _attempt in range(2):
        fetched = _git(
            checkout,
            "fetch",
            "--quiet",
            "--no-write-fetch-head",
            "--no-tags",
            "--refmap=",
            "origin",
            f"+pull/{number}/head:{ref}",
        )
        if fetched.returncode:
            raise TargetError(
                EXIT_CLONE, f"PR #{number} の head を fetch できません（{checkout}）: {fetched.stderr.strip()}"
            )
        current = _git(checkout, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if current.returncode == 0:
            break
    if current is None or current.returncode:
        raise TargetError(EXIT_CLONE, f"fetch し直しても {ref} がありません（{checkout}）")
    fetched_sha = current.stdout.strip()
    if fetched_sha != head_sha:
        raise TargetError(
            EXIT_CLONE,
            f"PR #{number} の head が解決時の {head_sha[:12]} から {fetched_sha[:12]} に変わりました。"
            "解決からやり直してください",
        )
    return ref


def clone_repository(target: CloneTarget, dest: Path, *, checkout_branch: bool) -> Path:
    """``dest`` に一時 clone を作る。

    既定（検証用）は ``--no-checkout`` で clone して PR の head を ``refs/pr-review/<N>`` に固定する。
    ``checkout_branch``（修正用）は head ブランチを checkout し、先端が PR の head であることを確かめる。
    fork の PR は止まる。
    clone には ``NO_AUTO_MAINTENANCE`` を書き、以後の fetch・commit で自動 maintenance を起動させない。
    失敗したら作りかけの ``dest`` を残し、パスを報告する（片付けは呼び出し側が行う）。
    """
    if dest.exists() or dest.is_symlink():
        raise TargetError(EXIT_USAGE, f"clone 先がすでにあります: {dest}")
    if not dest.parent.is_dir():
        raise TargetError(EXIT_USAGE, f"clone 先の親ディレクトリがありません: {dest.parent}")
    if checkout_branch and not same_repository(target.head_repository, target.repository):
        source = target.head_repository or "削除済みのリポジトリ"
        raise TargetError(
            EXIT_CLONE,
            f"{target.repository} の PR #{target.number} は fork（{source}）からの PR なので、"
            "head ブランチを修正できません",
        )
    options = [f"--branch={target.head_ref}"] if checkout_branch else ["--no-checkout"]
    config = ["--config", "=".join(NO_AUTO_MAINTENANCE)]
    cloned = _run(["git", "clone", "--quiet", *config, *options, "--", target.clone_url, str(dest)])
    if cloned.returncode:
        raise TargetError(EXIT_CLONE, f"{target.clone_url} を {dest} に clone できません: {cloned.stderr.strip()}")
    if not checkout_branch:
        fetch_pull_head(dest, target.number, target.head_sha)
        return dest
    head = _git(dest, "rev-parse", "HEAD").stdout.strip()
    if head != target.head_sha:
        raise TargetError(
            EXIT_CLONE,
            f"head ブランチ {target.head_ref} の先端 {head[:12]} が PR の head {target.head_sha[:12]} と"
            f"一致しません（{dest}）",
        )
    return dest


def scan_context(value: object, checkout: Path, *, runner: Runner = _run) -> dict[str, str]:
    """解決済み PR と送信先・履歴を再確認する。場所の許可は scan_before_push の route 検査が担当する。"""
    snapshot = CloneTarget.from_json(value)
    data = cast(dict[str, Any], value)
    base_ref = data.get("base_ref")
    if not isinstance(base_ref, str) or not base_ref:
        raise TargetError(EXIT_USAGE, "--target に base_ref がありません")
    require_planning_owner(snapshot.repository)
    checkout = checkout.resolve()
    if _toplevel(checkout) != checkout:
        raise TargetError(EXIT_CLONE, f"Git の root で検査してください: {checkout}")
    for branch in (base_ref, snapshot.head_ref):
        if _git(checkout, "check-ref-format", f"refs/heads/{branch}").returncode:
            raise TargetError(EXIT_USAGE, f"ブランチ名として使えません: {branch!r}")
    for key in ("remote.origin.url", "remote.origin.pushurl"):
        configured = _git(checkout, "config", "--get-all", key)
        urls = configured.stdout.splitlines()
        if key == "remote.origin.pushurl" and configured.returncode == 1:
            continue  # pushurl 未設定なら、push も origin.url を使う。
        if (
            configured.returncode != 0
            or len(urls) != 1
            or not same_repository(github_repository(urls[0]), snapshot.repository)
        ):
            raise TargetError(
                EXIT_CLONE,
                f"{key} が PR の repository と一致する単一の URL ではありません",
            )

    pr = fetch_pull_request(snapshot.repository, snapshot.number, runner=runner)
    if (
        not same_repository(pr.repository, snapshot.repository)
        or not same_repository(snapshot.head_repository, snapshot.repository)
        or not same_repository(pr.head_repository, pr.repository)
        or pr.state.lower() != "open"
        or pr.head_ref != snapshot.head_ref
        or pr.head_sha != snapshot.head_sha
        or pr.base_ref != base_ref
    ):
        raise TargetError(
            EXIT_CLONE,
            "PR が open・同一 repository の head/base ではありません。対象の解決からやり直してください",
        )
    if _git(checkout, "merge-base", "--is-ancestor", snapshot.head_sha, "HEAD").returncode:
        raise TargetError(EXIT_CLONE, "local HEAD が解決済み PR head の修正履歴ではありません")
    return {
        "repository": pr.repository,
        "base_ref": pr.base_ref,
        "head_ref": pr.head_ref,
    }


# CLI


def main(argv: Sequence[str] | None = None, *, runner: Runner = _run) -> int:
    parser = argparse.ArgumentParser(
        prog="review_target.py", description="Resolve review targets and review directories (Issue #483)."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    resolve_parser = commands.add_parser("resolve", help="resolve a PR for pr-review")
    resolve_parser.add_argument("pr", help="PR number (3, #3) or PR URL")
    review_dir_parser = commands.add_parser("review-dir", help="find the saved review directory")
    review_dir_parser.add_argument("target", help="PR number, PR URL, or review directory path")
    review_dir_parser.add_argument("--for-planning", action="store_true", help="accept only PRs of PLANNING_OWNERS")
    clone_parser = commands.add_parser("clone", help="clone the PR repository into a new directory")
    clone_parser.add_argument("--target", required=True, help="JSON printed by resolve or review-dir")
    clone_parser.add_argument("--dest", required=True, help="new directory for the clone")
    clone_parser.add_argument("--checkout-branch", action="store_true", help="check out the head branch to fix it")
    scan_parser = commands.add_parser("scan-context", help="check the PR and push destination before secret scanning")
    scan_parser.add_argument("--target", required=True, help="JSON printed by resolve or review-dir")
    scan_parser.add_argument("--checkout", required=True, help="Git root containing the fix commits")
    args = parser.parse_args(argv)
    try:
        if args.command == "resolve":
            result: dict[str, Any] = resolve(args.pr, parent_root=default_parent_root(), runner=runner)
        elif args.command == "review-dir":
            result = find_review_directory(
                args.target, parent_root=default_parent_root(), for_planning=args.for_planning, runner=runner
            )
        else:
            try:
                value: object = json.loads(args.target)
            except json.JSONDecodeError as error:
                raise TargetError(EXIT_USAGE, f"--target が JSON ではありません: {error.msg}") from error
            if args.command == "scan-context":
                result = scan_context(value, Path(args.checkout), runner=runner)
            else:
                clone = CloneTarget.from_json(value)
                root = clone_repository(clone, Path(args.dest).absolute(), checkout_branch=args.checkout_branch)
                result = {"root": str(root), "head_sha": clone.head_sha}
    except TargetError as error:
        print(f"review_target: {error}", file=sys.stderr)
        return error.code
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
