"""uv run --project tools/devenv python <このファイル> で実行する。"""

import importlib
from collections.abc import Callable
from typing import cast

if __name__ == "__main__":
    main = cast(Callable[[], int], importlib.import_module("issue_queue.cli").main)
    raise SystemExit(main())
