"""Workflow behavior against real disposable Git checkouts and fake Python CLIs."""

import contextlib
import json
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Generator, Iterable
from pathlib import Path

import pytest

from tools.agent_runner import environ, process, runner, workspace
from tools.agent_runner.adapters import AdapterContext, AdapterOutcome, Capabilities, LaunchSpec
from tools.agent_runner.config import ConfigError
from tools.agent_runner.models import (
    DIAGNOSIS_TEXT_CHARS,
    Agent,
    Assignment,
    Config,
    Diagnosis,
    Identity,
    Receipt,
    Role,
    Runtime,
    State,
    Workflow,
)
from tools.agent_runner.runner import RunnerError, run_workflow

pytest_plugins = ("tools.test_support.plugin",)
# Only fake agents are launched here; drop AGENT_RUNNER_DEPTH inside each test so a runner agent can run them
pytestmark = pytest.mark.usefixtures("outside_runner")

# A fake CLI: options pick its receipt, exit code, and side effects; it echoes inherited values to its logs.
_SCRIPT = """
import json, os, pathlib, subprocess, sys, time
incoming = json.loads(sys.stdin.read())
request = incoming['request']
options = incoming['options']
if options.get('outside'):
    pathlib.Path(options['outside']).write_text('changed again')
probes = {}
for name in options.get('probe_paths', []):
    try:
        pathlib.Path(name).write_text('changed again')
    except OSError:
        probes[name] = True
    else:
        probes[name] = False
lock_written = None
if options.get('review_lock'):
    try:
        pathlib.Path(options['review_lock']).write_text('held')
    except OSError:
        lock_written = False
    else:
        lock_written = True
review_ref_written = None
if options.get('review_ref'):
    review_ref = options['review_ref']
    added = subprocess.run(
        ['git', '-C', request['work_dir'], 'update-ref', review_ref, 'HEAD'], capture_output=True
    ).returncode
    removed = subprocess.run(
        ['git', '-C', request['work_dir'], 'update-ref', '-d', review_ref], capture_output=True
    ).returncode if added == 0 else None
    review_ref_written = added == removed == 0
other_commit = None
if options.get('other_commit'):
    other_commit = subprocess.run(
        ['git', '-C', options['other_commit'], 'commit', '--allow-empty', '-qm', 'outside']
    ).returncode
own_commit = None
if options.get('own_commit'):
    own = pathlib.Path(request['work_dir'])
    (own / 'own.txt').write_text('own change')
    added = subprocess.run(['git', '-C', str(own), 'add', 'own.txt']).returncode
    committed = subprocess.run(
        ['git', '-C', str(own), '-c', 'commit.gpgsign=false', 'commit', '-qm', 'own']
    ).returncode
    own_commit = added == committed == 0
temporary_worktree = None
if options.get('temporary_worktree'):
    target = options['temporary_worktree']
    added = subprocess.run(
        ['git', '-C', request['work_dir'], 'worktree', 'add', '--detach', target, 'HEAD'], capture_output=True
    ).returncode
    removed = subprocess.run(
        ['git', '-C', request['work_dir'], 'worktree', 'remove', target], capture_output=True
    ).returncode if added == 0 else None
    temporary_worktree = added == removed == 0
replaced = None
if options.get('replace_parent'):
    import shutil
    parent = pathlib.Path(options['replace_parent'])
    moved = parent.with_name(parent.name + '.moved')
    try:
        parent.rename(moved)
        shutil.copytree(moved, parent, symlinks=True)
        (parent / options['replace_file']).write_text('changed by agent')
    except OSError:
        replaced = False
    else:
        replaced = True
if options.get('marker'):
    pathlib.Path(options['marker']).write_text('ready')
    time.sleep(0.3)
token = os.environ.get('SERVICE_TOKEN', 'no-token')
scratch_root = os.environ.get('TMPDIR')
if options.get('scratch_probe') and scratch_root:
    scratch = pathlib.Path(scratch_root)
    (scratch / 'same.txt').write_text(request['agent_id'] + ':' + token)
    direct = pathlib.Path('/tmp') / options['scratch_probe']
    direct.write_text(request['agent_id'] + ':' + token)
    nested = scratch / 'nested'
    nested.mkdir()
    (nested / 'output.log').write_text(token)
    (nested / 'external-link').symlink_to(options['external_file'])
    (nested / 'external-directory').symlink_to(pathlib.Path(options['external_file']).parent)
    os.mkfifo(nested / 'pipe')
    if options.get('scratch_unreadable'):
        (scratch / 'same.txt').chmod(0)
_export_file = pathlib.Path(sys.argv[1]).parent.joinpath('export.json')
if options.get('export_link'):
    _export_file.symlink_to(options['export_link'])
elif options.get('devin_export') is not None:
    _export_file.write_text(options['devin_export'].replace('$TOKEN', token), encoding='utf-8')
print(os.environ.get('SERVICE_TOKEN', 'no-token'))
print(os.environ.get('Extra_Password', 'no-password'), file=sys.stderr)
payload = options.get('payload', {'prompt': request['prompt'], 'depth': os.environ.get('AGENT_RUNNER_DEPTH'),
    'ambient': os.environ.get('UNCONFIGURED_SECRET'), 'git': os.environ.get('GIT_DIR'),
    'probes': probes, 'other_commit': other_commit, 'own_commit': own_commit,
    'temporary_worktree': temporary_worktree, 'lock_written': lock_written,
    'review_ref_written': review_ref_written, 'replaced': replaced, 'scratch_root': scratch_root})
receipt = {key: request[key] for key in ('schema_version', 'run_id', 'request_id', 'agent_id', 'role')}
receipt.update(status='success', payload=payload)
if not options.get('missing'):
    pathlib.Path(sys.argv[1]).write_text(options.get('raw', json.dumps(receipt)))
sys.exit(options.get('exit', 0))
"""


class FakeAdapter:
    capabilities = Capabilities()

    def __init__(
        self,
        options: dict[str, dict[str, object]] | None = None,
        *,
        outcome_state: State | None = None,
        identity: Identity | None = None,
        diagnosis: Diagnosis | None = None,
    ) -> None:
        self.options = options or {}
        self.outcome_state: State | None = outcome_state
        self.identity = identity or Identity()
        self.diagnosis = diagnosis

    def prepare(self, context: AdapterContext) -> LaunchSpec:
        options = self.options.get(context.request.agent_id, {})
        scratch = context.receipt_path.parent / "tmp"
        if options.get("scratch_probe") and scratch.is_dir():
            # Cross-mount hardlinks cannot be created after bubblewrap starts.
            (scratch / "hardlink").hardlink_to(str(options["external_file"]))
        return LaunchSpec(
            argv=(*context.agent.command, str(context.receipt_path)),
            stdin=json.dumps(
                {
                    "request": context.request.model_dump(mode="json"),
                    "options": self.options.get(context.request.agent_id, {}),
                }
            ).encode(),
        )

    def collect(self, context: AdapterContext, stdout_path: Path, stderr_path: Path) -> AdapterOutcome:
        receipt = (
            Receipt.model_validate_json(context.receipt_path.read_bytes()) if context.receipt_path.exists() else None
        )
        return AdapterOutcome(receipt, self.identity, self.outcome_state, self.diagnosis)


@contextlib.contextmanager
def set_when(event: threading.Event, ready: Callable[[], bool]) -> Generator[None]:
    """Set event from a watcher thread once ready() holds inside the with block.

    Interruption tests key cancellation to an observed point of the run, not to
    a timer started before run_workflow: preparation before the slot and inside
    the role deadline both stretch on a loaded host (#694). The event is never
    set if ready() does not hold before the block ends.
    """
    finished = threading.Event()

    def watch() -> None:
        while not finished.wait(0.01):
            if ready():
                event.set()
                return

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        yield
    finally:
        finished.set()
        watcher.join()


def written(paths: Iterable[Path]) -> int:
    """Count files whose content has been written (a single small write)."""
    return sum(path.stat().st_size > 0 for path in paths)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "tracked.txt").write_text("original")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True, capture_output=True)
    return repo


def configuration(agents: list[str] | None = None, fallback: list[str] | None = None) -> Config:
    return Config(
        agents={
            name: Agent(runtime="pi", command=[sys.executable, "-c", _SCRIPT], model=name) for name in ("a", "b", "c")
        },
        roles={"inspect": Role.model_validate({"writes": False, "deadline_seconds": 5.0, "result_shape": "json"})},
        workflows={
            "audit": Workflow(assignments={"inspect": Assignment(agents=agents or ["a"], fallback=fallback or [])})
        },
    )


def test_unavailable_registered_checkout_is_an_outside_change(repository: Path, tmp_path: Path) -> None:
    """監査できない登録済みの checkout が outside_changes から落ちると、呼び出し側が黙って受け入れる。"""
    external = workspace.create_worktree(repository, "HEAD", tmp_path / "unavailable")
    gitfile = external / ".git"
    original = gitfile.read_bytes()
    gitfile.write_text(f"gitdir: {tmp_path / 'missing-gitdir'}\n")
    try:
        batch = run_workflow(
            configuration(),
            "audit",
            "prompt",
            repository,
            output_dir=tmp_path / "run",
            adapter_loader=lambda _: FakeAdapter(),
        )
        assert batch.outside_changes == [str(external)]
    finally:
        gitfile.write_bytes(original)


@pytest.mark.parametrize("options", [{"payload": {}}, {"exit": 7}], ids=["empty-payload", "nonzero-exit"])
def test_exit_zero_is_not_a_substitute_for_valid_receipt(
    repository: Path, tmp_path: Path, options: dict[str, object]
) -> None:
    """空の payload・終了 7 の CLI を success とする、観測していない identity を補うと、呼び出し側が黙って誤る。"""
    batch = run_workflow(
        configuration(),
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter({"a": options}),
    )
    assert batch.results[0].state != "success"
    assert batch.results[0].attempts[0].identity.model is None


def test_adapter_success_cannot_bypass_missing_receipt(repository: Path, tmp_path: Path) -> None:
    """receipt が無いのに adapter の success で成功にすると、payload の無い success を黙って返す。"""
    batch = run_workflow(
        configuration(),
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter({"a": {"missing": True}}, outcome_state="success"),
    )
    assert batch.results[0].state != "success"


def test_partial_success_and_independent_worktrees(repository: Path, tmp_path: Path) -> None:
    """1 人の失敗を batch の success にする・並行の担当に同じ worktree を渡すと、失敗や混ざった変更を黙って通す。"""
    batch = run_workflow(
        configuration(["a", "b"]),
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter({"a": {"missing": True}}),
    )
    assert batch.state != "success"
    assert len({result.attempts[0].work_dir for result in batch.results}) == 2


def test_only_explicit_fallback_is_tried_in_order(repository: Path, tmp_path: Path) -> None:
    """設定に無い担当へ切り替える・fallback の順序を取り違えると、利用者が選んでいない provider に黙って送る。"""
    fake = FakeAdapter({"a": {"missing": True}, "b": {"exit": 2}})
    batch = run_workflow(
        configuration(fallback=["b", "c"]),
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: fake,
    )
    assert batch.results[0].actual_agent == "c"
    assert [attempt.agent_id for attempt in batch.results[0].attempts] == ["a", "b", "c"]
    alone = run_workflow(
        configuration(), "audit", "prompt", repository, output_dir=tmp_path / "alone", adapter_loader=lambda _: fake
    )
    assert [attempt.agent_id for attempt in alone.results[0].attempts] == ["a"]


def test_concurrent_dirty_checkout_change_keeps_warning_without_rejecting_agent(
    repository: Path, tmp_path: Path
) -> None:
    """別セッションの dirty な checkout の更新だけで担当を拒否すると、変更を取り込めず作業が止まる。"""
    external = workspace.create_worktree(repository, "HEAD", tmp_path / "parallel checkout")
    dirty = external / "tracked.txt"
    dirty.write_text("already dirty")
    marker = tmp_path / "agent-ready"
    ready = threading.Event()

    def change() -> None:
        if ready.wait(5):
            dirty.write_text("another session changed it")

    writer = threading.Thread(target=change)
    writer.start()
    try:
        with set_when(ready, marker.exists):
            batch = run_workflow(
                configuration(),
                "audit",
                "prompt",
                repository,
                output_dir=tmp_path / "run",
                adapter_loader=lambda _: FakeAdapter({"a": {"marker": str(marker)}}),
            )
    finally:
        writer.join()
    assert dirty.read_text() == "another session changed it"
    assert batch.state == "success" and batch.checkout_isolated
    assert batch.outside_changes == [str(external)]


def test_agent_cannot_write_registered_checkouts_but_can_commit_own_worktree(repository: Path, tmp_path: Path) -> None:
    """担当の既存 checkout への書き込みを許すと、監査差を並行変更扱いにした後に無断変更を取り込む。"""
    external = workspace.create_worktree(repository, "HEAD", tmp_path / "別セッション checkout\n2")
    external_gitdir = Path(
        subprocess.run(
            ["git", "-C", str(external), "rev-parse", "--path-format=absolute", "--git-dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    protected = [
        repository / "tracked.txt",
        repository / ".git/index",
        external / "tracked.txt",
        external_gitdir / "index",
    ]
    before = [path.read_bytes() for path in protected]
    head = subprocess.run(
        ["git", "-C", str(external), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout
    batch = run_workflow(
        configuration(),
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter(
            {
                "a": {
                    "probe_paths": [str(path) for path in protected],
                    "other_commit": str(external),
                    "own_commit": True,
                    "temporary_worktree": str(tmp_path / "temporary worktree"),
                }
            }
        ),
    )
    assert batch.state == "success" and batch.checkout_isolated
    payload = batch.results[0].attempts[0].payload
    assert isinstance(payload, dict)
    assert payload["probes"] == {str(path): True for path in protected} and payload["other_commit"] != 0
    assert payload["own_commit"] and payload["temporary_worktree"]
    assert [path.read_bytes() for path in protected] == before
    assert (
        subprocess.run(
            ["git", "-C", str(external), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
        ).stdout
        == head
    )


def test_allowed_storage_keeps_registered_checkout_inside_it_read_only(repository: Path, tmp_path: Path) -> None:
    """許可した保存先の内側の既存 worktree を担当が書けると、他者の無断変更を並行変更の警告だけで取り込む。"""
    (repository / ".git/info").mkdir(exist_ok=True)
    (repository / ".git/info/exclude").write_text("/tmp/\n")
    storage = repository / "tmp/agents"
    storage.mkdir(parents=True)
    nested = workspace.create_worktree(repository, "HEAD", storage / "別セッション checkout\n2")
    protected, scratch = nested / "tracked.txt", storage / "scratch.txt"
    batch = run_workflow(
        configuration(),
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter({"a": {"probe_paths": [str(protected), str(scratch)]}}),
        writable_paths=(storage,),
    )
    assert batch.state == "success" and batch.checkout_isolated
    payload = batch.results[0].attempts[0].payload
    assert isinstance(payload, dict)
    assert payload["probes"] == {str(protected): True, str(scratch): False}
    assert protected.read_text() == "original"


def test_agent_cannot_replace_registered_checkout_by_moving_its_parent(repository: Path, tmp_path: Path) -> None:
    """書ける祖先を移して同じパスに複製を置けると、他者の checkout を動かした無断変更を並行変更として取り込む。"""
    parent = tmp_path / "other session"
    parent.mkdir()
    external = workspace.create_worktree(repository, "HEAD", parent / "checkout")
    inode = external.stat().st_ino
    batch = run_workflow(
        configuration(),
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter(
            {"a": {"replace_parent": str(parent), "replace_file": "checkout/tracked.txt"}}
        ),
    )
    assert batch.state == "success"
    payload = batch.results[0].attempts[0].payload
    assert isinstance(payload, dict)
    assert payload["replaced"] is False
    assert external.stat().st_ino == inode and (external / "tracked.txt").read_text() == "original"


def test_review_save_lock_is_writable_without_exposing_existing_git_state(repository: Path, tmp_path: Path) -> None:
    """レビュー用のロック・ref を開けても、既存 checkout の HEAD・index・refs を変えさせない。"""
    common = repository / ".git"
    branch = subprocess.run(
        ["git", "-C", str(repository), "symbolic-ref", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    protected = [common / "HEAD", common / "index", common / "refs/heads" / branch]
    before = [path.read_bytes() for path in protected]
    lock = common / "pr-review-save.lock"
    review_ref = "refs/pr-review/151"
    batch = run_workflow(
        configuration(),
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter(
            {
                "a": {
                    "probe_paths": [str(path) for path in protected],
                    "review_lock": str(lock),
                    "review_ref": review_ref,
                }
            }
        ),
        writable_paths=(lock,),
        private_review_refs=True,
    )
    assert batch.state == "success" and batch.checkout_isolated
    payload = batch.results[0].attempts[0].payload
    assert isinstance(payload, dict)
    assert payload["lock_written"] is True
    assert payload["review_ref_written"] is True
    assert not (common / review_ref).exists()
    assert not (common / "packed-refs.lock").exists()
    assert [path.read_bytes() for path in protected] == before


def test_only_allowlisted_environment_and_redacted_results(
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """許可しない資格情報・GIT_* が担当に渡る、ログ・payload・identity・request.json に秘密が黙って残る。"""
    ambient = {
        "SERVICE_TOKEN": "token-value",
        "Extra_Password": "password-value",
        "UNCONFIGURED_SECRET": "ambient-value",
        "GIT_DIR": "/bogus-ambient-git",
    }
    for name, value in ambient.items():
        monkeypatch.setenv(name, value)
    # The same ambient values are the exec environment commands inherit from.
    monkeypatch.setattr(environ, "_literal", environ.literal_environ() | ambient)
    cfg = configuration()
    cfg.agents["a"].env = ["SERVICE_TOKEN", "Extra_Password", "GIT_DIR"]
    fake = FakeAdapter(
        {"a": {"payload": {"nested": ["token-value", {"x": "password-value"}]}}},
        identity=Identity(model="token-value", skill_versions={"version": "password-value"}),
    )
    batch = run_workflow(
        cfg, "audit", "prompt", repository, output_dir=tmp_path / "run", adapter_loader=lambda _: fake
    )
    serialized = batch.model_dump_json()
    assert "token-value" not in serialized and "password-value" not in serialized
    attempt = batch.results[0].attempts[0]
    assert attempt.stdout_path is not None and "token-value" not in attempt.stdout_path.read_text()
    assert attempt.stderr_path is not None and "password-value" not in attempt.stderr_path.read_text()
    assert attempt.request_path is not None and "password-value" not in attempt.request_path.read_text()
    environment = run_workflow(
        cfg,
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "environment",
        adapter_loader=lambda _: FakeAdapter(),
    )
    payload = environment.results[0].attempts[0].payload
    assert isinstance(payload, dict)
    assert payload["depth"] == "1" and payload["ambient"] is None and payload["git"] is None


def test_scratch_is_isolated_and_redacted_after_every_attempt(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """固定 /tmp 名の衝突と秘密の残存を防ぎ、整理がリンク先の他者のファイルを変更しない。"""
    secret = "scratch-dummy-token"
    external = tmp_path / "external.txt"
    external.write_text(secret)
    host_file = Path("/tmp") / f"runner-scratch-{uuid.uuid4().hex}"
    cfg = configuration(["a", "b"], ["c"])
    for agent in cfg.agents.values():
        agent.env = ["SERVICE_TOKEN", "TMPDIR"]
    monkeypatch.setattr(
        environ,
        "_literal",
        environ.literal_environ()
        | {
            "SERVICE_TOKEN": secret,
            "TMPDIR": str(tmp_path),
        },
    )
    fake = FakeAdapter(
        {
            name: {
                "scratch_probe": host_file.name,
                "external_file": str(external),
                "exit": int(name == "a"),
            }
            for name in cfg.agents
        }
    )
    try:
        batch = run_workflow(
            cfg,
            "audit",
            "日本語の依頼\n二行目",
            repository,
            output_dir=tmp_path / "run with spaces",
            adapter_loader=lambda _: fake,
        )
        assert batch.state == "success"
        assert not host_file.exists()
        attempts = [item for result in batch.results for item in result.attempts]
        roots: list[Path] = []
        for item in attempts:
            assert item.stop_confirmed and not item.sensitive_paths
            assert isinstance(item.payload, dict)
            value = item.payload["scratch_root"]
            assert isinstance(value, str)
            root = Path(value)
            assert root != tmp_path and item.request_path is not None
            assert root.is_relative_to(item.request_path.parent)
            roots.append(root)
            assert (root / "same.txt").read_text().startswith(item.agent_id + ":")
            assert (root / host_file.name).read_text().startswith(item.agent_id + ":")
            assert secret not in (root / "same.txt").read_text()
            assert secret not in (root / "nested/output.log").read_text()
            assert secret not in (root / "hardlink").read_text()
            assert not (root / "nested/external-link").is_symlink()
            assert not (root / "nested/external-directory").is_symlink()
            assert not (root / "nested/pipe").exists()
        assert len(roots) == len(set(roots)) and len(roots) > len(batch.results)
        assert external.read_text() == secret
    finally:
        host_file.unlink(missing_ok=True)


def test_unconfirmed_stop_keeps_scratch_unread_and_reports_it(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """停止未確認の一時ファイルを伏せ字・削除したり報告から落とすと、実行中の担当と秘密を見失う。"""
    original = runner.execute

    async def unconfirmed(*args: object, **kwargs: object) -> process.ProcessResult:
        result = await original(*args, **kwargs)  # type: ignore[arg-type]
        return process.ProcessResult(result.state, result.exit_code, False, result.elapsed_seconds)

    external = tmp_path / "external.txt"
    external.write_text("fixture")
    host_file = Path("/tmp") / f"runner-scratch-{uuid.uuid4().hex}"
    monkeypatch.setattr(runner, "execute", unconfirmed)
    try:
        batch = run_workflow(
            configuration(),
            "audit",
            "prompt",
            repository,
            output_dir=tmp_path / "run",
            adapter_loader=lambda _: FakeAdapter(
                {
                    "a": {
                        "scratch_probe": host_file.name,
                        "external_file": str(external),
                    }
                }
            ),
        )
        item = batch.results[0].attempts[0]
        assert item.request_path is not None
        root = item.request_path.parent / "tmp"
        assert root in item.sensitive_paths and root.is_dir()
        assert (root / "nested/external-link").is_symlink() and (root / "nested/pipe").exists()
        assert item.state != "success" and item.payload is None
    finally:
        host_file.unlink(missing_ok=True)


def test_unreadable_scratch_is_reported_instead_of_success(repository: Path, tmp_path: Path) -> None:
    """伏せ字に失敗した一時ファイルを成功として返すと、秘密のある保持領域が通常回収される。"""
    external = tmp_path / "external.txt"
    external.write_text("fixture")
    host_file = Path("/tmp") / f"runner-scratch-{uuid.uuid4().hex}"
    try:
        batch = run_workflow(
            configuration(),
            "audit",
            "prompt",
            repository,
            output_dir=tmp_path / "run",
            adapter_loader=lambda _: FakeAdapter(
                {
                    "a": {
                        "scratch_probe": host_file.name,
                        "external_file": str(external),
                        "scratch_unreadable": True,
                    }
                }
            ),
        )
        item = batch.results[0].attempts[0]
        assert item.request_path is not None
        path = item.request_path.parent / "tmp/same.txt"
        assert path in item.sensitive_paths and item.state != "success"
        assert not host_file.exists()
    finally:
        host_file.unlink(missing_ok=True)


def test_replaced_scratch_root_does_not_redact_external_files(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """tmp 自体がリンクに置き換わったとき、伏せ字処理が他者のファイルを黙って変更しない。"""
    target = tmp_path / "external-directory"
    target.mkdir()
    secret = "external-fixture-token"
    marker = target / "original.txt"
    marker.write_text(secret)
    monkeypatch.setattr(environ, "_literal", environ.literal_environ() | {"SERVICE_TOKEN": secret})
    original = runner.execute

    async def replaced(*args: object, **kwargs: object) -> process.ProcessResult:
        result = await original(*args, **kwargs)  # type: ignore[arg-type]
        output = kwargs["stdout_path"]
        assert isinstance(output, Path)
        root = output.parent / "tmp"
        root.rename(output.parent / "tmp-original")
        root.symlink_to(target)
        return result

    monkeypatch.setattr(runner, "execute", replaced)
    batch = run_workflow(
        configuration(),
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter(),
    )
    item = batch.results[0].attempts[0]
    assert item.request_path is not None
    root = item.request_path.parent / "tmp"
    assert marker.read_text() == secret
    assert root in item.sensitive_paths and root.is_symlink() and item.state != "success"


def test_diagnosis_is_redacted_before_it_is_cut(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """先に切ってから伏せると、境界で切れた秘密の先頭が diagnosis に黙って残る（#96）。"""
    monkeypatch.setenv("SERVICE_TOKEN", "token-value")
    # The cut falls inside the secret: its first five characters fit below the limit.
    message = "x" * (DIAGNOSIS_TEXT_CHARS - 5) + "token-value の後\n2 行目"
    fake = FakeAdapter(
        {"a": {"exit": 1}},
        diagnosis=Diagnosis(
            ending="turn_end", reason="error", code="token-value", status=503, message=message, steps=2
        ),
    )
    batch = run_workflow(
        configuration(), "audit", "prompt", repository, output_dir=tmp_path / "run", adapter_loader=lambda _: fake
    )
    diagnosis = batch.results[0].attempts[0].diagnosis
    assert diagnosis is not None and diagnosis.message is not None
    assert "token" not in diagnosis.message
    assert "token-value" not in diagnosis.model_dump_json()


def test_nested_invocation_fails_closed(repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """担当の中（AGENT_RUNNER_DEPTH=1）から Python API を呼ぶと、入れ子の担当を黙って起動する。"""
    monkeypatch.setenv("AGENT_RUNNER_DEPTH", "1")
    with pytest.raises(ConfigError):
        run_workflow(
            configuration(),
            "audit",
            "prompt",
            repository,
            output_dir=tmp_path / "run",
            adapter_loader=lambda _: FakeAdapter(),
        )


def test_invalid_write_exceptions_are_rejected_before_any_output(repository: Path, tmp_path: Path) -> None:
    """ブランチ名に入りうる U+2028 を通すと、作業範囲の指示に行が足され、許可の範囲を黙って書き換える。"""
    output = tmp_path / "output"
    with pytest.raises(RunnerError):
        run_workflow(
            configuration(),
            "audit",
            "prompt",
            repository,
            output_dir=output,
            write_exceptions=("ok", "a\u2028b"),
        )
    assert not output.exists()


def test_a_bare_string_is_not_split_into_exceptions(repository: Path, tmp_path: Path) -> None:
    """文字列を 1 つ渡すと 1 文字ずつ（- / を含む）が許可として並び、書き込みを黙って広く許す。"""
    output = tmp_path / "output"
    with pytest.raises(RunnerError):
        run_workflow(configuration(), "audit", "prompt", repository, output_dir=output, write_exceptions="abc")
    assert not output.exists()


class ReadOnlyAdapter(FakeAdapter):
    """codex の -s read-only のように、writes=false の役割をランタイム側で読み取り専用にする担当（#52）。"""

    capabilities = Capabilities(enforces_read_only=True)


def read_only_codex(runtime: str) -> FakeAdapter:
    return ReadOnlyAdapter() if runtime == "codex" else FakeAdapter()


@pytest.mark.parametrize("primary", [True, False])
def test_read_only_agents_are_rejected_before_any_output_when_writes_are_needed(
    repository: Path, tmp_path: Path, primary: bool
) -> None:
    """読み取り専用の一次担当か fallback を起動すると、却下され続けてレビュアーが黙って減る。"""
    config = configuration(["b"] if primary else ["a"], [] if primary else ["b"])
    runtimes: dict[str, Runtime] = {"a": "pi", "b": "codex", "c": "cursor"}
    for name, agent in config.agents.items():
        agent.runtime = runtimes[name]
    output = tmp_path / "output"
    with pytest.raises(RunnerError):
        run_workflow(
            config,
            "audit",
            "prompt",
            repository,
            output_dir=output,
            adapter_loader=read_only_codex,
            write_exceptions=("/x/reviews: run records",),
        )
    assert not output.exists()


def test_existing_or_symlink_output_is_never_reused(repository: Path, tmp_path: Path) -> None:
    """既存の出力ディレクトリを再利用すると前の run の成果物を黙って上書きし、リンクなら別の場所へ書く。"""
    existing = tmp_path / "existing"
    existing.mkdir()
    marker = existing / "receipt.json"
    marker.write_text("retained")
    for path in (existing, tmp_path / "broken"):
        if path != existing:
            path.symlink_to(tmp_path / "missing")
        with pytest.raises(RunnerError):
            run_workflow(configuration(), "audit", "prompt", repository, output_dir=path)
    assert marker.read_text() == "retained"


def test_preparation_logs_are_redacted(repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """準備（git worktree add）の hook が出した秘密を、準備のログ（worktree.*.log）に伏せずに黙って残す。"""
    hook = repository / ".git/hooks/post-checkout"
    hook.write_text(
        f"#!{sys.executable}\n"
        "import os,sys\n"
        "print('hook-ran', os.environ['SERVICE_TOKEN'], flush=True)\n"
        "print(os.environ['UNCONFIGURED_SECRET'], file=sys.stderr, flush=True)\n"
    )
    hook.chmod(0o755)
    values = {"SERVICE_TOKEN": "prep-token-value", "UNCONFIGURED_SECRET": "ambient-prep-value"}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    # Preparation helpers run under the exec environment, not os.environ.
    monkeypatch.setattr(environ, "_literal", environ.literal_environ() | values)
    batch = run_workflow(
        configuration(),
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter(),
    )
    work_dir = batch.results[0].attempts[0].work_dir
    assert work_dir is not None
    logs = "".join((work_dir.parent / name).read_text() for name in ("worktree.stdout.log", "worktree.stderr.log"))
    assert "hook-ran" in logs
    assert not any(value in logs for value in values.values())


# Devin leaves an ATIF export next to the receipt. Its transcript can repeat
# inherited secret values in the final answer, history, and tool arguments,
# so the runner must redact the managed export like the receipt.
def _devin_configurable_export() -> str:
    return json.dumps(
        {
            "schema_version": "ATIF-v1.7",
            "session_id": "session-1",
            "agent": {"name": "devin", "version": "3000.11.3", "model_name": "SWE-2 Max"},
            "steps": [
                {"step_id": 1, "source": "user", "message": "run with $TOKEN"},
                {
                    "step_id": 2,
                    "source": "agent",
                    "message": "answer using $TOKEN",
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": "run",
                                "arguments": json.dumps({"command": "curl -H 'Authorization: Bearer $TOKEN'"}),
                            },
                        }
                    ],
                },
            ],
        }
    )


def _as_devin(config: Config) -> Config:
    for agent in config.agents.values():
        agent.runtime = "devin"
    return config


def _literal_env(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SERVICE_TOKEN", value)
    monkeypatch.setattr(environ, "_literal", environ.literal_environ() | {"SERVICE_TOKEN": value})


def test_devin_retained_export_is_redacted_after_nonzero_exit(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失敗した試行の Devin の export.json（継承した秘密を含みうる）を伏せずに黙って残す。"""
    _literal_env(monkeypatch, "synthetic-token-2345")
    config = _as_devin(configuration())
    config.agents["a"].env = ["SERVICE_TOKEN"]
    batch = run_workflow(
        config,
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter({"a": {"exit": 4, "devin_export": _devin_configurable_export()}}),
    )
    attempt = batch.results[0].attempts[0]
    assert attempt.request_path is not None
    text = (attempt.request_path.parent / "export.json").read_text(encoding="utf-8")
    # The fake CLI wrote the inherited value, so the export held it before redaction.
    assert "no-token" not in text
    assert "synthetic-token-2345" not in text


def test_unsafe_devin_export_is_retained_reported_and_never_collected(
    repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """export.json が外を指すリンクのとき、伏せ字でリンク先（利用者のファイル）を書き換える・収集して成功にする。"""
    _literal_env(monkeypatch, "synthetic-token-6789")
    sentinel = tmp_path / "sentinel.txt"
    sentinel.write_text("synthetic-token-6789", encoding="utf-8")
    config = _as_devin(configuration())
    config.agents["a"].env = ["SERVICE_TOKEN"]
    batch = run_workflow(
        config,
        "audit",
        "prompt",
        repository,
        output_dir=tmp_path / "run",
        adapter_loader=lambda _: FakeAdapter(
            {"a": {"devin_export": _devin_configurable_export(), "export_link": str(sentinel)}}
        ),
    )
    attempt = batch.results[0].attempts[0]
    assert attempt.state != "success" and attempt.payload is None
    assert attempt.request_path is not None
    assert attempt.sensitive_paths == [attempt.request_path.parent / "export.json"]
    assert sentinel.read_text(encoding="utf-8") == "synthetic-token-6789"
