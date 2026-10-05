#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
BROWSER_DIR="$ROOT/.browser/naukri_agent"
PORT="${NAUKRI_CDP_PORT:-9229}"
HOST="${NAUKRI_CDP_HOST:-127.0.0.1}"

if [ -n "${BROWSER_EXECUTABLE_PATH:-}" ] && [ -x "${BROWSER_EXECUTABLE_PATH}" ]; then
    BROWSER_EXECUTABLE="${BROWSER_EXECUTABLE_PATH}"
else
    BROWSER_EXECUTABLE=""
    for candidate in /usr/bin/google-chrome /usr/bin/google-chrome-stable /usr/bin/chromium /usr/bin/chromium-browser /snap/bin/chromium; do
        if [ -x "$candidate" ]; then BROWSER_EXECUTABLE="$candidate"; break; fi
    done
fi

if [ -z "$BROWSER_EXECUTABLE" ]; then echo "No supported Chrome/Chromium executable found." >&2; exit 1; fi
mkdir -p "$BROWSER_DIR"

exec "$BROWSER_EXECUTABLE" \
    --user-data-dir="$BROWSER_DIR" \
    --remote-debugging-address="$HOST" \
    --remote-debugging-port="$PORT" \
    --no-first-run \
    --no-default-browser-check \
    --disable-gpu \
    --disable-dev-shm-usage \
    "$@"
