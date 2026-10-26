#!/usr/bin/env bash
# Mint the public tree from HEAD, deterministically and offline.
#
# The rule this encodes: everything the published repository needs must live
# IN this repository. If you find yourself hand-editing a file only in the
# published copy, move that content here instead — otherwise the next mint
# silently reverts it.
#
# usage: scripts/make_public.sh [outdir]        (default: ../reelsee-public)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
OUT="${1:-../reelsee-public}"

command -v git >/dev/null || { echo "git required" >&2; exit 2; }
rm -rf "$OUT"; mkdir -p "$OUT"
git archive HEAD | tar -x -C "$OUT"
cd "$OUT"

# --- internal-only material never ships ------------------------------------
rm -rf vendor buildout references scratch
# Root-level spike output (a developer's eyeball image, cited by the DSH
# reports that also do not ship). It belongs in docs/screenshots or not at
# all, never loose in the project root of a release.
rm -f spike-*.png
rm -f DSH-REPORT*.md
find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
find . -name '*.pyc' -delete 2>/dev/null || true

# --- PLAN.md is internal; the published tree calls it docs/DESIGN.md --------
# Docstrings and test names cite "PLAN §x", so the numbers must survive.
mkdir -p docs
{
  printf '# REELSEE — design document (as built)\n\n'
  printf '> Written before the first line of code; kept because docstrings, test\n'
  printf '> names and gates cite its section numbers (`PLAN §4.6`, `R-5`,\n'
  printf '> `P0`…`P12`). Deviating from a rule here means updating the rule *and*\n'
  printf '> its gate, not just the code.\n\n'
  cat PLAN.md
} > docs/DESIGN.md
rm -f PLAN.md

# --- the gates: a mint must FAIL rather than publish something private -----
fail=0
# Generic patterns are compiled in; the PERSONAL ones live in
# scripts/leak-patterns.txt, which is git-ignored on purpose. This script is
# committed, so anything literal in it would be published with the release —
# a leak checker that names the private things it is protecting is a leak.
# Deliberately narrow: `10\.` once matched `-10.0`, and matching
# @users.noreply would refuse the release for carrying the one identity it is
# supposed to carry. Site-specific names and addresses live in the ignored
# pattern file, where they can be specific without being published.
PATTERNS='^(/|[.]|[a-z]+:)?/?home/[a-z]+/|192\.168\.|TODO-SET-UPSTREAM'
if [ -f scripts/leak-patterns.txt ]; then
  while IFS= read -r pat; do
    [ -n "$pat" ] || continue
    case "$pat" in \#*) continue ;; esac
    if grep -rInE --exclude=make_public.sh "$pat" . >/dev/null 2>&1; then
      echo "ERROR: minted tree matches a private pattern ($pat)" >&2; fail=1
    fi
  done < scripts/leak-patterns.txt
fi
if grep -rInE --exclude=make_public.sh "$PATTERNS" . >/dev/null 2>&1; then
  echo "ERROR: a private path, private network or unset placeholder is present" >&2
  fail=1
fi
# A gate that hard-codes an old version tests a package that no longer exists.
VER="$(sed -n 's/^Version: *//p' packaging/DEBIAN/control 2>/dev/null | head -1)"
if [ -n "$VER" ]; then
  if grep -rInE --exclude=make_public.sh 'reelsee_[0-9]+\.[0-9]+[._0-9]*_all[.]deb' . 2>/dev/null |
     grep -v "reelsee_${VER}_all[.]deb" | grep -q .; then
    echo "ERROR: a .deb filename is pinned to something other than $VER" >&2; fail=1
  fi
fi
if [ -e vendor ] || [ -e references ] || ls DSH-REPORT* >/dev/null 2>&1; then
  echo "ERROR: internal material survived the prune" >&2; fail=1
fi
[ "$fail" -eq 0 ] || { echo "mint refused — fix the source tree, not this script" >&2; exit 1; }

# --- documentation assets must match each other -----------------------------
# A README that pictures nothing, or a screenshot nothing points at, is a
# regression that byte-parity checks cannot see (0.1.1 shipped exactly that).
while IFS= read -r img; do
  [ -f "$img" ] || { echo "ERROR: README references a missing file: $img" >&2; fail=1; }
done < <(grep -oE '\!\[[^]]*\]\((docs/[^)]+)\)' README.md 2>/dev/null | sed 's/.*(\(.*\))/\1/' | sort -u)
while IFS= read -r png; do
  grep -qF "($png)" README.md 2>/dev/null || { echo "ERROR: asset not referenced by README: $png" >&2; fail=1; }
done < <(find docs/screenshots -name '*.png' 2>/dev/null | sort)
[ "$fail" -eq 0 ] || { echo "mint refused — documentation assets are inconsistent" >&2; exit 1; }

echo "minted $(find . -type f | wc -l) files, $(du -sh . | cut -f1) -> $OUT"
