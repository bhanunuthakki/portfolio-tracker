#!/usr/bin/env bash
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
fixture_root=$(mktemp -d "${TMPDIR:-/tmp}/portfolio-public-boundary.XXXXXX")
trap 'rm -rf "$fixture_root"' EXIT

git -C "$fixture_root" init -q
mkdir -p "$fixture_root/scripts"
cp "$repo_root/scripts/check_public_tree.sh" "$fixture_root/scripts/check_public_tree.sh"
printf 'synthetic public fixture\n' > "$fixture_root/README.md"
git -C "$fixture_root" add -f README.md scripts/check_public_tree.sh
(cd "$fixture_root" && bash scripts/check_public_tree.sh)

printf '/%s/%s/private\n' Users example > "$fixture_root/operator-notes.md"
git -C "$fixture_root" add -f operator-notes.md
if (cd "$fixture_root" && bash scripts/check_public_tree.sh >/dev/null 2>&1); then
  echo "guard accepted a personal home path" >&2
  exit 1
fi

git -C "$fixture_root" rm -q -f operator-notes.md
printf '%s@%s\n' bhanu example.com > "$fixture_root/contact.md"
git -C "$fixture_root" add -f contact.md
if (cd "$fixture_root" && bash scripts/check_public_tree.sh >/dev/null 2>&1); then
  echo "guard accepted a personal email" >&2
  exit 1
fi

git -C "$fixture_root" rm -q -f contact.md
mkdir -p "$fixture_root/reports"
printf 'synthetic private export\n' > "$fixture_root/reports/holdings.csv"
git -C "$fixture_root" add -f reports/holdings.csv
if (cd "$fixture_root" && bash scripts/check_public_tree.sh >/dev/null 2>&1); then
  echo "guard accepted a private report path" >&2
  exit 1
fi

git -C "$fixture_root" rm -q -f reports/holdings.csv
printf 'api_key=%s\n' 'ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA' > "$fixture_root/private.txt"
git -C "$fixture_root" add -f private.txt
if (cd "$fixture_root" && bash scripts/check_public_tree.sh >/dev/null 2>&1); then
  echo "guard accepted credential material" >&2
  exit 1
fi

git -C "$fixture_root" rm -q -f private.txt
printf 'my portfolio cost basis: $1234\n' > "$fixture_root/private.txt"
git -C "$fixture_root" add -f private.txt
if (cd "$fixture_root" && bash scripts/check_public_tree.sh >/dev/null 2>&1); then
  echo "guard accepted a personal account fact" >&2
  exit 1
fi

git -C "$fixture_root" rm -q -f private.txt
printf 'password="UltraSecretValue123"\n' > "$fixture_root/private.txt"
git -C "$fixture_root" add -f private.txt
if (cd "$fixture_root" && bash scripts/check_public_tree.sh >/dev/null 2>&1); then
  echo "guard accepted a quoted generic credential" >&2
  exit 1
fi

git -C "$fixture_root" rm -q -f private.txt
printf '{"cost_basis":1234}\n' > "$fixture_root/private.json"
git -C "$fixture_root" add -f private.json
if (cd "$fixture_root" && bash scripts/check_public_tree.sh >/dev/null 2>&1); then
  echo "guard accepted a standalone cost basis" >&2
  exit 1
fi

git -C "$fixture_root" rm -q -f private.json
printf '{"shares":250}\n' > "$fixture_root/private.json"
git -C "$fixture_root" add -f private.json
if (cd "$fixture_root" && bash scripts/check_public_tree.sh >/dev/null 2>&1); then
  echo "guard accepted a standalone share quantity" >&2
  exit 1
fi

git -C "$fixture_root" rm -q -f private.json
printf '{"weight":0.08,"position_size":0.08}\n' > "$fixture_root/public.json"
git -C "$fixture_root" add -f public.json
(cd "$fixture_root" && bash scripts/check_public_tree.sh)

printf 'opaque bytes\n' > "$fixture_root/private.xlsx"
git -C "$fixture_root" add -f private.xlsx
if (cd "$fixture_root" && bash scripts/check_public_tree.sh >/dev/null 2>&1); then
  echo "guard accepted an uninspected binary artifact" >&2
  exit 1
fi
