# notes_tool

レビュアー（エージェント）が書いた指摘を、1 件ずつ JSON ファイルに保存する小さな CLI。

## 使い方

```bash
python -m notes_tool.cli add --store <保存先のディレクトリ> --id H-1 --content-file finding.md
```

`finding.md` には指摘の本文を書く。本文は複数行の Markdown でよい（位置・内容・根拠・推奨対応を行ごとに書く）。
