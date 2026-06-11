#!/usr/bin/env bash
# Скачивает рабочую базу бота с VPS для офлайн-анализа и eval-харнесса.
# Usage: bash scripts/pull_kb.sh
set -euo pipefail

mkdir -p data
scp config1:/opt/hde-bot/hde_bot.db data/hde_bot_vps.db
python - <<'EOF'
import sqlite3
conn = sqlite3.connect("data/hde_bot_vps.db")
n_samples = conn.execute("SELECT COUNT(*) FROM optimization_samples").fetchone()[0]
n_prompts = conn.execute("SELECT COUNT(*) FROM prompt_versions").fetchone()[0]
n_kb = conn.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0]
print(f"OK: optimization_samples={n_samples}, prompt_versions={n_prompts}, knowledge_items={n_kb}")
EOF
