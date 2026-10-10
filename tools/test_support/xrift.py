"""XRift のツールの導入を、ネットワーク無しで真似る fake。"""

from typing import Final

# `npx -y skills@<v> add https://github.com/<repo>/tree/<ref> ...` を受けて、skills CLI 1.7.2 と同じ配置を作る
# （2026-10-10 に実物で確かめた形）。FAKE_SKILLS_MODE で失敗の形を選ぶ。
SKILLS_NPX_FAKE: Final[str] = r"""#!/bin/bash
case " $* " in
*" add "*) ;;
*) exit 0 ;;
esac
mode="${FAKE_SKILLS_MODE:-ok}"
[ "$mode" = "fail" ] && { echo "fake skills: network error" >&2; exit 1; }
url=""
for arg in "$@"; do
    case "$arg" in https://github.com/*/tree/*) url="$arg" ;; esac
done
[ -n "$url" ] || exit 0  # 固定した URL を伴わない add（共有スキルの導入など）は何もしない
ref="${url##*/tree/}"
repo="${url#https://github.com/}"
repo="${repo%/tree/*}"
[ "$mode" = "wrong-ref" ] && ref="0000000000000000000000000000000000000000"
names="xrift-world xrift-sdk"
[ "$mode" = "missing" ] && names="xrift-world"
[ "$mode" = "unignored" ] && names="xrift-world xrift-sdk other-skill"
mkdir -p .agents/skills .claude/skills
entries=""
for name in $names; do
    mkdir -p ".agents/skills/${name}"
    printf -- '---\nname: %s\ndescription: fake\n---\n' "$name" > ".agents/skills/${name}/SKILL.md"
    ln -sfn "../../.agents/skills/${name}" ".claude/skills/${name}"
    entries="${entries}${entries:+,}\"${name}\":{\"source\":\"${repo}\",\"ref\":\"${ref}\",\"sourceType\":\"github\",\"skillPath\":\"skills/${name}/SKILL.md\",\"computedHash\":\"fake\"}"
done
printf '{"version":1,"skills":{%s}}\n' "$entries" > skills-lock.json
"""
