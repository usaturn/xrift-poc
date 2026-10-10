#!/usr/bin/env python3
"""~/.claude.json に hasCompletedOnboarding: true を冪等かつアトミックに追記するスクリプト。"""

import json
import os
import shutil
import sys
import tempfile

CLAUDE_JSON = os.path.expanduser("~/.claude.json")
BACKUP = CLAUDE_JSON + ".bak"
KEY = "hasCompletedOnboarding"
VALUE = True


def main():
    # 既存 JSON を読み込み（なければ空 dict）
    data = {}
    if os.path.exists(CLAUDE_JSON):
        with open(CLAUDE_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)

    # 冪等性: 既に設定済みなら中断
    if data.get(KEY) == VALUE:
        print(f"{KEY} は既に {VALUE} です。変更なし。")
        return 0

    # バックアップ
    if os.path.exists(CLAUDE_JSON):
        shutil.copy2(CLAUDE_JSON, BACKUP)
        print(f"バックアップ作成: {BACKUP}")

    data[KEY] = VALUE

    # 一時ファイルに書き出して rename で一括置き換え
    dir_name = os.path.dirname(CLAUDE_JSON) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".claude.json.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        if os.path.exists(CLAUDE_JSON):
            shutil.copymode(CLAUDE_JSON, tmp_path)
        os.replace(tmp_path, CLAUDE_JSON)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

    print(f"{KEY} を {VALUE} に設定しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())