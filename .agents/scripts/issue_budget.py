"""Issue 全体の能動時間・checkpoint・同じ失敗の回数を、小さな JSONL ledger で数える（Issue #598）。

規則は ``.agents/execution-budget.md`` に定義する。この helper は数えるだけで、継続・縮小・停止を決めない。

- ``start``: ledger を新しく作り、予算・再試行上限・checkpoint の閾値を記録する
- ``mark``: 利用者待ちの開始と再開、checkpoint の判断、失敗、終了を 1 行ずつ追記する
- ``show``: 能動時間（壁時計の経過から利用者待ちを除いたもの）と、期日を過ぎた checkpoint、
  再試行上限に達した失敗を JSON で返す

並行する worker の時間は controller の壁時計の内側にあるので加算しない。壊れた行・時刻の逆転・
閉じていない利用者待ちの後の記録は、黙って捨てずに行番号付きで拒否する。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

Decision = Literal["continue", "shrink", "split", "change-method", "stop"]
FailureClass = Literal["reliability", "safety"]


class LedgerError(Exception):
    """ledger の行が仕様に合わない。line は 1 から数えた行番号。"""

    def __init__(self, line: int, message: str) -> None:
        self.line = line
        super().__init__(f"line {line}: {message}")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class StartEntry(_Strict):
    event: Literal["start"]
    at: AwareDatetime
    budget_minutes: float = Field(gt=0)
    retry_limit: int = Field(default=1, ge=0)
    checkpoint_percents: tuple[int, ...] = (50, 100)

    @model_validator(mode="after")
    def _percents(self) -> Self:
        if not self.checkpoint_percents or any(not 0 < value <= 100 for value in self.checkpoint_percents):
            raise ValueError("checkpoint_percents must be within 1..100")
        return self


class AwaitUserEntry(_Strict):
    event: Literal["await-user"]
    at: AwareDatetime
    phase: str | None = None
    reason: str | None = None


class ResumeEntry(_Strict):
    event: Literal["resume"]
    at: AwareDatetime
    phase: str | None = None


class CheckpointEntry(_Strict):
    event: Literal["checkpoint"]
    at: AwareDatetime
    phase: str | None = None
    decision: Decision
    reason: str = Field(min_length=1)


class FailureEntry(_Strict):
    event: Literal["failure"]
    at: AwareDatetime
    phase: str | None = None
    failure_class: FailureClass
    signature: str = Field(min_length=1)


class EndEntry(_Strict):
    event: Literal["end"]
    at: AwareDatetime
    phase: str | None = None
    reason: str | None = None


Entry = StartEntry | AwaitUserEntry | ResumeEntry | CheckpointEntry | FailureEntry | EndEntry
_ENTRY = TypeAdapter[Entry](Annotated[Entry, Field(discriminator="event")])


class FailureTally(_Strict):
    failure_class: FailureClass
    count: int


class CheckpointView(_Strict):
    at: AwareDatetime
    active_seconds: float
    decision: Decision
    reason: str


class Summary(_Strict):
    state: Literal["active", "waiting_user", "ended"]
    started_at: AwareDatetime
    wall_seconds: float
    user_wait_seconds: float
    active_seconds: float
    budget_seconds: float
    remaining_seconds: float
    used_ratio: float
    due_checkpoints: tuple[int, ...]
    failures: dict[str, FailureTally]
    retry_limit: int
    retry_limit_reached: tuple[str, ...]
    safety_failure: bool
    last_checkpoint: CheckpointView | None


def parse_entry(line: str | bytes) -> Entry:
    return _ENTRY.validate_json(line)


def load(path: Path) -> list[Entry]:
    entries: list[Entry] = []
    # 行の区切りは書き込みと同じ "\n" だけにする。splitlines() は JSON がエスケープしない
    # U+2028 などでも分割し、自由記述の reason を含む行を壊してしまう。
    for number, line in enumerate(path.read_text(encoding="utf-8").removesuffix("\n").split("\n"), 1):
        try:
            entries.append(parse_entry(line))
        except ValidationError as error:
            raise LedgerError(number, f"invalid entry ({error.error_count()} error(s))") from None
    return entries


def summarize(entries: Sequence[Entry], now: datetime) -> Summary:
    if not entries or not isinstance(entries[0], StartEntry):
        raise LedgerError(1, "the first entry must be start")
    start = entries[0]
    previous = start.at
    waiting_since: datetime | None = None
    ended_at: datetime | None = None
    user_wait = 0.0
    tallies: dict[str, FailureTally] = {}
    checkpoint_actives: list[float] = []
    last: CheckpointView | None = None
    for number, entry in enumerate(entries[1:], 2):
        if ended_at is not None:
            raise LedgerError(number, "no entry may follow end")
        if entry.at < previous:
            raise LedgerError(number, "time goes backwards")
        if waiting_since is not None and not isinstance(entry, ResumeEntry | EndEntry):
            raise LedgerError(number, "only resume or end may follow await-user")
        previous = entry.at
        match entry:
            case StartEntry():
                raise LedgerError(number, "start may appear only once")
            case AwaitUserEntry():
                waiting_since = entry.at
            case ResumeEntry():
                if waiting_since is None:
                    raise LedgerError(number, "resume requires a preceding await-user")
                user_wait += (entry.at - waiting_since).total_seconds()
                waiting_since = None
            case EndEntry():
                if waiting_since is not None:
                    user_wait += (entry.at - waiting_since).total_seconds()
                    waiting_since = None
                ended_at = entry.at
            case CheckpointEntry():
                active = (entry.at - start.at).total_seconds() - user_wait
                checkpoint_actives.append(active)
                last = CheckpointView(at=entry.at, active_seconds=active, decision=entry.decision, reason=entry.reason)
            case FailureEntry():
                count = tallies[entry.signature].count + 1 if entry.signature in tallies else 1
                tallies[entry.signature] = FailureTally(failure_class=entry.failure_class, count=count)
    finish = ended_at if ended_at is not None else max(now, previous)
    if waiting_since is not None:
        user_wait += (finish - waiting_since).total_seconds()
    wall = (finish - start.at).total_seconds()
    active = wall - user_wait
    budget = start.budget_minutes * 60
    due = tuple(
        percent
        for percent in start.checkpoint_percents
        if active >= budget * percent / 100 and not any(seen >= budget * percent / 100 for seen in checkpoint_actives)
    )
    return Summary(
        state="ended" if ended_at is not None else "waiting_user" if waiting_since is not None else "active",
        started_at=start.at,
        wall_seconds=wall,
        user_wait_seconds=user_wait,
        active_seconds=active,
        budget_seconds=budget,
        remaining_seconds=budget - active,
        used_ratio=active / budget,
        due_checkpoints=due,
        failures=tallies,
        retry_limit=start.retry_limit,
        retry_limit_reached=tuple(
            name
            for name, tally in tallies.items()
            if tally.failure_class == "reliability" and tally.count >= start.retry_limit + 1
        ),
        safety_failure=any(tally.failure_class == "safety" for tally in tallies.values()),
        last_checkpoint=last,
    )


def _now() -> datetime:
    return datetime.now().astimezone()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="ledger を新しく作る")
    start.add_argument("--ledger", type=Path, required=True)
    start.add_argument("--budget-minutes", type=float, required=True)
    start.add_argument("--retry-limit", type=int, default=1)
    start.add_argument("--checkpoint-percent", type=int, action="append", dest="percents")
    mark = commands.add_parser("mark", help="1 行追記する")
    mark.add_argument("--ledger", type=Path, required=True)
    mark.add_argument("--event", choices=("await-user", "resume", "checkpoint", "failure", "end"), required=True)
    for option in ("--phase", "--decision", "--reason", "--failure-class", "--signature"):
        mark.add_argument(option)
    show = commands.add_parser("show", help="集計を JSON で返す")
    show.add_argument("--ledger", type=Path, required=True)
    return parser


def _append(path: Path, entry: Entry, *, create: bool) -> None:
    flags = os.O_WRONLY | os.O_APPEND | (os.O_CREAT | os.O_EXCL if create else 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(entry.model_dump_json(exclude_none=True) + "\n")


def _fail(message: str) -> int:
    print(f"issue_budget: {message}", file=sys.stderr)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    now = _now()
    try:
        if args.command == "start":
            fields: dict[str, object] = {"event": "start", "at": now, "budget_minutes": args.budget_minutes}
            fields["retry_limit"] = args.retry_limit
            if args.percents:
                fields["checkpoint_percents"] = tuple(sorted(set(args.percents)))
            _append(args.ledger, StartEntry.model_validate(fields, strict=False), create=True)
            return 0
        entries = load(args.ledger)
        if args.command == "show":
            print(summarize(entries, now).model_dump_json(indent=2))
            return 0
        values = {"phase": args.phase, "decision": args.decision, "reason": args.reason}
        values |= {"failure_class": args.failure_class, "signature": args.signature}
        given = {name: value for name, value in values.items() if value is not None}
        entry = _ENTRY.validate_python({"event": args.event, "at": now, **given}, strict=False)
        summarize([*entries, entry], now)
        _append(args.ledger, entry, create=False)
        return 0
    except FileExistsError:
        return _fail(f"{args.ledger} already exists")
    except OSError as error:
        return _fail(f"{args.ledger}: {error.strerror}")
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in error.errors())
        return _fail(f"invalid {args.command}: {problems}")
    except LedgerError as error:
        return _fail(f"{args.ledger}: {error}")


if __name__ == "__main__":
    raise SystemExit(main())
