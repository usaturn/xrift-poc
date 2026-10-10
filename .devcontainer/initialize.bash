#!/bin/bash
set -e

# Create required directories
mkdir -p \
  "${HOME}/.codex" \
  "${HOME}/.claude" \
  "${HOME}/.grok" \
  "${HOME}/.config/gh" \
  "${HOME}/.pi" \
  "${HOME}/.dsh" \
  "${HOME}/.cursor" \
  "${HOME}/.config/gcloud" \
  "${HOME}/.local/share/devin" \
  "${HOME}/.config/devin" \
  "${HOME}/.config/cursor" \
  "${HOME}/.local/share/cursor-agent"

# Create required files
touch \
  "${HOME}/.claude.json"
