"""収集の設定（review-eval.toml）のモデルと読み込み。"""

import importlib.util
import os
import tomllib
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Annotated, Literal, Self, cast, get_args

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, model_validator

from tools.review_eval import shim

Route = Literal["pr-review", "pr-review-fix-loop", "review-fix-planning", "review-verify"]
ROUTES: tuple[Route, ...] = get_args(Route)


class ConfigError(Exception):
    """設定の誤り。メッセージに設定値を含めない。"""


class _Model(BaseModel):
    # TOML は日付を date、パスを文字列で渡すため、型変換を許す（strict にしない）
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(_Model):
    repo: str = Field(min_length=1)
    routes: Annotated[list[Route], Field(min_length=1)]
    since: date | None = None
    until: date | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _unique_routes(self) -> Self:
        if len(set(self.routes)) != len(self.routes):
            raise ValueError("routes が重複している")
        return self


class Config(_Model):
    schema_version: Literal[1]
    enabled: StrictBool
    approved_by: str | None = None
    approved_on: date | None = None
    store: Path
    workspace_root: Path
    max_store_bytes: int = Field(gt=0)
    max_artifact_bytes: int = Field(default=4 * 1024 * 1024, gt=0)
    max_sweep_files: int = Field(default=2000, gt=0)
    scan_timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    lock_timeout_seconds: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    secret_scanner: list[str] | None = None
    sources: list[Source] = Field(default_factory=list[Source])

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if not self.store.is_absolute() or not self.workspace_root.is_absolute():
            raise ValueError("store と workspace_root は絶対パスにする")
        if self.enabled and (not self.approved_by or self.approved_on is None or not self.sources):
            raise ValueError("enabled の設定には approved_by・approved_on・sources が必要")
        repos = [source.repo.casefold() for source in self.sources]
        if len(set(repos)) != len(repos):
            raise ValueError("sources の repo が重複している（大文字と小文字は区別しない）")
        store = Path(os.path.normpath(self.store))
        for name in ("reviews", "items", "worlds"):
            forbidden = Path(os.path.normpath(self.workspace_root / name))
            if store == forbidden or forbidden in store.parents:
                raise ValueError(f"store は workspace_root/{name} の中に置けない")
        return self

    def source_for(self, repository: str) -> Source | None:
        key = repository.casefold()
        return next((source for source in self.sources if source.repo.casefold() == key), None)


def load_config(path: Path) -> Config:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"設定を TOML として読めない: {type(exc).__name__}") from None
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['type']}" for e in exc.errors())
        raise ConfigError(f"設定の内容が不正: {details}") from None


def _load_route_target() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / ".agents/scripts/route_target.py"
    spec = importlib.util.spec_from_file_location("route_target", path)
    if spec is None or spec.loader is None:
        raise ConfigError("route_target.py を読み込めない")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_registered(config: Config) -> None:
    """`sources[].repo` が親か、origin が GitHub の制作物のリポジトリのどれかであることを照合する。"""
    route_target = _load_route_target()
    root = config.workspace_root
    registered: set[str] = set()
    try:
        env_route = route_target.route(root, root)
        if env_route.kind != "env":
            raise ConfigError("workspace_root が開発環境のリポジトリではない")
        registered.add(cast(str, env_route.repository).casefold())
        for kind in ("items", "worlds"):
            base = root / kind
            if not base.is_dir():
                continue
            for child in sorted(base.iterdir()):
                if not child.is_dir():
                    continue
                product = route_target.route(child, root)
                if product.kind == "product" and product.repository is not None:
                    registered.add(cast(str, product.repository).casefold())
    except route_target.RouteError:
        raise ConfigError("workspace_root の操作先を判定できない") from None
    missing = [source.repo for source in config.sources if source.repo.casefold() not in registered]
    if missing:
        raise ConfigError(f"親にも制作物にも当たらない repo: {', '.join(missing)}")


def load_active(environ: Mapping[str, str] | None = None) -> Config | None:
    """有効な設定だけを返す。ファイルが無い・無効なら None。有効なのに誤りがあれば ConfigError。"""
    path = shim.config_path(environ)
    if path is None or not path.is_file():
        return None
    config = load_config(path)
    if not config.enabled:
        return None
    check_registered(config)
    return config
