#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
python_bin=${PYTHON_BIN:-python3}
current_tree=""
cleanup() {
  if [[ -n "$current_tree" ]]; then
    git -C "$repo_root" worktree remove --force "$current_tree" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

git -C "$repo_root" fetch origin '+refs/heads/*:refs/remotes/origin/*' --prune

while IFS= read -r ref; do
  current_tree=$(mktemp -d "${RUNNER_TEMP:-/tmp}/public-ref.XXXXXX")
  git -C "$repo_root" worktree add --detach "$current_tree" "$ref" >/dev/null

  if [[ -f "$repo_root/scripts/check_public_tree.sh" ]]; then
    mkdir -p "$current_tree/scripts"
    cp "$repo_root/scripts/check_public_tree.sh" "$current_tree/scripts/check_public_tree.sh"
    if ! (cd "$current_tree" && bash scripts/check_public_tree.sh >/dev/null 2>&1); then
      echo "Public-boundary violation found on a live branch." >&2
      exit 1
    fi
  elif [[ -f "$repo_root/src/blog_engine/public_boundary.py" ]]; then
    if ! "$python_bin" - "$repo_root/src/blog_engine/public_boundary.py" "$current_tree" <<'PY'
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("public_boundary_guard", sys.argv[1])
if spec is None or spec.loader is None:
    raise SystemExit(2)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
raise SystemExit(1 if module.violations(Path(sys.argv[2])) else 0)
PY
    then
      echo "Public-boundary violation found on a live branch." >&2
      exit 1
    fi
  elif [[ -f "$repo_root/snippets/check_public_boundary.py" ]]; then
    if ! "$python_bin" - "$repo_root/snippets/check_public_boundary.py" "$current_tree" <<'PY'
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("public_boundary_guard", sys.argv[1])
if spec is None or spec.loader is None:
    raise SystemExit(2)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
raise SystemExit(1 if module.violations(Path(sys.argv[2])) else 0)
PY
    then
      echo "Public-boundary violation found on a live branch." >&2
      exit 1
    fi
  else
    echo "No public-boundary guard is configured." >&2
    exit 2
  fi

  git -C "$repo_root" worktree remove --force "$current_tree" >/dev/null
  current_tree=""
done < <(
  git -C "$repo_root" for-each-ref     --format='%(refname)' refs/remotes/origin     | grep -v '^refs/remotes/origin/HEAD$'
)

echo "All live public branch tips passed the current privacy guard."
