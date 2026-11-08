"""収集 hook を呼び出す薄い入口。標準ライブラリだけを使う（pydantic の無い `uv run --no-project` から読まれる）。"""

import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path

CONFIG_ENV = "REVIEW_EVAL_CONFIG"
RUNNER_DEPTH_ENV = "AGENT_RUNNER_DEPTH"


def config_path(environ: Mapping[str, str] | None = None) -> Path | None:
    env = os.environ if environ is None else environ
    explicit = env.get(CONFIG_ENV)
    if explicit is not None:
        return Path(explicit).expanduser() if explicit else None
    xdg = env.get("XDG_CONFIG_HOME", "")
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path(env.get("HOME") or str(Path.home())) / ".config"
    return base / "xrift-poc" / "review-eval.toml"


def enabled_flag(path: Path) -> bool | None:
    """設定ファイルの `enabled` が true か。ファイルはあるが TOML として読めなければ None。"""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except OSError, ValueError:
        return None
    return data.get("enabled") is True


def enabled_config(environ: Mapping[str, str] | None = None) -> Path | None:
    """ファイルがあり、TOML として読めて `enabled = true` のときだけパスを返す。例外は出さない。"""
    try:
        path = config_path(environ)
        if path is None or not path.is_file():
            return None
        return path if enabled_flag(path) else None
    except Exception:
        return None


def in_runner_child(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return bool(env.get(RUNNER_DEPTH_ENV))


def run_hook(
    *,
    tool_root: Path,
    route: str,
    event: str,
    review_dir: Path | None = None,
    files: Sequence[Path] = (),
    records: Sequence[tuple[str, Path]] = (),
    repository: str | None = None,
    pr: int | None = None,
    context: Mapping[str, object] | None = None,
    timeout: float = 120.0,
    environ: Mapping[str, str] | None = None,
) -> None:
    """収集 hook を子プロセスで起動する。失敗は標準エラーへ 1 行出すだけで、例外を出さない。"""
    try:
        if in_runner_child(environ):
            return
        path = config_path(environ)
        if path is None or not path.is_file():
            return
        flag = enabled_flag(path)
        if flag is None:
            _warn("設定ファイルを TOML として読めない")
            return
        if not flag:
            return
        argv = [
            "uv",
            "run",
            "--project",
            str(tool_root / "tools/devenv"),
            "--locked",
            "python",
            "-m",
            "tools.review_eval",
            "hook",
            "--route",
            route,
            "--event",
            event,
        ]
        if review_dir is not None:
            argv += ["--review-dir", str(review_dir)]
        for file in files:
            argv += ["--file", str(file)]
        for vpath, file in records:
            argv += ["--record", f"{vpath}={file}"]
        if repository is not None and pr is not None:
            argv += ["--repo", repository, "--pr", str(pr)]
        if context is not None:
            argv += ["--context-json", json.dumps(context, ensure_ascii=False)]
        try:
            result = subprocess.run(
                argv,
                cwd=tool_root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            _warn("timeout")
            return
        except OSError as exc:
            _warn(f"{type(exc).__name__}")
            return
        if result.returncode != 0:
            _warn(_one_line(result.stderr) or f"exit {result.returncode}")
    except Exception as exc:
        _warn(type(exc).__name__)


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _warn(reason: str) -> None:
    try:
        print(f"review-eval: hook failed: {reason}", file=sys.stderr)
    except Exception:
        pass
