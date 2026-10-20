# 範囲外の不具合の記録のコメント（.agents/out-of-scope-defects.md）を取り出す。
#
#   jq -n --slurpfile comments <comments.json> -f defect_records.jq
#
# comments.json: gh api --paginate --slurp "repos/<repo>/issues/<PR>/comments" の出力（平らな配列も可）
#
# 記録のコメント: 信頼する投稿者（review_records.py の TRUSTED_ASSOCIATIONS と同じ）のコメントのうち、
# 1 行目がマーカー <!-- out-of-scope-defects:v1 --> のもの。項目を引用しただけのコメントや、
# 信頼しない投稿者が同じ形で書いたコメントは読まない。本文はそのまま返す（項目は人が読む）。

# 本文が null・空のコメントは 1 行目を空文字にする（previous_followups.jq と同じ）
def first_line: (. // "") | gsub("\r\n"; "\n") | split("\n") | .[0] // "";

def trusted: .author_association as $association | ["OWNER", "MEMBER", "COLLABORATOR"] | index($association) != null;

[
  $comments[0] | flatten(1)[]
  | select(trusted and (.body | first_line) == "<!-- out-of-scope-defects:v1 -->")
  | {comment_id: .id, url: .html_url, created_at, author: .user.login, body}
]
