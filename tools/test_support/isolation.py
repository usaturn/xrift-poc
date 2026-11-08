"""テスト全体の起動時に効く pytest plugin（`tools/devenv/pyproject.toml` の `addopts` で `-p` 指定して読む）。

`REVIEW_EVAL_CONFIG` を空文字（明示的に無効）にし、利用者の収集の設定（`~/.config/xrift-poc/review-eval.toml`）を
テストに読ませない。収集の仮想記録（判断の記録・finalize の結果）は review_dir を持たないので、利用者の実設定が
有効で fixture の repo が sources に入っていると、テストの記録が実の私有の保存先へ書かれうる。

利用者の設定の有無・値にかかわらず上書きする。収集を有効にして確かめるテストは、テストの中で
`monkeypatch.setenv("REVIEW_EVAL_CONFIG", <一時の設定>)` で上書きする（終わると空文字に戻る）。子プロセスは空文字を
継承する。autouse の fixture ではなく起動時に 1 回だけ設定するので、fixture を使わないテスト・子プロセスにも効く。
"""

import os

import pytest


def pytest_configure(config: pytest.Config) -> None:
    os.environ["REVIEW_EVAL_CONFIG"] = ""
