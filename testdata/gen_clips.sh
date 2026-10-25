#!/usr/bin/env bash
# Deterministic test clips for Reelsee (PLAN §6.1). Regenerating must be
# byte-stable for the hash-pinned inputs (fixed src filters + fixed encode
# settings).  Clips stay <= 4 MB each.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FF="ffmpeg -y -v error -fflags +bitexact -flags:v +bitexact"

# 3 s, 640x360, moving test pattern + tone (smoke / fps assertions)
$FF -f lavfi -i testsrc2=size=640x360:rate=30 -f lavfi -i sine=frequency=440:sample_rate=48000 \
    -t 3 -c:v libx264 -preset veryfast -crf 28 -pix_fmt yuv420p -c:a aac -b:a 64k -shortest \
    "$here/smoke_360p.mp4"

# 10 s, 1920x1080 h264 + aac (pixel/render gate + perf base)
$FF -f lavfi -i testsrc2=size=1920x1080:rate=30 -f lavfi -i sine=frequency=600:sample_rate=48000 \
    -t 10 -c:v libx264 -preset veryfast -crf 30 -pix_fmt yuv420p -c:a aac -b:a 64k -shortest \
    "$here/perf_1080p30.mp4"

# 5 s, 3840x2160 h264 (watchdog stress input)
$FF -f lavfi -i testsrc2=size=3840x2160:rate=30 -t 5 \
    -c:v libx264 -preset veryfast -crf 32 -pix_fmt yuv420p "$here/perf_4k30.mp4"

# 5 s matroska: h264 + TWO audio tracks + mov_text subtitle track (track tests)
$FF -f lavfi -i testsrc2=size=640x360:rate=30 \
    -f lavfi -i "sine=frequency=440:sample_rate=48000" \
    -f lavfi -i "sine=frequency=880:sample_rate=48000" \
    -map 0:v -map 1:a -map 2:a -t 5 \
    -c:v libx264 -preset veryfast -crf 28 -pix_fmt yuv420p -c:a:0 aac -b:a 48k -c:a:1 aac -b:a 48k \
    -metadata:s:a:0 language=eng -metadata:s:a:1 language=fra \
    "$here/tone_5s.mkv"

# external subtitle file for sub-add tests
printf '1\n00:00:00,200 --> 00:00:01,400\nReelsee test cue one\n\n2\n00:00:01,600 --> 00:00:02,800\nReelsee test cue two\n' > "$here/ext.srt"

ls -la "$here"
