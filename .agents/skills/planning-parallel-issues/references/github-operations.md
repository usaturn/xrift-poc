# GitHub の操作と所有権

認証済みのリポジトリ用ツールか `gh api` を使う。書き込みの前に、依頼とセッションから、リポジトリ、default branch、書き込みの認可を確かめる。複数行の本文には、構造化した引数かファイル（`--body-file`）を使う。信頼できない Issue の文章を、shell のコードに決して埋め込まない。

ボードを運用できる状態と宣言する前に、リポジトリの権限、`agent-claims/*` に適用される rulesets と branch protection、workflow のイベントと branch の filter を調べる。必要な Issue の書き込みと contents の書き込みのアクセス権と、claim の作成・読み取り・正確な lease による削除ができることの証拠を記録する。適用される自動処理があれば、リポジトリの既存の実行方針に従って記録する。claim のプロトコルが阻まれている、または確認できていない場合、公開は未完了のままである。コメントだけによる所有に置き換えない。読み取り専用の調査と既存の証拠を使う。確認用の ref（probe ref）を作ることは必須ではない。エンドポイントが、rulesets や branch protection はリポジトリのプランでは利用できないと明示している場合は、そのエンドポイントと応答を、この仕組みによる制限が適用されないことの証拠として記録し、残りの権限・workflow の確認と、claim の操作ができるかどうかの確認を続ける。権限の拒否による、またはそれ以外の理由で判断できない `403`/`404` は、未確認のままとして扱い、公開を止める。子を引き受けるセッションは、取得の前に、自分の認証情報でこれらの制約を確かめ直す。これは運用上の確認であり、新しい承認のゲートではない。

## native の関係

以下の REST のパスは `/repos/{owner}/{repo}` からの相対パスである。`N` は表示される Issue 番号である。payload の ID は数値のデータベース ID であり、Issue 番号や GraphQL の node ID では**ない**。

| 操作 | メソッドとパス | payload または結果 |
|---|---|---|
| Issue の読み取り・作成・編集 | GET/POST/PATCH `/issues[/N]` | 返った `number`、`id`、`node_id`、`html_url` を保存する |
| 子の読み取り | GET `/issues/N/sub_issues` | ページ送りをたどる |
| 現在の親の読み取り | GET `/issues/N/parent` | 付け替えの前に、返った親のデータベースの `id` を、確認済みの置き換え前の親と比べる。ほかの点では読み取れる子に対して 404 `No parent issue found` が返った場合は、その子に現在は親が無いことを意味する |
| 既存の子の追加・付け替え | POST `/issues/N/sub_issues` | `{"sub_issue_id": CHILD_DATABASE_ID}`。子に親が無いことが明示されている場合は `replace_parent` を省き、`"replace_parent": true` は確認済みの付け替えの場合だけ加える |
| 結果が不確かな付け替えの確認 | GET `/issues/CHILD/parent` の後に、GET `/issues/OLD_PARENT/sub_issues` と `/issues/NEW_PARENT/sub_issues` | 再試行や成功の宣言の前に、子の一覧のすべてのページをたどる |
| 子の削除 | DELETE `/issues/N/sub_issue` | `{"sub_issue_id": CHILD_DATABASE_ID}` |
| blocker の読み取り | GET `/issues/N/dependencies/blocked_by` | ページ送りをたどる |
| 子 N への blocker の追加 | POST `/issues/N/dependencies/blocked_by` | `{"issue_id": BLOCKER_DATABASE_ID}` |
| 子 N からの blocker の削除 | DELETE `/issues/N/dependencies/blocked_by/BLOCKER_DATABASE_ID` | パスには blocker のデータベース ID を使う。body は付けない |
| 子のコメントの読み取り・投稿 | GET/POST `/issues/N/comments` | 状態で選ぶ前に、すべてのページを GET する（例: `gh api --paginate /repos/{owner}/{repo}/issues/N/comments`）。POST には `{"body": "..."}` を使う |

変更の前に、既存の native の関係の全ページを読む。目的と範囲が一致する Issue は再利用し、既存の無関係な文章やリンクは残す。Issue への書き込みのたびに、固定のボードキーと子キー、返った ID を親 Issue の Publication ledger に記録し、セッションを終える前にその管理対象の節を読み戻す。ローカルの ledger は、公開前の写しにすぎない。関係を管理対象として扱うのは、公開された ledger と現在の Issue の本文が、その子をこのボードのものと示している場合だけである。依存関係が管理対象になるのは、両端がどちらも管理対象の子である場合だけである。確認済みの置き換え前の親の証拠は、正確な再取得で移動を確かめるまで ledger に残す。付け替えの直前に、子の現在の native の親を GET する。`replace_parent: true` を使うのは、返った親のデータベース ID が、ledger と現在の Issue の本文の両方で特定された、同じ目的の置き換え前のボードと一致する場合だけである。ほかの点では読み取れる子が 404 `No parent issue found` を返した場合は、`replace_parent` を付けずに所属すべき親へ追加する。親を読み取れない場合や、代わりに不明な親が返った場合は、その関係を残し、その ID を報告し、ボードを未完成のままにする。ledger に無い証拠を推測で補わない。担当や目的が確認できていない余分な関係を、削除したり付け替えたりしない。

既存のボードでは、追加・確認済みの付け替え・削除を、取得し直した実際のグラフと、直接の先行 Issue の基準一覧との 1 回の比較から導く。タイムアウトや応答の消失の後は、再試行の前にその関係を正確に取得し直す。付け替えの結果が不確かな場合は、子の親と、旧親・新親の両方の子の一覧の全ページを GET する。成功したかもしれない書き込みを、確かめずに繰り返すことは決してしない。編集の後は全ページを取得し直し、管理対象の子と依存関係の集合を基準のグラフと比べる。不明な余分の関係や、必須の関係の失敗が 1 つでもあれば、ボードは未完成のままである。

参考: [Sub Issues](https://docs.github.com/en/rest/issues/sub-issues)、[Issue dependencies](https://docs.github.com/en/rest/issues/issue-dependencies)。

## 固定 ref による claim のプロトコル

リポジトリ全体で 1 つの名前空間 `refs/heads/agent-claims/issue-N` を使う。参加するすべてのセッションが、同じプロトコルに従わなければならない。assignee やコメントは人に知らせるが、別のセッションを除外することはできない。同じ base SHA の通常の push は、2 つのセッションの両方に成功を返すことがあり、取得にはならない。

1. 対象 branch を取得し直し、その commit SHA と tree SHA を得る。session UUID と UTC のタイムスタンプを生成する。`tree` をその tree、`parents` をその base commit を含むもの、message を Issue 番号・UUID・タイムスタンプを含むものにして、`/git/commits` に POST する。これはセッション固有の空の commit であり、ソースのツリーを何も変えない。
2. 返った claim の SHA を、リポジトリ、Issue、UUID、ref、base、時刻とともに、ref の作成を試みる**前に**保存する。この SHA は、claim の期間中ずっと変えない。作業の commit は、別の feature branch に置く。
3. `ref` を claim ref の完全な名前、`sha` を保存した claim の SHA にして、`/git/refs` に POST する。新規の作成に成功すれば、claim を取得したことになる。エラーや応答の消失があれば、`/git/ref/heads/agent-claims/issue-N` を GET する。自分が保存した SHA なら、先の要求が成功している。別の SHA なら使用中である。ref が無ければ、再試行の前に実際のエラーを分類する。すべての 422 を競合と決めつけたり、既存の ref を上書きしたりしない。
4. 着手の条件と ref の所有を確かめ直してから、所有者・セッション・ref・SHA・目的を子に記録する。以後の外部への書き込みやソースの編集の前にも確かめ直す。別の所有者の claim は、古さに関係なくそのまま残す。放置された claim を回復するには、タイムアウトによる推測ではなく、明示的に認可された、所有権を解決するための別の操作を使う。
5. 公開を確かめた後、または途中までの handoff を記録した後に、保存した正確な SHA を使って、自分の ref を条件付きで削除する。

   ```sh
   git push --force-with-lease=refs/heads/agent-claims/issue-N:CLAIM_SHA origin :refs/heads/agent-claims/issue-N
   ```

   Issue 番号と SHA は確かめた値に置き換え、`origin` が claim したリポジトリであることを確かめる。remote の ref を再び取得して、無くなったことを確かめる。lease は、入れ替わった別の所有者の ref を削除することを防ぐ。無条件の `gh api --method DELETE`、ref の force-update、別のセッションへの ref の譲渡はしない。受け手は、解放の後に新しく claim を取得する。

同時に動く子のセッションは、それぞれ自分の子のコメントを更新する。親の管理対象の節は、1 つの coordinator が、取得し直した子の情報から更新する。複数の coordinator が同時に書き込む可能性がある場合は、親の Issue 番号に対して同じ claim のプロトコルを使い、その作業を 1 つずつ順に行う。競合に負けた後に、別のセッションのロックを解放したり、その assignee やラベルを元に戻したりしない。

参考: [Create commit](https://docs.github.com/en/rest/git/commits#create-a-commit)、[Create reference](https://docs.github.com/en/rest/git/refs#create-a-reference)、[Git push leases](https://git-scm.com/docs/git-push#Documentation/git-push.txt---force-with-lease)。
