#!/usr/bin/env bash
# Local check that public branches carry no trace of a private extension branch.
#
#   bash scripts/check_no_extension.sh [rev ...]     # default: main dev
#
# Marker words are kept out of the repository, so the public tree never names an
# extension. Configure them once in your clone (see docs/BRANCHES.md):
#   git config --add healthos.extensionMarker <word>
#
# Per revision it reports every tracked file whose path or content contains a marker
# (case-insensitive), and every file an `eklenti/*` branch adds on top of main that also
# exists in the revision (a copied file under a new name). Set EXTENSION_BASE to compare
# against another branch than main.
#
# Exit 0: clean. Exit 1: a trace was found. Exit 2: no marker configured or bad revision.
# Not part of CI: extension branches and markers exist only in the maintainer's clone.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
if [ "$#" -gt 0 ]; then revs=("$@"); else revs=(main dev); fi
base="${EXTENSION_BASE:-main}"

markers=()
while IFS= read -r word; do [ -n "$word" ] && markers+=("$word"); done \
  < <(git config --get-all healthos.extensionMarker || true)
if [ "${#markers[@]}" -eq 0 ]; then
  echo "no healthos.extensionMarker configured; refusing to report clean" >&2
  exit 2
fi
extensions=()
while IFS= read -r ref; do extensions+=("$ref"); done \
  < <(git for-each-ref --format='%(refname:short)' 'refs/heads/eklenti/*')

found=0
for rev in "${revs[@]}"; do
  git rev-parse --verify --quiet "$rev^{commit}" >/dev/null || {
    echo "unknown revision: $rev" >&2
    exit 2
  }
  for word in "${markers[@]}"; do
    if git grep -il -F -e "$word" "$rev" --; then found=1; fi
    while IFS= read -r path; do
      echo "$rev:$path (path)"
      found=1
    done < <(git ls-tree -r --name-only "$rev" | grep -iF -e "$word" || true)
  done
  for ext in "${extensions[@]+"${extensions[@]}"}"; do
    while IFS= read -r path; do
      [ -z "$path" ] && continue
      if git cat-file -e "$rev:$path" 2>/dev/null; then
        echo "$rev:$path (added by $ext)"
        found=1
      fi
    done < <(git diff --diff-filter=A --name-only "$base...$ext")
  done
done

if [ "$found" -ne 0 ]; then
  echo "extension traces found; move them back to their eklenti/* branch" >&2
  exit 1
fi
echo "clean: ${revs[*]}"
