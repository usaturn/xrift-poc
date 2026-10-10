# エージェント引き継ぎのスキーマ

**`schema_version` 2。** 読む側は version 1 のアーティファクトも引き続き受け付けなければならない。[version 1 のアーティファクトを読む](#version-1-のアーティファクトを読む)を参照する。

handoff は、親リポジトリの main worktree（`uv run --no-project python .agents/scripts/agent_tmp.py root` が出力するディレクトリ）の `tmp/docs_draft/claude_handoffs/` に置く 2 ファイルの対である。

- `<timestamp>-<session>.handoff.md` — 人が読む
- `<timestamp>-<session>.handoff.json` — プログラムが読む

2 つは同じ事実を持つ。両方を書く。

## 目次

- [handoff の書き方](#handoff-の書き方)
- [必須フィールド](#必須フィールド)
- [JSON のフィールド](#json-のフィールド)
- [Markdown の節](#markdown-の節)
- [version 1 のアーティファクトを読む](#version-1-のアーティファクトを読む)
- [安全性](#安全性)

## handoff の書き方

対の 2 ファイルをこのスキーマに従って手書きする。

**これらのフィールドが重要な理由。** 何が*決まった*か、次のエージェントが何に*触ってはいけない*かを知っているのは、作業を渡す側のエージェントだけである。`decisions`、`out_of_scope`、`verification`、`landing` は、毎回、渡す側のエージェントが書く。これらのフィールドが空の下書きは下書きであって、handoff ではない。

## 必須フィールド

次のフィールドがすべてそろったとき、handoff は完成する。空にしてよいのは、空であることが事実で、そう明記した場合だけである（`"decisions": []` は「何も決めていない」を意味し、「まだ書いていない」を意味しない）。

| フィールド | 必須である理由 |
|---|---|
| `schema_version`, `generated_at`, `project_dir` | アーティファクトを識別する |
| `session.id`, `session.stop_reason` | どこから来て、なぜ止まったか |
| `decisions` | 裁定が無いと、受け手は決着済みの判断を蒸し返す |
| `out_of_scope` | 無いと、受け手は意図的に除外した作業を「親切に」やってしまう |
| `verification.commands`, `verification.isolation` | 無いと、受け手は独自の検証を発明するか、破壊的な検証を走らせる |
| `landing.branch`, `landing.pr`, `landing.prohibited` | 無いと、受け手は二本目の PR を開く |
| `git.worktree`, `git.branch`, `git.head`, `git.status` | 親チェックアウトが作業のある場所ではないことが多い |
| `sources.spec` | 長い理由付けは、Spec があれば Spec に置く。無いときは `null` / `none` にし、理由を書く |

それ以外のフィールド — `skills`、`tasks`、`reports`、`next_work`、`sources.plan`、`sources.ledger`、`sources.session_jsonl`、`sources.plan_files`、`git.merge_base` — は任意で、事実であるときは入れる価値がある。

## JSON のフィールド

- `schema_version`: `"2"`。
- `generated_at`: UTC のタイムスタンプ。
- `project_dir`: handoff が対象とするリポジトリのパス。
- `session`:
  - `id`, `path`, `ai_title`, `agent_name`, `permission_mode`, `last_prompt`
  - `stop_reason`: 区切りを 1 行で書く。何が終わっていて、何に着手していないか。
- `skills`: 使っているスキルの名前（検出した `attributionSkill` の値、または自分が呼び出したもの）。
- `tasks`: `id`, `subject`, `status`。ランタイムの todo / plan の機能を使ったときは、そこから取る。todo リストを一度も開かなかったエージェントには `tasks` が無い。作り話をせず、無いと書く。
- `reports`: 秘密を伏せたレポートの抜粋。`timestamp`, `skill`, `text` を持つ。
- `decisions`: 決着済みの判断ごとに 1 エントリ。ここで理由付けを繰り返さない。1 行にし、論証は `sources.spec` で指す。
  - `id`: Spec で使う安定したラベル（`"F-1"`, `"M-2"`）。
  - `disposition`: `"fix"`（対応する）| `"skip"`（意図的に対応しない）|
    `"deferred"`（人の判断待ち）| `"resolved"`（現在の HEAD で既に満たされている）。
    判断が `review-fix-planning` から来たときは、区分を次のように対応させる。

    | review-fix-planning | disposition |
    |---|---|
    | Fix | `fix` |
    | Hold | `deferred` |
    | Skip | `skip` |
    | Resolved | `resolved` |

    意図した挙動を問う論点は Skip のままであり、したがって `skip` にする。`deferred` に上げない。

  - `subject`: 何についての判断か。
  - `rationale`: 1 行。なぜこの disposition にしたか。
  - `source`: どこから来たか（レビューファイルと指摘の id、Issue 番号、ユーザのメッセージ）。
  - `status`: `"pending"` | `"in_progress"` | `"done"` — `fix` のエントリだけに付ける。
- `out_of_scope`: 受け手がしてはいけないことを、普通の文で書く。助言ではなく拘束力を持つ。役に立ちそうに見える紛らわしいもの（隣のファイル、「ついでに」のリファクタ、`skip` にした指摘）も入れる。
- `verification`:
  - `commands`: 作業を確かめるために受け手が実行するコマンドそのもの。
  - `isolation`: それらのコマンドが実環境に触れないようにする方法 — 一時ディレクトリ、環境変数の上書き、書き込んではいけない実際のパス。これを省くと、設定まわりのバグを再現しようとした受け手が、ユーザの実際の設定に対して再現してしまう。
  - `evidence`: 既に確かめていて、やり直す必要の無いもの。
- `landing`:
  - `branch`: 作業を反映するブランチ。
  - `pr`: 既存の PR。`owner/repo#N` または `#N`。無いときは `null` にし、新しく開いてよいかを `prohibited` に書く。
  - `prohibited`: 反映の時点で起きてはいけないこと（新しい PR、別のブランチ、ここでは適用しないゲート）。
  - `commit_trailers`: リポジトリが commit に求める trailer。原文のまま書く。
- `next_work`: 次の手順を順に並べる。最も差し迫ったものを先頭に置く。
- `sources`:
  - `spec`（必須）: 理由付けを持つ設計・Spec へのパス。Spec が無いときは `null` / `none` にし、`landing.pr` と同じように理由を書く。
  - `plan`, `ledger`, `sdd_workspace`, `session_jsonl`, `plan_files`: 存在するときに書く。
  - エディタのツールではなくシェルの heredoc で書いたファイルは書き忘れやすい。明示的に列挙する。
- `git`:
  - `worktree`（必須）: 作業が実際にあるチェックアウト。親チェックアウトがそれと違う（detached、別のブランチ）ときは、親も書く — `branch` を読んで親チェックアウトで実行した受け手は、別のツリーを編集する。
  - `branch`, `head`, `status`（必須）: 「必須フィールド」の表と同じ扱いで書く。
  - `merge_base`: 分岐点が重要なときに書く。

## Markdown の節

JSON と同じ事実を、読みやすい順に並べる。`Session`、`Detected Skills`、`Current Phase / Gate`、`Task State`、`Decisions`、`Out of Scope`、`Verification`、`Landing`、`Next Recommended Work`、`Sources`、`Git`。

`Decisions` は表にすると読みやすい。

```markdown
## Decisions

| ID | Disposition | Subject | Rationale |
|---|---|---|---|
| F-1 | fix | piccg の provider 判定が非 dict で traceback | 現行 HEAD で再現。宣言した fail-open に反する |
| S-4 | skip | setup:287 に同じガードを足す | ステップ 2 が先に落ちるため到達不能 |
```

`Out of Scope` と `Landing` はリストにすると読みやすい。`Landing` の禁止事項は命令の形のまま書く — 「新しい PR を開かない」は流し読みしても伝わるが、「既存の PR を使うのが望ましい」は伝わらない。

## version 1 のアーティファクトを読む

version 1 には `decisions`、`out_of_scope`、`verification`、`landing` が無く、その `git` の節は worktree ではなく親チェックアウトを記述していることがある。これらは **「空」ではなく「不明」** として扱う。動く前に、欠けている disposition と反映先をユーザに確認する。会話の記録や、ファイルが編集されたという事実から推測しない。

## 安全性

`SECRET`、`TOKEN`、`CREDENTIAL`、`PASSWORD`、`API_KEY` を名前に含む、代入の形をした秘密は伏せる。最も漏れやすいのは `verification.commands` である — 環境変数の名前は書いてよいが、値は書かない。ローカルの開発環境の外へ共有する前に、handoff を見直す。
