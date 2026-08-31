#!/usr/bin/env bash
set -euo pipefail

# Deterministic pre-commit guard for this public repository.
if git grep -I -l -E '(/Users/|/home/|[A-Za-z]:\\Users\\)' -- ':!AGENTS.md' ':!scripts/check_public_tree.sh'; then
  echo "Personal absolute path found in tracked content" >&2
  exit 1
fi
if git ls-files | grep -E '(^|/)(credentials\.json|token\.json|.*\.db|.*\.sqlite|.*\.pem|.*\.key)$'; then
  echo "Credential or local-data artifact is tracked" >&2
  exit 1
fi
if git ls-files | grep -E '(^|/)(exports|reports|imports|snapshots)/|\.jsonl$'; then
  echo "Private import, export, report, or snapshot material is tracked" >&2
  exit 1
fi
