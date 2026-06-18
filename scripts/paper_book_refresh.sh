#!/usr/bin/env bash
# Refresh the local Paper Book in one command — no key pasting.
#
# Auto-sources the Supabase service_role key via the CLI (the awk-piped form from
# CLAUDE.md — the key is captured into an env var, never printed to stdout). Then
# runs scripts/paper_book.py (default mode "run": sync -> replay -> state -> dash).
#
# Usage:
#   scripts/paper_book_refresh.sh            # full refresh + rebuild dashboard
#   scripts/paper_book_refresh.sh dash       # rebuild dashboard only (no network)
set -euo pipefail

PROJECT_REF="wlfwdtdtiedlcczfoslt"
export SUPABASE_URL="https://${PROJECT_REF}.supabase.co"
export SUPABASE_SERVICE_KEY="$(supabase projects api-keys --project-ref "${PROJECT_REF}" \
  | awk '/service_role/{print $NF}')"

if [[ -z "${SUPABASE_SERVICE_KEY}" ]]; then
  echo "ERROR: could not read service_role key (is the supabase CLI logged in?)" >&2
  exit 1
fi

cd "$(dirname "$0")/.."
exec python3 scripts/paper_book.py "${1:-run}"
