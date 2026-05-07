#!/usr/bin/env bash
# stock_app_sync.sh — Mirror Hostinger archive to local Mac for offline DuckDB / pandas analysis.
#
# Downloads archive/index.json then fetches each JSONL.gz week referenced in it.
# Idempotent: skips files that already exist locally.
#
# Usage:
#   ./bin/stock_app_sync.sh [--dest ~/stock_app_archive]
#
# Install as a weekly cron (runs Mon 04:00 local, after Sunday archive_agent):
#   (crontab -l 2>/dev/null; echo "0 4 * * 1 $HOME/Documents/nishant_projects/stock_app/bin/stock_app_sync.sh") | crontab -
#
# Requirements: curl, jq

set -euo pipefail

ARCHIVE_BASE="https://hub4apps.com/stock_app/archive"
DEST="${1:-$HOME/stock_app_archive}"

# ── helpers ─────────────────────────────────────────────────────────────────

log() { echo "[$(date -u '+%H:%M:%S')] $*"; }

require() {
    command -v "$1" >/dev/null 2>&1 || { echo "ERROR: $1 not found — install with: brew install $1"; exit 1; }
}

# ── preflight ────────────────────────────────────────────────────────────────

require curl
require jq

mkdir -p "$DEST"

# ── fetch index ──────────────────────────────────────────────────────────────

INDEX_LOCAL="$DEST/index.json"
log "Fetching $ARCHIVE_BASE/index.json"
curl -fsSL --retry 3 --retry-delay 5 -o "$INDEX_LOCAL" "$ARCHIVE_BASE/index.json"

WEEKS=$(jq -r '.weeks[]?' "$INDEX_LOCAL")
if [[ -z "$WEEKS" ]]; then
    log "No weeks found in index.json — archive may be empty. Done."
    exit 0
fi

TABLES=(
    stock_normalized_events
    stock_event_paper_trades
    stock_signals
    stock_raw_prices
    stock_raw_filings
    stock_institutional_holdings_snapshot
)

# ── sync each week × table ───────────────────────────────────────────────────

n_downloaded=0
n_skipped=0

while IFS= read -r week; do
    # week looks like "2026/W19"
    year="${week%%/*}"
    wlabel="${week##*/}"
    week_dir="$DEST/$year/$wlabel"
    mkdir -p "$week_dir"

    for table in "${TABLES[@]}"; do
        local_file="$week_dir/$table.jsonl.gz"
        remote_url="$ARCHIVE_BASE/$year/$wlabel/$table.jsonl.gz"

        if [[ -f "$local_file" ]]; then
            n_skipped=$((n_skipped + 1))
            continue
        fi

        # HEAD check: if the remote file doesn't exist, skip silently (not all tables
        # archive every week if they had 0 eligible rows).
        http_code=$(curl -fsSL -o /dev/null -w "%{http_code}" --head --retry 2 "$remote_url" 2>/dev/null || echo "000")
        if [[ "$http_code" != "200" ]]; then
            continue
        fi

        log "  $week / $table"
        curl -fsSL --retry 3 --retry-delay 5 -o "$local_file" "$remote_url"
        n_downloaded=$((n_downloaded + 1))
    done
done <<< "$WEEKS"

log "Sync complete — $n_downloaded downloaded, $n_skipped already local"
log "Archive at: $DEST"
