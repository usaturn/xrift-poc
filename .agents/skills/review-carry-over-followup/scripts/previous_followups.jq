# 前回の事後調査コメントを見つけ、判断の記録の候補を「調査済み」「要確認」「未調査」に分ける。
#
#   jq -n --slurpfile comments <comments.json> --slurpfile state <fold.json> -f previous_followups.jq
#
# comments.json: gh api --paginate --slurp "repos/<repo>/issues/<PR>/comments" の出力（平らな配列も可）
# fold.json:     同じコメントを review_records.py fold に通した出力
#
# 前回の事後調査: 信頼する投稿者（review_records.py の TRUSTED_ASSOCIATIONS と同じ）のコメントのうち、
# 1 行目がマーカーか「## 持ち越し候補の再調査」「## 持ち越しの候補の再調査」で始まるもの。
# 候補: fold の decisions のうち state が skip・hold・unverified のもの。
# status: 判断 ID の完全な形が前回の本文にあれば investigated（調査済み）、@ より前の
# <ファイル>#<ID> だけが一致すれば needs_check（要確認。本文を読んで決める）、無ければ uninvestigated（未調査）。
# comments は一致した前回の事後調査のコメント ID。

# 本文が null・空のコメントは 1 行目を空文字にする（"" | split("\n") は [] になる）
def first_line: (. // "") | gsub("\r\n"; "\n") | split("\n") | .[0] // "";

# 判断 ID を正規表現の中で文字どおりに照合するため、特殊文字をエスケープする
def literal: gsub("(?<c>[\\\\.^$|?*+()\\[\\]{}])"; "\\\(.c)");

# 前後が ID の一部でないときだけ一致させる（L-1 と L-10、opus-5-5#… と claude-opus-5-5#… を区別する）
def mentions($ref): test("(?<![A-Za-z0-9._-])" + ($ref | literal) + "(?![A-Za-z0-9])");

def trusted: .author_association as $association | ["OWNER", "MEMBER", "COLLABORATOR"] | index($association) != null;

[
  $comments[0] | flatten(1)[]
  | select(trusted)
  | (.body | first_line) as $line
  | (if $line == "<!-- review-carry-over-followup:v1 -->" then "marker"
     elif $line | test("^## 持ち越しの?候補の再調査") then "heading"
     else empty end) as $kind
  | {comment_id: .id, url: .html_url, created_at, author: .user.login, kind: $kind, body: (.body // "")}
] as $followups
| {
    followups: [$followups[] | del(.body)],
    candidates: [
      $state[0].decisions[]
      | select(.state == "skip" or .state == "hold" or .state == "unverified")
      | .id as $id
      | [$followups[] | select(.body | mentions($id)) | .comment_id] as $full
      | [$followups[] | select(.body | mentions($id | split("@")[0])) | .comment_id] as $short
      | {
          id: $id,
          state,
          carry_over: (.carry_over // null),
          status: (if $full != [] then "investigated" elif $short != [] then "needs_check" else "uninvestigated" end),
          comments: (if $full != [] then $full else $short end)
        }
    ]
  }
