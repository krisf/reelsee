#!/usr/bin/env bash
# P10: build buildout/reelsee_0.2.0_all.deb (§5.5 tree).  No network, no
# root: staging tree + `dpkg-deb --build --root-owner-group`.
set -euo pipefail
cd "$(dirname "$0")/.."

VER=0.2.0
OUT=buildout
STAGE=$OUT/stage

# ambient /tmp may be read-only on this box — dpkg-deb needs a scratch TMPDIR
mkdir -p scratch/dpkg-tmp
export TMPDIR="$PWD/scratch/dpkg-tmp"

rm -rf "$STAGE"
mkdir -p "$STAGE/usr/bin" \
         "$STAGE/usr/share/reelsee" \
         "$STAGE/usr/share/applications" \
         "$STAGE/usr/share/icons/hicolor/scalable/apps" \
         "$STAGE/usr/share/doc/reelsee" \
         "$STAGE/DEBIAN"

install -m 0755 packaging/reelsee-wrapper.sh "$STAGE/usr/bin/reelsee"
install -m 0644 src/*.py            "$STAGE/usr/share/reelsee/"
install -m 0644 data/pro.krisforbes.Reelsee.desktop \
        "$STAGE/usr/share/applications/pro.krisforbes.Reelsee.desktop"
install -m 0644 data/reelsee.svg \
        "$STAGE/usr/share/icons/hicolor/scalable/apps/reelsee.svg"
if [ -f README.md ]; then
    install -m 0644 README.md "$STAGE/usr/share/doc/reelsee/README.md"
else
    printf 'Reelsee\n=======\n\nGTK4/libmpv video player (see upstream README).\n' \
        > "$STAGE/usr/share/doc/reelsee/README.md"
    chmod 0644 "$STAGE/usr/share/doc/reelsee/README.md"
fi
install -m 0644 packaging/copyright "$STAGE/usr/share/doc/reelsee/copyright"
install -m 0644 packaging/DEBIAN/control "$STAGE/DEBIAN/control"

rm -f "$OUT/reelsee_${VER}_all.deb"
dpkg-deb --build --root-owner-group "$STAGE" "$OUT/reelsee_${VER}_all.deb" >&2
echo "built $OUT/reelsee_${VER}_all.deb"
