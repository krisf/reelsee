#!/bin/sh
# /usr/bin/reelsee — §5.5 installed launcher.
#   REELSEE_SHARE_DIR      app tree location (default /usr/share/reelsee)
#   REELSEE_SMOKE_INTERP   interpreter override for packaging smoke tests
set -e
SHARE="${REELSEE_SHARE_DIR:-/usr/share/reelsee}"
INTERP="${REELSEE_SMOKE_INTERP:-/usr/bin/python3}"
exec "$INTERP" "$SHARE/reelsee.py" "$@"
