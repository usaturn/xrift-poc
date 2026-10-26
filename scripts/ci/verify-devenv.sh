#!/usr/bin/env bash
set -euo pipefail
if (($# != 0)); then
    printf 'usage: %s\n' "$0" >&2
    exit 64
fi
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
project="$root/tools/devenv"
for required in "$project/pyproject.toml" "$project/uv.lock" "$root/tools/devenv/tests" "$root/tools/devenv_migration/tests" "$root/tools/agent_runner/tests" "$root/tools/test_support/tests" "$root/.agents"; do
    if [[ ! -e "$required" ]]; then
        printf 'missing: %s\n' "$required" >&2
        exit 66
    fi
done
cd -- "$root"
uv sync --project "$project" --locked
uv run --project "$project" --no-sync ruff check --config "$project/pyproject.toml" tools/devenv tools/devenv_migration tools/agent_runner tools/test_support .agents scripts/write_shell_block.py
uv run --project "$project" --no-sync ruff format --check --config "$project/pyproject.toml" tools/devenv tools/devenv_migration tools/agent_runner tools/test_support .agents scripts/write_shell_block.py
uv run --project "$project" --no-sync pyright --project "$project/pyproject.toml"
uv run --project "$project" --no-sync pytest -c "$project/pyproject.toml" --rootdir "$root" tools/devenv/tests tools/devenv_migration/tests tools/agent_runner/tests tools/test_support/tests .agents
