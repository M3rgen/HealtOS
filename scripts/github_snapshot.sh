#!/usr/bin/env bash
# Build the GitHub `main` snapshot: one commit whose tree is a source revision's tree minus
# the paths listed in the local git config, on top of the snapshot branch.
#
#   bash scripts/github_snapshot.sh [options]
#
#   --source <rev>          tree to publish (default: main)
#   --branch <name>         snapshot branch, fast-forwarded to the new commit (default: github-main)
#   --override <path>=<rev> take <path> from <rev> instead of the source tree (repeatable);
#                           a stopgap until the change reaches the source branch
#   -m, --message <text>    commit message (default: "HealthOS: snapshot of <source> (<date>)")
#   --allow-references      update the branch even if the tree still names an excluded file
#   --dry-run               build the tree and print the diff, but create no commit
#
# The working tree and the real index are never touched: the tree is assembled in a
# temporary index. Excluded paths and the commit identity live only in the local clone:
#   git config --add healthos.githubExclude <path>     # once per path, files or directories
#
# A directory (written with or without a trailing slash) excludes everything under it. The
# reference check looks for a file's name and, as a whole word, its name without the extension
# (so `import helper` of an excluded `helper.py` is caught); for a directory it looks for its path.
#   git config healthos.githubAuthorName  <name>
#   git config healthos.githubAuthorEmail <address>
#
# If the snapshot branch is checked out in a worktree, it is updated there with
# `git merge --ff-only`; otherwise its ref is moved after a fast-forward check. Nothing is pushed.
#
# Exit 0: branch updated, or nothing to do. Exit 1: an excluded file is still referenced.
# Exit 2: missing configuration, bad argument or a non-fast-forward update.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

source_rev=main
branch=github-main
message=""
allow_references=0
dry_run=0
overrides=()

die() {
  echo "github_snapshot: $*" >&2
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --source) source_rev="${2:?--source needs a revision}"; shift 2 ;;
    --branch) branch="${2:?--branch needs a name}"; shift 2 ;;
    --override) overrides+=("${2:?--override needs <path>=<rev>}"); shift 2 ;;
    -m | --message) message="${2:?--message needs text}"; shift 2 ;;
    --allow-references) allow_references=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    -h | --help) awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

excludes=()
while IFS= read -r path; do [ -n "$path" ] && excludes+=("$path"); done \
  < <(git config --get-all healthos.githubExclude || true)
[ "${#excludes[@]}" -gt 0 ] || die "no healthos.githubExclude configured; refusing to publish the full tree"

author_name="$(git config --get healthos.githubAuthorName || true)"
author_email="$(git config --get healthos.githubAuthorEmail || true)"
[ -n "$author_name" ] && [ -n "$author_email" ] ||
  die "set healthos.githubAuthorName and healthos.githubAuthorEmail"

source_commit="$(git rev-parse --verify --quiet "$source_rev^{commit}")" || die "unknown revision: $source_rev"
parent="$(git rev-parse --verify --quiet "refs/heads/$branch^{commit}")" || die "unknown branch: $branch"

index="$(mktemp "${TMPDIR:-/tmp}/github-snapshot-index.XXXXXX")"
trap 'rm -f "$index"' EXIT
export GIT_INDEX_FILE="$index"

git read-tree "$source_commit^{tree}"
for path in "${excludes[@]}"; do
  if [ -z "$(git ls-files -- "$path")" ]; then
    echo "note: excluded path not in $source_rev: $path" >&2
    continue
  fi
  git rm --cached -r -f -q -- "$path"
  echo "excluded: $path"
done
for item in "${overrides[@]+"${overrides[@]}"}"; do
  path="${item%=*}"
  rev="${item##*=}"
  [ -n "$path" ] && [ -n "$rev" ] && [ "$path" != "$item" ] || die "bad --override: $item"
  entry="$(git ls-tree "$rev" -- "$path")"
  [ -n "$entry" ] || die "--override: $path not found in $rev"
  mode="${entry%% *}"
  blob="$(git rev-parse "$rev:$path")"
  git update-index --add --cacheinfo "$mode,$blob,$path"
  echo "override: $path from $rev"
done
tree="$(git write-tree)"
unset GIT_INDEX_FILE

referenced=0
for path in "${excludes[@]}"; do
  path="${path%/}"
  if [ "$(git cat-file -t "$source_commit:$path" 2>/dev/null || true)" = tree ]; then
    if git grep -n -F -e "$path" "$tree" --; then referenced=1; fi
    continue
  fi
  name="${path##*/}"
  stem="${name%.*}"
  if git grep -n -F -e "$name" "$tree" --; then referenced=1; fi
  if [ -n "$stem" ] && [ "$stem" != "$name" ] && git grep -n -w -F -e "$stem" "$tree" --; then
    referenced=1
  fi
done
if [ "$referenced" -ne 0 ]; then
  echo "warning: the snapshot still names an excluded file (lines above)" >&2
  [ "$allow_references" -eq 1 ] || { echo "not updating $branch; fix the source or pass --allow-references" >&2; exit 1; }
fi

echo "diff $source_rev -> snapshot:"
git diff --stat "$source_commit" "$tree"
if [ "$(git rev-parse "$parent^{tree}")" = "$tree" ]; then
  echo "$branch already has this tree; nothing to do"
  exit 0
fi
[ "$dry_run" -eq 0 ] || { echo "dry run: tree $tree"; exit 0; }

[ -n "$message" ] || message="HealthOS: snapshot of $source_rev ($(date +%Y-%m-%d))"
commit="$(GIT_AUTHOR_NAME="$author_name" GIT_AUTHOR_EMAIL="$author_email" \
  GIT_COMMITTER_NAME="$author_name" GIT_COMMITTER_EMAIL="$author_email" \
  git commit-tree "$tree" -p "$parent" -m "$message")"

worktree=""
current=""
while IFS= read -r line; do
  case "$line" in
    "worktree "*) current="${line#worktree }" ;;
    "branch refs/heads/$branch") worktree="$current" ;;
  esac
done < <(git worktree list --porcelain)

if [ -n "$worktree" ]; then
  git -C "$worktree" merge --ff-only -q "$commit" || die "fast-forward of $branch failed in its worktree"
else
  git update-ref "refs/heads/$branch" "$commit" "$parent"
fi
echo "$branch -> $(git rev-parse --short "$commit") (parent $(git rev-parse --short "$parent"))"
