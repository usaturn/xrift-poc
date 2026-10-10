.. _test_support:

==========================================
test_support: 実環境の形をした共有 fixture
==========================================

.. _test_support_overview:

概要
====

``tools/test_support`` は、実親リポジトリの形をした入力を共有するテスト補助パッケージである。
レビュー修正ループ（Issue #486）で「実装に都合のよい fixture」が見落とした条件── ``public`` gitlink が submodule HEAD とずれた ``+`` 状態、 ``.venv`` の無い新規 worktree、そこで失敗する ``uv run``、改行・タブ・CRLF・非 ASCII を含む複数行の自由記述──を、ローカルの bare remote とオフライン指定だけで再現する。
ネットワーク・実 PR・実 herdr は使わない。

.. _test_support_provides:

提供物
======

``tools.test_support.repos``
    ``create_parent_repo`` は、 ``public`` submodule（ ``git ls-files -s public`` が mode ``160000`` の gitlink）を持つ親 repository をローカルの bare remote 経由の clone として作る。
    親 checkout は実 checkout と同じく ``origin/main`` の detached HEAD で、ローカルの ``main`` ブランチを持たない（ ``refs/remotes/origin/HEAD`` は ``origin/main`` を指す）。
    ブランチが必要なテストは ``git -C <root> switch -c <branch> origin/main`` で自分で作る。
    submodule の状態は ``SubmoduleState.DIVERGED`` （ ``git submodule status`` が ``+`` ）、 ``PINNED`` （空白）、 ``UNINITIALIZED`` （ ``-`` ）から選ぶ。
    ``create_owned_worktree`` は ``git worktree add --detach`` で ``.venv`` を持たない worktree を作る。

``tools.test_support.texts``
    ``FINDING_CONTENT`` と ``FINDING_REASON`` は、改行・タブ・CRLF・非 ASCII 文字を含み、識別子として解釈できない（ ``str.isidentifier()`` が ``False`` ）自由記述のサンプル。

``tools.test_support.uv``
    ``offline_uv_env`` は ``UV_OFFLINE=1`` ・隔離した ``UV_CACHE_DIR`` ・ ``UV_PYTHON=<テスト実行中の interpreter>`` を立てた環境変数を返す。
    テストを動かしている venv の ``bin`` は ``PATH`` から除く。残っていると、 ``uv run`` が spawn するコマンドが漏れた venv から解決され、 venv 無し worktree での失敗を再現できない。
    ``run_uv_offline`` はその環境で ``uv`` を起動し、 ``CompletedProcess`` を返す。

``tools.test_support.plugin``
    pytest fixture ``parent_repo_factory`` ・ ``parent_repo`` （DIVERGED が既定）・ ``owned_worktree`` 。

.. _test_support_usage:

利用方法
========

fixture 経由
------------

test module の先頭でプラグインを宣言する ::

    pytest_plugins = ("tools.test_support.plugin",)


    def test_something(parent_repo: ParentRepo, owned_worktree: Path) -> None:
        ...  # parent_repo は DIVERGED（実 checkout と同じ '+' 状態）

状態の上書きや複数個の構築には ``parent_repo_factory`` を使う ::

    def test_uninitialized(parent_repo_factory: Callable[..., ParentRepo]) -> None:
        parent = parent_repo_factory(submodule_state=SubmoduleState.UNINITIALIZED)

直接呼び出し
------------

::

    parent = create_parent_repo(tmp_path / "repo", submodule_state=SubmoduleState.DIVERGED)
    worktree = create_owned_worktree(parent.root, tmp_path / "owned")
    result = run_uv_offline(
        ["run", "--project", "tools/devenv", "--no-sync", "pytest"],
        cwd=worktree,
        cache_dir=tmp_path / "uv-cache",
    )

``create_parent_repo`` は ``tools/devenv/pyproject.toml`` と ``tools/devenv/uv.lock`` を実 checkout からコピーしてコミットするため、このパッケージは親リポジトリ（ ``usaturn/maatlog-poc`` ）の checkout 内でのみ動く。
