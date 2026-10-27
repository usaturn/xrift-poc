#!/bin/bash
set -e

# Create required directories
mkdir -p \
  "${HOME}/.codex" \
  "${HOME}/.claude" \
  "${HOME}/.config/gh" \
  "${HOME}/.pi" \
  "${HOME}/.cursor" \
  "${HOME}/.config/gcloud" \
  "${HOME}/.local/share/devin" \
  "${HOME}/.config/devin" \
  "${HOME}/.config/cursor" \
  "${HOME}/.local/share/cursor-agent"

# Create required files
touch \
  "${HOME}/.claude.json"

# XRift CLI の認証情報（~/.xrift/config.json）の保存先。bind mount の元になる。
# 新しく作るときだけ 700 にし、既存のディレクトリの権限と中身は変えない。
if [ ! -e "${HOME}/.xrift" ] && [ ! -L "${HOME}/.xrift" ]; then
  mkdir -m 700 "${HOME}/.xrift"
elif [ ! -d "${HOME}/.xrift" ] || [ -L "${HOME}/.xrift" ]; then
  echo "initialize: ${HOME}/.xrift exists but is a symlink or not a directory; leaving it unchanged." >&2
  exit 1
fi
