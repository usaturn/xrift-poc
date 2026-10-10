#!/usr/bin/env python3
"""~/.claude.json の hasCompletedOnboarding を false に戻す（poke_claude_onboarding.py の逆操作）。"""

import argparse
import json
import os
import shutil
import sys
import tempfile

CLAUDE_JSON = os.path.expanduser("~/.claude.json")
BACKUP = CLAUDE_JSON + ".bak"
KEY = "hasCompletedOnboarding"
VALUE = False


def main():
    parser = argparse.ArgumentParser(
        description="~/.claude.json の hasCompletedOnboarding を false に戻す"
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help=f"{BACKUP} が存在する場合、そこから復元する",
    )
    args = parser.parse_args()

    # --restore: バックアップから復元
    if args.restore:
        if not os.path.exists(BACKUP):
            print(f"バックアップ {BACKUP} が見つかりません。", file=sys.stderr)
            return 1
        # 現在の JSON をさらにバックアップ（安全策）
        if os.path.exists(CLAUDE_JSON):
            shutil.copy2(CLAUDE_JSON, CLAUDE_JSON + ".before_restore")
            print(f"復元前の状態を保存: {CLAUDE_JSON}.before_restore")
        shutil.copy2(BACKUP, CLAUDE_JSON)
        print(f"{BACKUP} から {CLAUDE_JSON} を復元しました。")
        return 0

    # 既存 JSON を読み込み（なければ空 dict）
    data = {}
    if os.path.exists(CLAUDE_JSON):
        with open(CLAUDE_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)

    # 冪等性: 既に false または未設定なら中断
    if data.get(KEY) != True:
        current = data.get(KEY, "未設定")
        print(f"{KEY} は既に {current} です。変更なし。")
        return 0

    # バックアップ（まだ存在しない場合のみ）
    if not os.path.exists(BACKUP):
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