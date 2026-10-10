"""実環境の形をした共有 test fixture。

xrift-poc の環境リポジトリの形（items/・worlds/ を無視する main checkout、Git の有無と origin を選べる制作物、
venv の無い owned worktree、複数行の自由記述、venv 無し環境での uv の振る舞い）を、
ローカルの Git とオフライン指定だけで再現する。
"""

from tools.test_support.repos import (
    ENV_ORIGIN,
    EnvRepo,
    TestSupportError,
    create_env_repo,
    create_owned_worktree,
    create_product,
)
from tools.test_support.texts import FINDING_CONTENT, FINDING_REASON
from tools.test_support.uv import offline_uv_env, run_uv_offline

__all__ = [
    "ENV_ORIGIN",
    "FINDING_CONTENT",
    "FINDING_REASON",
    "EnvRepo",
    "TestSupportError",
    "create_env_repo",
    "create_owned_worktree",
    "create_product",
    "offline_uv_env",
    "run_uv_offline",
]
