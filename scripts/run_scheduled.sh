#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

exec "$ROOT/.venv/bin/python" -m src.cli naukri scheduled-run --scan-queries 30 --per-query 20 --limit 5 --slow-mo 0
