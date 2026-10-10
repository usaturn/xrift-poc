# 過去の PR の再生評価の手順

レビューの工程（探す・確かめる・決める・修正の確認）を、過去の PR の HEAD や修正の組（`from..to`）で再生して評価するときの手順である（Issue #620）。評価担当が、入力として渡したもの以外から答え（後のサイクルで見つかった欠陥や、期待する検出）を得られないようにする。架空のパケット（`verify-packet.md` など）による行動確認は対象外である。

この文書は `evals/` にあるので、2 章の隔離で tool root から除かれ、評価担当には見えない。評価を進める側（controller）が読む。

## 1. 汚染の経路

| 経路 | 例 | 防ぐ章 |
| --- | --- | --- |
| リポジトリの道具・文書 | `evals/validation.md` が組ごとの期待する検出を書いている。スキルが読む文書の記入例が、再生する PR の指摘から作られている | 2 章（隔離）、3 章（文書の確認） |
| `to` より後の履歴 | 後の commit が修正そのものを含む | 2 章（隔離） |
| ランタイムが読み込む利用者の記憶 | Claude Code の自動メモリの索引（`MEMORY.md`）に、再生する PR の結論が書かれている。索引は Agent ツールのサブエージェントにも渡る | 3 章（記憶の確認） |

## 2. 隔離

親の main root で、評価の領域を 2 つ作る。`EVAL` は評価担当に渡す領域、`ANSWERS` は正解集とパターンのファイルを置く領域で、評価担当に渡すパスの外にする。

```text
uv run --no-project python .agents/scripts/agent_tmp.py mkdir <topic>-eval      # EVAL
uv run --no-project python .agents/scripts/agent_tmp.py mkdir <topic>-answers   # ANSWERS
```

次の環境変数を設定し、ブロックを bash で実行する（`bash -euo pipefail` で、どのコマンドが失敗しても先へ進まない）。

| 変数 | 内容 |
| --- | --- |
| `PARENT` | 親の main root（履歴の取り出し元。shallow でもよい） |
| `TOOL_HEAD` | 評価する道具・スキルの commit（feature branch の HEAD など） |
| `TO` | 再生する組の `to`（探す・確かめる工程の再生では、そのサイクルの HEAD）。短縮した SHA でもよい |
| `EVAL` | 上で作った評価の領域 |

```bash
# review-verify replay: isolate
TO="$(git -C "${PARENT:?}" rev-parse --verify "${TO:?}^{commit}")"
mkdir -- "${EVAL:?}/toolroot"
git -C "$PARENT" archive --output="$EVAL/toolroot.tar" "${TOOL_HEAD:?}"
tar -x -f "$EVAL/toolroot.tar" -C "$EVAL/toolroot"
rm -f -- "${EVAL:?}/toolroot.tar"
find "${EVAL:?}/toolroot" -type d -name evals -prune -exec rm -rf -- {} +
git init -q -- "$EVAL/repo"
git -C "$EVAL/repo" fetch -q --update-shallow "$PARENT" "$TO:refs/heads/eval-to"
git -C "$EVAL/repo" checkout -q --detach eval-to
```

- tool root（`$EVAL/toolroot`）: `TOOL_HEAD` のコミット済みの内容から、**すべてのスキルの `evals/` ディレクトリ**を除いたもの。review-verify 以外の `evals/validation.md` にも過去の PR の結論がある。dispatch などの道具はここから起動する（`uv sync --project "$EVAL/toolroot/tools/devenv" --locked` の後、`$EVAL/toolroot/.agents/skills/review-verify/scripts/dispatch.py`）。
- 最小の repository（`$EVAL/repo`）: `to` までの履歴だけを持ち、`to` を detached で checkout する。request の `repository` にこのパスを渡す。checkout しない（unborn HEAD のまま）と、runner の checkout の監査が働かず、`outside_changes` に repository が出て `accept` が受け取らない。
- 正解集は `ANSWERS` に置き、再生の前後で `sha256sum` の値を比べる。

## 3. 記憶と文書の確認

再生の前に、評価担当の文脈に入りうる記憶と文書に、再生する PR の結論が無いかを確かめる。複数回の再生では、**各回の直前に**やり直す。再生の途中で評価の結果をメモリに書かない（次の回の Claude Code が読む）。

`$ANSWERS/answer-patterns.txt` に、結論を表す固定文字列を 1 行に 1 つ書く。

- 再生する PR・Issue の番号（`#<番号>`）
- 後の commit の SHA の先頭 7 文字
- 期待する検出の位置（`<path>:<line>`）
- 指摘を特徴づける語（設定名・関数名など）

```bash
# review-verify replay: context check
for name in ANSWERS EVAL HOME; do
  if [[ -z "${!name:-}" ]]; then echo "$name is not set" >&2; exit 2; fi
done
if [[ ! -d "$EVAL/toolroot" ]]; then echo "$EVAL/toolroot is not a directory; run the isolate block first" >&2; exit 2; fi
patterns="$(sed -e 's/\r$//' -e '/^[[:space:]]*$/d' -- "$ANSWERS/answer-patterns.txt")"
if [[ -z "$patterns" ]]; then echo 'answer-patterns.txt has no patterns' >&2; exit 2; fi
targets=("$EVAL/toolroot")
for path in "$HOME/.claude/CLAUDE.md" "$HOME"/.claude/projects/*/memory; do
  if [[ -e "$path" ]]; then targets+=("$path"); fi
done
grep -r -n -i -I -F -e "$patterns" -- "${targets[@]}"
```

- 終了コード: 0 は当たりあり（当たった行を出力する）、1 は当たりなし、2 は誤り（`ANSWERS`・`EVAL`・`HOME` のどれかが未設定か空、tool root が無い、パターンが無い、読めないなど）。変数と tool root が欠けているときは、検索せずに 2 で止まる。
- 検索する対象: tool root と、Claude Code の利用者の指示（`~/.claude/CLAUDE.md`）・自動メモリ（`~/.claude/projects/*/memory`）のうち存在するもの。tool root は必須で、無ければ 2 章の隔離を先に実行するよう標準エラーに出して止まる。Claude Code が読み込むのは作業ディレクトリに対応する 1 つのメモリだけだが、どれが読まれるかを推測せず、すべてを検索する。
- 空行と行末の CR は除く。空のパターンは全行に当たるためである。

当たりを読み、次のどれかに分ける。

| 当たり | 扱い |
| --- | --- |
| 結論ではない一致（番号の偶然の一致など） | 当たった行と判断を記録して続ける |
| tool root の文書に結論がある（記入例など） | 具体例を伏せたコピーに差し替えた tool root で再生する。どのランタイムでも同じ |
| 記憶に結論がある | その記憶を読み込むランタイムの結果を採点に使わない。記憶を持たないランタイム（Pi など）で再生する。Claude Code の結果も残すなら、汚染の可能性を記録し、比較にだけ使う |

記憶を読み込むランタイム: この環境の Claude Code である。Agent ツールのサブエージェントは親のセッションの索引を受け取る（#547 段階3 で確かめた）。agent_runner から起動する `claude` も利用者の HOME で動き、作業ディレクトリに対応するメモリを読みうるので、読み込むものとして扱う。

当たりが無ければ Claude Code でも再生できる。パターンは言い換えた書き方に当たらないので、確認の範囲（パターンの種類と数、検索した対象）を記録する。

## 4. 実行後の確認

- 正解集の `sha256sum` が再生の前と同じであること。
- runner の結果の `outside_changes` が空であること。
- 評価担当の記録に、答えの資料を読んだ跡が無いこと。`$ANSWERS/leak-patterns.txt` に、正解集とパターンのディレクトリの名前、`evals/`、後の commit の SHA、`.claude/projects` を 1 行に 1 つ書き、`RUN_DIR`（dispatch が出力する `artifact_dir`）を検索する。

```bash
# review-verify replay: leak scan
for name in ANSWERS RUN_DIR; do
  if [[ -z "${!name:-}" ]]; then echo "$name is not set" >&2; exit 2; fi
done
patterns="$(sed -e 's/\r$//' -e '/^[[:space:]]*$/d' -- "$ANSWERS/leak-patterns.txt")"
if [[ -z "$patterns" ]]; then echo 'leak-patterns.txt has no patterns' >&2; exit 2; fi
grep -r -n -I -F -e "$patterns" -- "$RUN_DIR"
```

終了コードは 3 章の確認と同じ（`ANSWERS`・`RUN_DIR` のどれかが未設定か空なら、検索せずに 2）。当たりがあれば、その行を読み、答えの資料を読んだと判断した実行は採点から外して原因を記録する。

## 5. 記録する項目

評価の記録（`evals/` の記録のファイル）に、次を書く。

- 隔離: `TOOL_HEAD`、`TO`（完全な SHA）、除いたディレクトリ（`evals/`）、差し替えた文書があればその内容
- 記憶と文書の確認: パターンの種類と数、検索した対象、結果（当たりの有無、当たった場所と判断）
- 採点に使ったランタイム、記憶を持たないランタイムとの併用の有無と理由
- 実行後の確認: 正解集のハッシュの比較、`outside_changes`、記録の検索の結果

## 6. 限界

- パターンに当たらない書き方（言い換え、要約）の記憶は見つけられない。迷ったら、記憶を持たないランタイムでも再生する。
- `rg` は既定で `.agents` などの隠しディレクトリを読まないので、手順では `grep -r` を使う。
- tool root は `.git` を持たないので、git を前提にする検証（`verify-devenv.sh` の一部のテストなど）はそこでは失敗する。
- 正解集をファイルシステムに置いている限り、評価担当が読まなかったことは、指示と 4 章の検索でしか確かめられない。
