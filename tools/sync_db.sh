#!/usr/bin/env bash
# Usage: ./tools/sync_db.sh user@host:/path/to/hde_bot.db
set -euo pipefail
if [[ $# -lt 1 ]]; then
  echo "Usage: $0 user@host:/path/to/hde_bot.db" >&2
  exit 1
fi
rsync -avz --progress "$1" ./hde_bot_local.db
echo "Done. Size: $(du -sh ./hde_bot_local.db | cut -f1)"
