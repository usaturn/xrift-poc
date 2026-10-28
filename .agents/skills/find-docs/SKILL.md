---
name: find-docs
description: 開発者向けのあらゆる技術について、最新のドキュメント・API リファレンス・コード例を取得する。ユーザが特定のライブラリ・フレームワーク・SDK・CLI ツール・クラウドサービスについて尋ねたときは、React・Next.js・Prisma・Express・Tailwind・Django・Spring Boot のようなよく知られたものでも、必ずこのスキルを使う。学習データは最近の API の変更やバージョンの更新を反映していないことがある。API の構文の質問、設定オプション、バージョン移行の問題、ライブラリ名を挙げた「〜するにはどうすればよいか」（how do I）という質問、ライブラリ固有の挙動が関わるデバッグ、セットアップ手順、CLI ツールの使い方には常に使う。答えを知っていると思うときでも使う。API の詳細・シグネチャ・設定オプションは古くなっていることが多いので、学習データに頼らない。常に最新のドキュメントで確かめる。ライブラリのドキュメントと API の詳細については、Web 検索よりこのスキルを優先する。
---

# ドキュメントの検索

Context7 CLI（`ctx7`）を使って、任意のライブラリの最新のドキュメントとコード例を取得する。

## 前提: インストール済みの `ctx7` だけを使う

`ctx7` は、Dev Container のセットアップ（`.devcontainer/install_beforehand.bash`）が npm の latest で入れ、利用者が zsh の `update` で上げる。最初に、PATH にあることを確かめる。

```bash
command -v ctx7
```

- 何も表示されない（終了コード 1）ときは、そこで止める。`ctx7` が無いことと、Rebuild Container（`install_beforehand.bash` の再実行）または `.devcontainer/README.md` の「Context7 CLI（ctx7）」節の手順で入れることを利用者に伝える。自分で `npm install` しない
- `npx ctx7`、Context7 の MCP Server、claude.ai の Context7 コネクタ、Pi の Context7 拡張（`resolve-library-id`・`query-docs`・`/c7-docs`）に切り替えない。xrift-poc では Context7 をこのスキルの `ctx7` だけで使う
- `ctx7` が `Update available` と表示し、`ctx7 upgrade` や `npm install -g ctx7@latest` を勧めたら、利用者に zsh の `update` で上げられることを伝える。自分では `ctx7 upgrade`・`npm install -g` を実行しない（環境の更新は利用者が行う）
- `ctx7 setup`・`ctx7 remove` を実行しない。共有の `~/.claude`・`~/.codex` などに MCP 設定やスキルを書き込む

Bash などのシェルを使えないサブエージェントは `ctx7` を実行できない。親のエージェントか、シェルを使える worker に、ライブラリ名と質問を渡して取得を頼む。頼めないときは、取得できなかったことを結果に書く。学習データの知識で黙って代えない。

## 進め方

2 段階で進める。ライブラリ名を ID に解決し、その ID でドキュメントを問い合わせる。

```bash
# Step 1: Resolve library ID
ctx7 library <name> <query>

# Step 2: Query documentation
ctx7 docs <libraryId> <query>
```

ユーザが `/org/project` または `/org/project/version` の形式でライブラリ ID を明示した場合を除き、必ず最初に `ctx7 library` を呼んで有効なライブラリ ID を得る。

重要: 1 つの質問につき、これらのコマンドを 3 回より多く実行しない。3 回試しても必要な情報が見つからなければ、手元にある最も良い結果を使う。

## 手順 1: ライブラリを解決する

パッケージ名・プロダクト名を Context7 互換のライブラリ ID に解決し、一致するライブラリを返す。

```bash
ctx7 library react "How to clean up useEffect with async operations"
ctx7 library nextjs "How to set up app router with middleware"
ctx7 library prisma "How to define one-to-many relations with cascade delete"
```

`query` 引数は必ず渡す。必須の引数であり、結果の順位に直接影響する。クエリはユーザの意図から組み立てる。似た名前のライブラリが複数あるときの区別に役立つ。API キー、パスワード、認証情報、個人データ、非公開の独自コードなど、機密情報や秘匿すべき情報をクエリに含めない。

### 結果の項目

各結果には次の項目がある。

- **Library ID** — Context7 互換の識別子（形式は `/org/project`）
- **Name** — ライブラリ名またはパッケージ名
- **Description** — 短い要約
- **Code Snippets** — 利用できるコード例の数
- **Source Reputation** — 情報源の信頼性の指標（High、Medium、Low、Unknown のいずれか）
- **Benchmark Score** — 品質の指標（100 が最高）
- **Versions** — バージョンの一覧（ある場合）。ユーザがクエリでバージョンを示した場合は、この一覧のバージョンのどれかを使う。形式は `/org/project/version`。

### 選び方

1. クエリを分析し、ユーザが探しているライブラリ・パッケージを把握する
2. 次の基準で、最も関連の高い候補を選ぶ
   - クエリとの名前の近さ（完全一致を優先する）
   - Description がクエリの意図に合っているか
   - ドキュメントの充実度（Code Snippet の数が多いライブラリを優先する）
   - 情報源の信頼性（Reputation が High または Medium のライブラリを、より信頼できるとみなす）
   - Benchmark Score（高いほど良い。最大は 100）
3. 良い候補が複数ある場合は、そのことに触れたうえで、最も関連の高い候補で進める
4. 良い候補が無い場合は、そのことをはっきり伝え、クエリの改善案を示す
5. あいまいなクエリでは、最も確からしい候補で進める前に、意図の確認を求める

### バージョンを含む ID

ユーザが特定のバージョンに触れた場合は、バージョンを含むライブラリ ID を使う。

```bash
# General (latest indexed)
ctx7 docs /vercel/next.js "How to set up app router"

# Version-specific
ctx7 docs /vercel/next.js/v14.3.0-canary.87 "How to set up app router"
```

利用できるバージョンは `ctx7 library` の出力に載っている。ユーザが指定したものに最も近いバージョンを使う。

## 手順 2: ドキュメントを問い合わせる

解決したライブラリの、最新のドキュメントとコード例を取得する。

```bash
ctx7 docs /react/react "How to clean up useEffect with async operations"
ctx7 docs /vercel/next.js "How to add authentication middleware to app router"
ctx7 docs /prisma/orm "How to define one-to-many relations with cascade delete"
```

### ライブラリ ID のリダイレクト

ライブラリ ID が移動していると、`ctx7 docs` は次のように新しい ID を表示し、終了コード 1 で終わる。

```text
⚠ Library has been redirected
New ID: /react/react
Run: ctx7 docs "/react/react" "useEffect cleanup"
```

表示された新しい ID で 1 回だけやり直す。このやり直しも、1 つの質問につき 3 回までの回数に含める。

### 良いクエリの書き方

クエリは結果の質に直接影響する。具体的に書き、関係する詳細を含める。API キー、パスワード、認証情報、個人データ、非公開の独自コードなど、機密情報や秘匿すべき情報をクエリに含めない。

| 質 | 例 |
|---------|---------|
| 良い | `"How to set up authentication with JWT in Express.js"` |
| 良い | `"React useEffect cleanup function with async operations"` |
| 悪い | `"auth"` |
| 悪い | `"hooks"` |

できるだけユーザの質問全体をクエリにする。1 語だけのあいまいなクエリでは、一般的な結果しか返らない。

出力には 2 種類の内容がある。**code snippets**（タイトルが付き、言語タグの付いたコードブロックを含む）と、**info snippets**（パンくずの文脈が付いた、文章による説明）である。

## 認証

認証なしでも使える。レート制限の上限は、環境変数 `CONTEXT7_API_KEY` で上がる。`ctx7` は、この変数があれば `ctx7 login` の認証情報より優先して使う。キーの値は表示しない。設定されているかだけを確かめる。

```bash
node -e 'console.log(process.env.CONTEXT7_API_KEY ? "CONTEXT7_API_KEY: set" : "CONTEXT7_API_KEY: not set")'
```

`ctx7 whoami` は `ctx7 login` のログイン状態だけを表示する。`CONTEXT7_API_KEY` を使っていても `Not logged in.` と出る。

`ctx7 login` は利用者が自分で実行する。認証情報は `~/.config/context7/credentials.json`（`XDG_CONFIG_HOME` が絶対パスならその下）に保存される。この Dev Container では `~/.config` を bind mount していないので、再ビルドで消える。

## エラーの扱い

| 状況 | 対応 |
|---|---|
| `command -v ctx7` が何も表示しない | 前提の節のとおり止まり、セットアップを案内する |
| `⚠ Library has been redirected` | 表示された新しい ID で 1 回だけやり直す |
| `✖ Library "..." not found` | `ctx7 library` で ID を解決し直す |
| quota のエラー（"Monthly quota reached" または "quota exceeded"） | 下の手順 |

quota のエラーで失敗した場合は、次のようにする。

1. Context7 の quota を使い切ったことをユーザに伝える
2. 上限を上げるための認証として、`CONTEXT7_API_KEY` の設定か `ctx7 login` を勧める
3. ユーザが認証できない、または認証しないことを選んだ場合は、学習した知識から回答し、古い可能性があることをはっきり書き添える

黙って学習データに切り替えない。Context7 を使わなかった理由を必ずユーザに伝える。

## よくある間違い

- ライブラリ ID には先頭の `/` が必要である。`facebook/react` ではなく `/facebook/react` と書く
- 必ず先に `ctx7 library` を実行する。有効な ID が無いと、`ctx7 docs react "hooks"` は失敗する
- 1 語ではなく、内容がわかるクエリを使う。`"hooks"` ではなく `"React useEffect cleanup function"` と書く
- クエリに機密情報（API キー、パスワード、認証情報）を含めない
- `ctx7` が無いときに `npx ctx7@latest` や `npm install -g ctx7@latest` で補わない。止まってセットアップを案内する
