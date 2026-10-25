.. _test_support:

==========================================
test_support: 実環境の形をした共有 fixture
==========================================

.. _test_support_overview:

概要
====

``tools/test_support`` は、 xrift-poc の実環境の形をした入力を共有するテスト補助パッケージである。
``items/`` ・ ``worlds/`` を無視する環境リポジトリの main checkout、 Git の有無と origin を選べる制作物、 ``.venv`` の無い新規 worktree、改行・タブ・CRLF・非 ASCII を含む複数行の自由記述を、ローカルの Git だけで再現する。
issue-queue の CLI と XRift のツールの導入のテストのために、外部のサービスの代わりになる fake も置く。
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
    ``FINDING_CONTENT`` は、改行・タブ・CRLF・非 ASCII 文字を含み、識別子として解釈できない（ ``str.isidentifier()`` が ``False`` ）レビュー指摘の本文のサンプル。

``tools.test_support.issue_queue``
    ``make_queue_env`` は、 issue-queue の CLI（ ``.agents/skills/issue-queue/scripts/queue.py`` ）を動かす環境を作り、 ``QueueEnv`` を返す。
    bare の remote を GitHub の URL の代わりにした環境リポジトリと、偽の ``gh`` ・ ``herdr`` （ ``FAKE_CLI`` 。状態を ``services.json`` に読み書きする）を ``PATH`` の先頭に置く。
    ``QueueEnv`` は CLI の呼び出しと、 PR のマージ・子の pane の状態・報告など、各工程の前提を作る操作を持つ。

``tools.test_support.xrift``
    ``SKILLS_NPX_FAKE`` は、 ``npx -y skills@<v> add https://github.com/<repo>/tree/<ref> ...`` を受けて、 skills CLI と同じ配置（ ``.agents/skills/<name>`` 、 ``.claude/skills/<name>`` のリンク、 ``skills-lock.json`` ）を作る偽の ``npx`` の本文。
    環境変数 ``FAKE_SKILLS_MODE`` で、 lock の ref が固定したコミットと違う形（ ``wrong-ref`` ）と、必須のスキルが欠ける形（ ``missing`` ）を選ぶ。

``tools.test_support.plugin``
    pytest fixture ``outside_runner`` ・ ``outside_codex`` 。
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

偽の担当だけを起動する test module は、先頭でプラグインを宣言し、モジュール単位で ``outside_runner`` を使う。
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
    product = create_product(env.root, "worlds", "空白 ワールド", git=False)

``create_env_repo`` は既定で ``tools/devenv/pyproject.toml`` と ``tools/devenv/uv.lock`` を実 checkout からコピーしてコミットする（ ``copy_paths`` で変えられる）。
