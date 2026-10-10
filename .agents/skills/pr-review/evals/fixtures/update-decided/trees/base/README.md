# syncer

計画（JSON）に書いた変更を、worktree（root）の中だけに書き込む。。

計画の変更対象（`target`）は root からの相対パスで書く。root の外を指す変更対象は拒否する。
