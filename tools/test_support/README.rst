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
    GitHub の URL を bare の remote に差し替えた環境リポジトリを作り、偽の ``gh`` ・ ``herdr`` （ ``FAKE_CLI`` 。状態を ``services.json`` に読み書きする）を ``PATH`` の先頭に置く。
    ``QueueEnv`` は CLI の呼び出しと、 PR のマージ・子の pane の状態・報告など、各工程の前提を作る操作を持つ。 CLI の既定の出力は要約なので、 ``QueueEnv.call`` は ``--full`` を付けて run の全体（ ``run`` ）も受け取る。

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

``tools.test_support.explicit_paths``
    テストのパスを省略した pytest を、何も集めずに止める pytest プラグイン。 ``tools/devenv/pyproject.toml`` の ``addopts`` が ``-p`` で読み込む。
    パスを省略すると、pytest は起動した場所によって、別の checkout のテストや一部だけを集めるためである（#215）。
    パスは ``scripts/ci/verify-devenv.sh`` と AGENTS.md「親の検証」の形で指定する。

``tools.test_support.isolation``
    テスト全体の起動時に効く pytest plugin。 ``tools/devenv/pyproject.toml`` の ``addopts`` で ``-p`` 指定して読み、 ``pytest_configure`` で ``REVIEW_EVAL_CONFIG`` を空文字（収集を明示的に無効）にする（Issue #242）。
    利用者の設定（ ``~/.config/xrift-poc/review-eval.toml`` ）の有無・値にかかわらず上書きし、テストに読ませない。
    収集の仮想記録（判断の記録・finalize の結果）は review_dir を持たないので、利用者の実設定が有効で fixture の repo が sources に入っていると、テストの記録が実の私有の保存先へ書かれうるためである。
    autouse の fixture ではなく起動時に 1 回だけ設定するので、fixture を使わないテストと子プロセスにも効く。
    収集を有効にして確かめるテストは、テストの中で ``monkeypatch.setenv("REVIEW_EVAL_CONFIG", <一時の設定>)`` で上書きする（終わると空文字に戻る）。

``tools.test_support.review_eval``
    ``make_collection_workspace`` は、 ``create_env_repo`` で親の main checkout を作り、指定した ``owner/repo`` ごとに origin 付きの制作物を ``items/`` に作る。 ``(workspace_root, [親の repository, *制作物の repository])`` を返す。移植元の ``projects.toml`` を書く補助の代わりである。
    ``write_collection_config`` は、収集の設定（ ``review-eval.toml`` ）を一時ディレクトリに書く。承認済み（ ``enabled = true`` ）で、秘密検査には ``FAKE_SCANNER`` （検出なしの空のレポートを書く偽の scanner）を使う。
    ``make_observation`` は最小の observation を作り、 ``put_revisions_worker`` ・ ``append_times_worker`` は spawn したプロセスから保存先へ書くための worker である。

``tools.test_support.review_eval_ledger``
    確認待ち台帳（ ``tools.review_eval.ledger`` ）と packet のテストの共有 fixture。収集した ``review-findings/v2`` の候補一覧（別 repo の同じ番号を含む）から、候補・主張・許可・正解の回答を作る。

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
