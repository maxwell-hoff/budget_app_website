#!/bin/sh
# Renders og_image.html to frontend/static/og-image.png (1200x630), the image shown
# when a link to the site is shared (Twitter/X, Slack, iMessage, LinkedIn, ...).
# Needs Google Chrome; run from anywhere.
set -e
here=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$here/../.." && pwd)
chrome=${CHROME:-"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"}
"$chrome" --headless --disable-gpu --hide-scrollbars --force-device-scale-factor=1 \
  --window-size=1200,630 --virtual-time-budget=5000 \
  --allow-file-access-from-files \
  --screenshot="$root/frontend/static/og-image.png" "file://$here/og_image.html"
echo "Wrote $root/frontend/static/og-image.png"
