"""task_watch のテスト用の fake CLI。モードごとに進捗 JSONL を書き、決まった終わり方をする。

usage: fake_worker.py MODE PROGRESS RESULT STATE_DIR [PARTIAL]
"""

from __future__ import annotations

import itertools
import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import FrameType

INTERVAL = 0.05


def append(path: Path, record: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + "\n")


def new_items(path: Path) -> None:
    for number in itertools.count(1):
        append(path, {"kind": "progress", "items": [f"確認した挙動 {number}"]})
        time.sleep(INTERVAL)


def wait_forever() -> None:
    while True:
        time.sleep(INTERVAL)


def main(argv: list[str]) -> int:
    mode, progress, result, state = argv[1], Path(argv[2]), Path(argv[3]), Path(argv[4])
    partial = Path(argv[5]) if len(argv) > 5 else None
    # テストが失敗しても、無限に書き続ける fake を残さない。
    signal.alarm(60)
    match mode:
        case "progress":
            for number in range(1, 6):
                append(progress, {"kind": "progress", "items": [f"確認した挙動 {number}"]})
                time.sleep(INTERVAL)
            result.write_text("{}\n", encoding="utf-8")
            return 0
        case "heartbeat":
            while True:
                append(progress, {"kind": "heartbeat"})
                append(progress, {"kind": "progress", "items": ["同じ項目"]})
                time.sleep(INTERVAL)
        case "failure":
            while True:
                append(progress, {"kind": "failure", "class": "reliability", "signature": "same-timeout"})
                time.sleep(INTERVAL)
        case "safety":
            append(progress, {"kind": "failure", "class": "safety", "signature": "unknown-send"})
            wait_forever()
        case "endless":
            new_items(progress)
        case "ignore-term":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            wait_forever()
        case "undeclared":

            def record_term(_signum: int, _frame: FrameType | None) -> None:
                (state.parent / "got-term").write_text("term\n", encoding="utf-8")

            signal.signal(signal.SIGTERM, record_term)
            time.sleep(1.5)
            return 0
        case "reserve-investigate":
            assert partial is not None
            partial.write_text("途中までの結論\n", encoding="utf-8")
            append(progress, {"kind": "saved", "path": str(partial)})
            new_items(progress)
        case "finalize-aware":
            for number in itertools.count(1):
                if (state / "finalize.requested").exists():
                    result.write_text("{}\n", encoding="utf-8")
                    append(progress, {"kind": "saved", "path": str(result)})
                    return 0
                append(progress, {"kind": "progress", "items": [f"確認した挙動 {number}"]})
                time.sleep(INTERVAL)
        case "finalize-late":
            # 保存への移行を見た直後に、もう 1 件だけ新しい項目を書き、保存に少し時間をかける worker。
            for number in itertools.count(1):
                if (state / "finalize.requested").exists():
                    append(progress, {"kind": "progress", "items": ["書きかけだった最後の確認"]})
                    time.sleep(0.5)
                    result.write_text("{}\n", encoding="utf-8")
                    append(progress, {"kind": "saved", "path": str(result)})
                    return 0
                append(progress, {"kind": "progress", "items": [f"確認した挙動 {number}"]})
                time.sleep(INTERVAL)
        case "safety-exit":
            append(progress, {"kind": "failure", "class": "safety", "signature": "unknown-send"})
            result.write_text("{}\n", encoding="utf-8")
            return 0
        case "orphan-child":
            subprocess.Popen(["sleep", "30"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return 0
        case _:
            return 64
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
