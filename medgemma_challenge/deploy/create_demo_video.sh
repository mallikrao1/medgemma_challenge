#!/usr/bin/env bash
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: ./medgemma_challenge/deploy/create_demo_video.sh <LIVE_URL>"
  exit 1
fi

LIVE_URL="$1"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VIDEO_DIR="${SCRIPT_DIR}/videos"
mkdir -p "${VIDEO_DIR}"

pushd "${SCRIPT_DIR}" >/dev/null
npm install
node record_browser_demo.js "$LIVE_URL"
popd >/dev/null

LATEST_WEBM=$(ls -t "${VIDEO_DIR}"/*.webm 2>/dev/null | head -n 1 || true)
if [ -z "$LATEST_WEBM" ]; then
  echo "No webm recording found"
  exit 1
fi

OUT_MP4="${VIDEO_DIR}/demo_submission.mp4"
ffmpeg -y -i "$LATEST_WEBM" -c:v libx264 -pix_fmt yuv420p -movflags +faststart "$OUT_MP4"
echo "Demo video ready: $OUT_MP4"
