.. _test_support:

==========================================
test_support: 実環境の形をした共有 fixture
==========================================

.. _test_support_overview:

概要
====

``tools/test_support`` は、 xrift-poc の実環境の形をした入力を共有するテスト補助パッケージである。
``items/`` ・ ``worlds/`` を無視する環境リポジトリの main checkout、 Git の有無と origin を選べる制作物、 ``.venv`` の無い新規 worktree、そこで失敗する ``uv run`` 、改行・タブ・CRLF・非 ASCII を含む複数行の自由記述を、ローカルの Git とオフライン指定だけで再現する。
ネットワーク・実 PR は使わない。

.. _test_support_provides:

提供物
======

``tools.test_support.repos``
    ``create_env_repo`` は、 ``git init`` した環境リポジトリの main checkout を ``<base_dir>/xrift-poc`` に作る。
    origin は GitHub の URL（ ``ENV_ORIGIN`` ）で、 HEAD は ``refs/remotes/origin/main`` を指す detached HEAD、ローカルの ``main`` ブランチは持たない。
    ``items/`` ・ ``worlds/`` は ``.gitignore`` でルートから無視し、ディレクトリを作っておく。
    ブランチが必要なテストは ``git -C <root> switch -c <branch> origin/main`` で自分で作る。
    ``create_product`` は ``items/<name>`` ・ ``worlds/<name>`` に制作物を作る。 ``git=False`` なら Git を初期化せず、 ``origin_url`` で origin を選べる。
    ``create_owned_worktree`` は ``git worktree add --detach`` で ``.venv`` を持たない worktree を作る。

``tools.test_support.texts``
    ``FINDING_CONTENT`` と ``FINDING_REASON`` は、改行・タブ・CRLF・非 ASCII 文字を含み、識別子として解釈できない（ ``str.isidentifier()`` が ``False`` ）自由記述のサンプル。

``tools.test_support.worktree_race``
    ``plant_racer`` は、並行した ``git worktree add`` の書きかけの登録（ ``commondir`` が空）を ``<repository>/.git/worktrees/<name>`` に置き、その ``commondir`` のパスを返す。
    書き終えるまで、同じリポジトリの ``git worktree add`` は登録の一覧を読むところで、何も作らずに終了コード 128 で止まる（#68）。
    ``commondir`` に ``../..`` を書くと、相手が書き終えた状態になる。
    本物の ``git worktree add`` と同じく ``locked`` を置き、別の場所で作ってから rename で一括で置くので、 ``git commit`` が切り離して起動する自動 maintenance の prune に消されない（#88）。

``tools.test_support.uv``
    ``offline_uv_env`` は ``UV_OFFLINE=1`` ・隔離した ``UV_CACHE_DIR`` ・ ``UV_PYTHON=<テスト実行中の interpreter>`` を立てた環境変数を返す。
    テストを動かしている venv の ``bin`` と、親ディレクトリに ``pyvenv.cfg`` がある別の venv の ``bin`` は ``PATH`` から除く。残っていると、 ``uv run`` が spawn するコマンドが漏れた venv から解決され、 venv 無し worktree での失敗を再現できない。
    ``UV_PROJECT_ENVIRONMENT`` も外す。 外側の実行環境が設定する別の venv が残っていると、 uv が worktree の ``.venv`` の代わりにそれを使い、同じく再現できない。
    ``run_uv_offline`` はその環境で ``uv`` を起動し、 ``CompletedProcess`` を返す。

``tools.test_support.plugin``
    pytest fixture ``env_repo_factory`` ・ ``env_repo`` ・ ``owned_worktree`` ・ ``outside_runner`` ・ ``outside_codex`` 。制作物は ``create_product`` を直接呼んで作る。
    ``outside_runner`` は、テストの中だけ ``AGENT_RUNNER_DEPTH`` を外す。
    runner の担当（ ``AGENT_RUNNER_DEPTH=1`` ）が関連テストを実行しても、偽の adapter・偽の CLI だけを起動するテストが ``nested_invocation`` で落ちないようにする。
    本物の担当を起動しうるテストには使わない。
    ``outside_codex`` は、テストの中だけ ``CODEX_THREAD_ID`` を外す。
    Codex の中（ツールの実行に ``CODEX_THREAD_ID`` が入る）でも、モデルを固定したテストが pr-review の helper の実行中のモデルとの照合で落ちないようにする。
    Codex の中でモデルを読む挙動を確かめるテストは、テストの中で一時の ``CODEX_HOME`` と rollout を作って設定し直す。
    どの fixture も autouse ではない。test module で読み込んだプラグインの fixture はテスト一式から見えるため、autouse にすると読み込んだ後に収集したモジュールにも効く。

.. _test_support_usage:

利用方法
========

fixture 経由
------------

test module の先頭でプラグインを宣言する ::

    pytest_plugins = ("tools.test_support.plugin",)


    def test_something(env_repo: EnvRepo, owned_worktree: Path) -> None:
        product = create_product(env_repo.root, "worlds", "空白 ワールド", git=False)

複数個の構築には ``env_repo_factory`` を使う ::

    def test_two(env_repo_factory: Callable[[], EnvRepo]) -> None:
        first, second = env_repo_factory(), env_repo_factory()

偽の担当だけを起動する test module は、モジュール単位で ``outside_runner`` を使う。
モジュールに後から足したテストにも効く ::

    pytest_plugins = ("tools.test_support.plugin",)
    pytestmark = pytest.mark.usefixtures("outside_runner")

モデルを固定してレビューの helper を動かす test module は、同じ形で ``outside_codex`` を使う ::

    pytest_plugins = ("tools.test_support.plugin",)
    pytestmark = pytest.mark.usefixtures("outside_codex")

直接呼び出し
------------

::

    env = create_env_repo(tmp_path / "repo")
    worktree = create_owned_worktree(env.root, tmp_path / "owned")
    result = run_uv_offline(
        ["run", "--project", "tools/devenv", "--no-sync", "pytest"],
        cwd=worktree,
        cache_dir=tmp_path / "uv-cache",
    )

``create_env_repo`` は既定で ``tools/devenv/pyproject.toml`` と ``tools/devenv/uv.lock`` を実 checkout からコピーしてコミットする（ ``copy_paths`` で変えられる）。
