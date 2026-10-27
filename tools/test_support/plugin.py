"""`tools.test_support` の pytest fixture。

利用側の test module で ``pytest_plugins = ("tools.test_support.plugin",)``
を宣言して読み込む。どちらの fixture も、外側の実行環境が付ける環境変数を 1 つ、テストの中だけ外す。
``outside_runner`` は runner の担当が付ける ``AGENT_RUNNER_DEPTH`` を、``outside_codex`` は Codex が
ツールの実行に付ける ``CODEX_THREAD_ID`` を外す。
環境リポジトリと制作物は ``tools.test_support.repos`` の factory を直接呼んで作る。

ここの fixture は autouse にしない。test module で読み込んだプラグインの fixture は
テスト一式から見え、autouse だと読み込んだ後に収集したモジュールにも効く。
"""

from __future__ import annotations

import pytest


@pytest.fixture
def outside_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    """テストの中だけ ``AGENT_RUNNER_DEPTH`` を外し、runner の担当の中でもテストを実行できるようにする。

    runner は担当に ``AGENT_RUNNER_DEPTH=1`` を付け、``run_workflow`` と CLI はそれを見て
    ``nested_invocation`` で止まる（入れ子の防止）。偽の adapter・偽の CLI だけを起動するテストも
    本物の ``run_workflow`` を通るので、担当が関連テストを実行すると検査したい挙動の前に落ちる。

    偽の担当だけを起動する test module が、モジュール単位で明示して使う::

        pytest_plugins = ("tools.test_support.plugin",)
        pytestmark = pytest.mark.usefixtures("outside_runner")

    本物の担当を起動しうるテストには使わない。入れ子の防止そのものを確かめるテストは、
    テストの中で ``AGENT_RUNNER_DEPTH`` を設定し直すので、この fixture を使うモジュールでも通る。
    """
    monkeypatch.delenv("AGENT_RUNNER_DEPTH", raising=False)


@pytest.fixture
def outside_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    """テストの中だけ ``CODEX_THREAD_ID`` を外し、Codex の中でもモデルを固定したテストを実行できるようにする。

    Codex はツールの実行に ``CODEX_THREAD_ID`` を付ける。pr-review の helper はこれがあると、
    その thread の rollout から実行中のモデルを読み、引数のモデルと照合する。モデルを固定したテストは、
    Codex の中では固定値と違うモデルで ``explicit model does not match Codex runtime model`` になる（#81）。

    モデルを固定するテストの module が、モジュール単位で明示して使う::

        pytest_plugins = ("tools.test_support.plugin",)
        pytestmark = pytest.mark.usefixtures("outside_codex")

    Codex の中でモデルを読む挙動を確かめるテストは、テストの中で一時の ``CODEX_HOME`` と rollout を作り、
    ``CODEX_THREAD_ID`` を設定し直すので、この fixture を使うモジュールでも通る。
    """
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
