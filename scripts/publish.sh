#!/usr/bin/env bash
# Mint HEAD and publish it, with the identity fixed in one place.
#
# Why this exists: a fresh clone of the public repository inherits whatever
# user.name/user.email this machine is configured with — which is the real
# personal address — and git writes that into every commit it makes. Push a
# release that way and the private address is published in commit metadata
# even though no file in the tree contains it. Setting it here means the
# mistake needs a deliberate edit rather than a forgotten step.
#
# usage: scripts/publish.sh [-m msgfile] [--dry-run] [--clone DIR]
#   --dry-run   mint, sync, print what WOULD be committed, push nothing
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
REMOTE_URL="${REELSEE_PUBLIC_URL:-https://github.com/krisf/reelsee.git}"
PUB_NAME="Kris Forbes"
PUB_EMAIL="krisf@users.noreply.github.com"
MSG=""; DRY=0; CLONE=""
while [ $# -gt 0 ]; do
  case "$1" in
    -m) MSG="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --clone) CLONE="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$CLONE" ] || CLONE="$(cd .. && pwd)/reelsee-publish-clone"

scripts/make_public.sh
if [ ! -d "$CLONE/.git" ]; then
  git clone -q "$REMOTE_URL" "$CLONE"
fi
cd "$CLONE"
[ "$(git remote get-url origin)" = "$REMOTE_URL" ] || {
  echo "ERROR: $CLONE origin is not $REMOTE_URL" >&2; exit 1; }
git fetch -q origin
git checkout -q main 2>/dev/null || git checkout -q -B main origin/main
git pull -q --ff-only origin main
git config user.name "$PUB_NAME"
git config user.email "$PUB_EMAIL"
rsync -a --delete --exclude .git "$(cd ../reelsee-public && pwd)/" ./
git add -A
if git diff --cached --quiet; then
  echo "nothing to publish — the published tree already equals HEAD"
  exit 0
fi
echo "staged: $(git diff --cached --name-only | wc -l) files"
git diff --cached --name-status | sed 's/^/  /'
if [ "$DRY" -eq 1 ]; then
  echo "DRY RUN — nothing committed or pushed"
  git reset -q
  exit 0
fi
if [ -z "$MSG" ] || [ ! -f "$MSG" ]; then
  echo "ERROR: -m <message file> is required to publish (multi-line, via -F)" >&2
  exit 2
fi
git commit -q -F "$MSG"
echo "commit: $(git log -1 --format='%h %an <%ae> — %s')"
git push -q origin main
echo "pushed $(git rev-parse HEAD) to $REMOTE_URL"
