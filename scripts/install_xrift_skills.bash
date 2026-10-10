#!/bin/bash
#
# XRift 公式スキル（WebXR-JP/xrift-skills）を、コミットを固定した URL から、リポジトリの
# ルートの .agents/skills/ と .claude/skills/ に導入する（usaturn/xrift-poc#1）。
# 導入したパスと skills-lock.json は Git で無視する（.gitignore）。
#
# 更新するとき: XRIFT_SKILLS_REF を新しいコミットに変えてこのスクリプトを実行し、
# README の「XRift スキルの更新」の手順に従って確認し直してから PR にする。
set -euo pipefail

readonly SKILLS_CLI_VERSION="1.7.2"
readonly XRIFT_SKILLS_REPOSITORY="WebXR-JP/xrift-skills"
readonly XRIFT_SKILLS_REF="41d65040e8c2a5313991acddf93406888fc84326"
# 少なくとも導入されているべきスキル。上流で増えたスキルも、Git で無視されていれば受け入れる。
readonly REQUIRED_SKILLS=(xrift-world xrift-sdk)

fail() {
    printf '[install_xrift_skills] ERROR: %s\n' "$*" >&2
    exit 1
}

root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)" || fail "cannot resolve the repository root"
cd -- "$root"

npx -y "skills@${SKILLS_CLI_VERSION}" add \
    "https://github.com/${XRIFT_SKILLS_REPOSITORY}/tree/${XRIFT_SKILLS_REF}" \
    -y -a claude-code -a codex -a pi </dev/null \
    || fail "skills add failed for ${XRIFT_SKILLS_REPOSITORY} at ${XRIFT_SKILLS_REF}"

names="$(python3 - "$XRIFT_SKILLS_REPOSITORY" "$XRIFT_SKILLS_REF" "${REQUIRED_SKILLS[@]}" <<'PY'
import json
import sys
from pathlib import Path

repository, ref, *required = sys.argv[1:]
lock = Path("skills-lock.json")
try:
    data = json.loads(lock.read_text(encoding="utf-8"))
except (OSError, ValueError) as error:
    sys.exit(f"cannot read {lock}: {error}")
skills = data.get("skills") if isinstance(data, dict) else None
if not isinstance(skills, dict):
    sys.exit(f"{lock} has no skills table")
ours = {name: entry for name, entry in skills.items() if isinstance(entry, dict) and entry.get("source") == repository}
missing = [name for name in required if name not in ours]
if missing:
    sys.exit(f"missing skills in {lock}: {', '.join(missing)}")
wrong = sorted(name for name, entry in ours.items() if entry.get("ref") != ref)
if wrong:
    sys.exit(f"skills not at {ref}: {', '.join(wrong)}")
print("\n".join(sorted(ours)))
PY
)" || fail "the installed XRift skills do not match ${XRIFT_SKILLS_REPOSITORY} at ${XRIFT_SKILLS_REF}"

while IFS= read -r name; do
    [ -f ".agents/skills/${name}/SKILL.md" ] || fail ".agents/skills/${name}/SKILL.md is missing"
    [ -L ".claude/skills/${name}" ] \
        && [ "$(readlink -f ".claude/skills/${name}")" = "$(readlink -f ".agents/skills/${name}")" ] \
        || fail ".claude/skills/${name} does not link to .agents/skills/${name}"
    git check-ignore -q ".agents/skills/${name}/SKILL.md" \
        || fail ".agents/skills/${name} is not ignored by git; add it to .gitignore"
    git check-ignore -q ".claude/skills/${name}" \
        || fail ".claude/skills/${name} is not ignored by git; add it to .gitignore"
done <<<"$names"
git check-ignore -q skills-lock.json || fail "skills-lock.json is not ignored by git"
printf '[install_xrift_skills] installed %s at %s\n' "$(tr '\n' ' ' <<<"$names")" "$XRIFT_SKILLS_REF"
