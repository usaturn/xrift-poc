"""持ち越しの投稿を保存したintentと照合し、送信を繰り返さない。"""

from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from pydantic import Field

from .external import REPOSITORY, command, object_json
from .models import Attempt, Effect, Record, Run
from .storage import QueueError

if TYPE_CHECKING:
    from .workflow import Workflow

# 投稿の1件の読み取りが失敗したときの読み直しの回数と間隔。GETだけで、POSTはしない。
READ_ATTEMPTS = 3
READ_INTERVAL = 2


class Publication(Record):
    key: str = Field(min_length=1, max_length=100)
    kind: Literal["issue", "comment"]
    target: int | None
    title: str | None
    body: str = Field(min_length=1)


def inherited_effects(run: Run, attempt: Attempt) -> list[Effect]:
    """retryで引き継ぎが続くattemptの投稿の記録の写し。retryは全投稿を照合できたときだけ通るので、どれも照合済み。

    マーカーで重複を除き、投稿したattemptをinherited_fromに残す。新しいattemptで同じkeyのpublishは照合だけになる。
    """
    effects: dict[str, Effect] = {}
    for previous in run.handovers(attempt.issue, attempt.phase):
        for effect in previous.effects:
            inherited_from = effect.inherited_from or previous.id
            effects.setdefault(effect.marker, effect.model_copy(update={"inherited_from": inherited_from}))
    return list(effects.values())


def read_effect(work: Workflow, effect: Effect) -> dict[str, Any]:
    """POSTの応答の番号の1件を読む。作成の直後に読めなければ、間を置いて読み直す（POSTはしない）。"""
    endpoint = f"issues/{effect.remote_id}" if effect.kind == "issue" else f"issues/comments/{effect.remote_id}"
    tries = 0
    while True:
        try:
            return work.ext.get(endpoint)
        except QueueError as error:
            tries += 1
            if tries >= READ_ATTEMPTS:
                raise QueueError(f"投稿 {effect.key} を読み戻せません。再送しません（{error}）") from error
            time.sleep(READ_INTERVAL)


def reconcile_effects(work: Workflow, attempt: Attempt) -> None:
    """投稿を読み戻して照合する。番号が分かれば1件を読み、応答が失われた投稿だけ一覧の全ページから探す。

    一覧は作成の直後に新しい投稿を含まないことがある。見つからなくても再送しない。
    """
    for effect in attempt.effects:
        if effect.remote_id is not None:
            item = read_effect(work, effect)
            if effect.marker not in (item.get("body") or ""):
                raise QueueError(f"投稿 {effect.key} の読み戻しが一致しません")
        else:
            endpoint = (
                "issues?state=all&per_page=100"
                if effect.kind == "issue"
                else f"issues/{effect.target}/comments?per_page=100"
            )
            objects = work.ext.pages(endpoint)
            matches = [item for item in objects if effect.marker in (item.get("body") or "")]
            if len(matches) != 1:
                raise QueueError(f"投稿 {effect.key} の成否不明または重複です。再送しません")
            item = matches[0]
        if item["body"] != effect.body or (effect.kind == "issue" and item.get("title") != effect.title):
            raise QueueError(f"投稿 {effect.key} の読み戻しが一致しません")
        # 以後の照合（待機・retry・advance）は、この番号の1件を読む。
        effect.remote_id = item["number"] if effect.kind == "issue" else item["id"]
        effect.url = item["html_url"]
        effect.state = "confirmed"
        work.save()


def publish(work: Workflow, attempt_id: str, request: Publication) -> None:
    attempt = work.child(attempt_id)
    if attempt.phase != "followup" or not attempt.acknowledged or attempt.result is not None:
        raise QueueError("受領済みの持ち越し工程でだけ投稿できます")
    existing = next((effect for effect in attempt.effects if effect.key == request.key), None)
    if existing:
        if (
            existing.kind != request.kind
            or existing.target != request.target
            or existing.title != request.title
            or existing.body != request.body + "\n\n" + existing.marker
        ):
            raise QueueError("同じkeyで異なる投稿はできません")
        reconcile_effects(work, attempt)
        return
    if any(e.state != "confirmed" for e in attempt.effects):
        raise QueueError("成否不明の投稿を照合してから次の投稿へ進んでください")
    # 別keyに変えて同じ投稿を送らない。retryで引き継いだ投稿の記録とも比べる。
    # 記録した投稿の送信本文やその引用は、マーカーの照合を重複させるので、kind・target・titleによらず送らない。
    marked = next((e for e in attempt.effects if e.marker in request.body), None)
    if marked is not None:
        raise QueueError(
            f"本文に投稿 {marked.key} のマーカーが含まれます（{marked.url}）。送信した本文やその引用は投稿できません"
        )
    same = next(
        (
            e
            for e in attempt.effects
            if (e.kind, e.target, e.title) == (request.kind, request.target, request.title)
            and e.body == request.body + "\n\n" + e.marker
        ),
        None,
    )
    if same is not None:
        raise QueueError(f"同じ内容の投稿がkey {same.key} で記録されています（{same.url}）。同じkeyで照合してください")
    implementation = next(
        a for a in reversed(work.run.attempts) if a.issue == attempt.issue and a.phase == "implement"
    )
    if not implementation.pr_url:
        raise QueueError("元PRが未確認です")
    pr_number = int(implementation.pr_url.rsplit("/", 1)[-1])
    if request.kind == "issue":
        if request.target is not None or not request.title or "\n" in request.title:
            raise QueueError("Issueのtitleは1行、targetはnullで指定してください")
    else:
        if request.title is not None or request.target is None or request.target < 1:
            raise QueueError("コメントには対象番号が必要です")
        if request.target != pr_number and work.ext.issue(request.target)["state"] != "OPEN":
            raise QueueError("集約先Issueがopenではありません")
    # タイトルと本文は既存スキルのcheck_bodyで検査された入力を受け取る。
    marker = f"<!-- issue-queue-effect:{work.run.id}:{attempt.id}:{uuid4().hex} -->"
    effect = Effect(
        key=request.key,
        kind=request.kind,
        target=request.target,
        title=request.title,
        marker=marker,
        body=request.body + "\n\n" + marker,
    )
    attempt.effects.append(effect)
    work.run.event("publish_intent", attempt=attempt.id, key=effect.key)
    work.save()
    endpoint = (
        f"repos/{REPOSITORY}/issues"
        if effect.kind == "issue"
        else f"repos/{REPOSITORY}/issues/{effect.target}/comments"
    )
    payload = {"body": effect.body}
    if effect.title:
        payload["title"] = effect.title
    file = work.path / f"post-{uuid4().hex}.json"
    file.write_text(json.dumps(payload, ensure_ascii=False))
    # 成功応答でも読み戻す。timeout後はintentを保持し、このPOSTへ戻らない。
    response = command(["gh", "api", endpoint, "--method", "POST", "--input", str(file)])
    # 応答の番号でその1件を読み戻す。応答を読めなければ止まり、同じkeyの再実行で一覧から照合する。
    unreadable = f"投稿 {effect.key} の応答を確認できません。再送しません。同じkeyで照合し直してください"
    try:
        created = object_json(response)
    except (QueueError, ValueError) as error:
        raise QueueError(unreadable) from error
    remote_id = created.get("number" if effect.kind == "issue" else "id")
    if type(remote_id) is not int or remote_id < 1:
        raise QueueError(unreadable)
    effect.remote_id = remote_id
    work.save()
    reconcile_effects(work, attempt)


def check_followup_result(work: Workflow, attempt: Attempt) -> None:
    assert attempt.result is not None
    reconcile_effects(work, attempt)
    confirmed = {e.url for e in attempt.effects}
    own = {e.url for e in attempt.effects if e.inherited_from is None}
    # このattemptの投稿はすべて結果に書く。retryで引き継いだ投稿は、書いても書かなくてもよい。
    if not own <= set(attempt.result.urls) <= confirmed:
        raise QueueError("持ち越し結果のURLと、投稿・読み戻し済みのURLが一致しません")
    for url in attempt.result.issues:
        if not re.fullmatch(rf"https://github.com/{REPOSITORY}/issues/[1-9]\d*", url):
            raise QueueError("追加Issueは同repositoryのURLで指定してください")
        work.ext.issue(int(url.rsplit("/", 1)[-1]))
