"""実環境の形をした共有 test fixture。

実親リポジトリの形（`public` gitlink とその submodule 状態、venv の無い
owned worktree、複数行の自由記述、venv 無し環境での uv の振る舞い）を、
ローカルの bare remote とオフライン指定だけで再現する。
"""

from tools.test_support.repos import (
    ParentRepo,
    SubmoduleState,
    TestSupportError,
    create_owned_worktree,
    create_parent_repo,
)
from tools.test_support.texts import FINDING_CONTENT, FINDING_REASON
from tools.test_support.uv import offline_uv_env, run_uv_offline

__all__ = [
    "FINDING_CONTENT",
    "FINDING_REASON",
    "ParentRepo",
    "SubmoduleState",
    "TestSupportError",
    "create_owned_worktree",
    "create_parent_repo",
    "offline_uv_env",
    "run_uv_offline",
]
