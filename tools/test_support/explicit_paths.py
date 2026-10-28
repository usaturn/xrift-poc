"""テストのパスを省略した pytest を、何も集めずに止める pytest プラグイン。

``tools/devenv/pyproject.toml`` の ``addopts`` の ``-p tools.test_support.explicit_paths`` で読み込む。
pytest は ``pythonpath`` （設定ファイルを基準にした ``../..`` ）を ``-p`` のプラグインより先に
``sys.path`` に入れるので、設定ファイルと同じ checkout のこのモジュールを読む。

パスを省略すると、pytest は ``testpaths`` を起動した場所（cwd）を基準に展開し、cwd が rootdir と
同じときだけ使う。一致が無いか cwd が rootdir と違えば、cwd から再帰的に集める。このため、起動した
場所によって、別の checkout のテスト（親の root から深さ 2 の worktree では、親の main checkout の
``.agents`` ）や一部だけ（ ``.agents`` を落とす）を、警告だけで、または警告も無く集める（#215）。
設定ファイルを基準にした書き方は ``testpaths`` に無いので、入口をパスを明示した形
（AGENTS.md「親の検証」）に限る。
"""

from __future__ import annotations

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_collection(session: pytest.Session) -> None:
    if session.config.args_source is not pytest.Config.ArgsSource.ARGS:
        raise pytest.UsageError(
            "テストのパスを指定して実行する。正式な対象は scripts/ci/verify-devenv.sh の pytest の引数と同じ"
            "（AGENTS.md「親の検証」）。パスを省略すると、起動した場所によって"
            "別の checkout のテストや一部だけを集める（#215）"
        )
