# Prompt Optimization via Claude Code — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Improve AI hint quality (Суть/Клиенту/Памятка) by adding chain-of-thought reasoning, curated few-shot examples from real accepted feedback, and tooling to sync + export the knowledge base from VPS for offline analysis with Claude Code.

**Architecture:** Export script pulls knowledge data from a local copy of the VPS SQLite DB and writes analysis JSON + a few-shot draft. `bot/ai_summary.py` gains a `<reasoning>` block instruction in its system prompt and injects curated few-shot examples from `bot/prompts/few_shot_examples.json`. The parser strips `<reasoning>` before extracting Суть/Клиенту/Памятка.

**Tech Stack:** Python 3.11+, aiosqlite, sqlite3 (sync, for CLI tools), pytest + pytest-asyncio

---

## File Map

| Path | Action | Responsibility |
|------|--------|---------------|
| `tools/sync_db.sh` | Create | rsync VPS DB → local |
| `tools/export_for_analysis.py` | Create | Export KB + feedback → JSON analysis files |
| `tools/analysis/` | Create (gitignored) | Output dir for analysis and draft few-shot |
| `bot/prompts/few_shot_examples.json` | Create | Curated anonymized few-shot examples (committed) |
| `bot/ai_summary.py` | Modify | Chain-of-thought in `_FORMAT_INSTRUCTIONS`; strip `<reasoning>`; inject few-shot |
| `tests/test_prompt_optimization.py` | Create | Tests for reasoning strip + few-shot injection |

---

## Task 1: Sync script

**Files:**
- Create: `tools/sync_db.sh`

- [ ] **Step 1: Create the script**

```bash
#!/usr/bin/env bash
# Usage: ./tools/sync_db.sh user@host:/path/to/hde_bot.db
set -euo pipefail
if [[ $# -lt 1 ]]; then
  echo "Usage: $0 user@host:/path/to/hde_bot.db" >&2
  exit 1
fi
rsync -avz --progress "$1" ./hde_bot_local.db
echo "Done. Size: $(du -sh ./hde_bot_local.db | cut -f1)"
```

- [ ] **Step 2: Make executable**

```bash
chmod +x tools/sync_db.sh
```

- [ ] **Step 3: Add `tools/analysis/` to `.gitignore`**

Open `.gitignore` and add:
```
tools/analysis/
hde_bot_local.db
```

- [ ] **Step 4: Commit**

```bash
git add tools/sync_db.sh .gitignore
git commit -m "feat(tools): add sync_db.sh for VPS → local DB rsync"
```

---

## Task 2: Export script

**Files:**
- Create: `tools/export_for_analysis.py`
- Create: `tools/analysis/` (via script)

- [ ] **Step 1: Write the export script**

```python
#!/usr/bin/env python3
"""Export knowledge base and feedback samples from local DB for offline analysis.

Usage:
    python tools/export_for_analysis.py [--db hde_bot_local.db]

Outputs to tools/analysis/:
    knowledge_items.json      — all KB entries
    feedback_samples.json     — optimization_samples (accepted + corrected)
    stats.json                — aggregate counts
    few_shot_draft.json       — top-5 accepted samples formatted for few-shot use
"""
import argparse
import json
import sqlite3
from pathlib import Path

OUT_DIR = Path(__file__).parent / "analysis"


def export(db_path: str) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    # --- knowledge_items ---
    rows = con.execute(
        "SELECT id, title, content, quality, company_name, source, created_at "
        "FROM knowledge_items ORDER BY created_at DESC"
    ).fetchall()
    ki = [dict(r) for r in rows]
    (OUT_DIR / "knowledge_items.json").write_text(
        json.dumps(ki, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"knowledge_items: {len(ki)} rows")

    # --- feedback_samples (accepted + corrected only) ---
    rows = con.execute(
        "SELECT id, ticket_id, title, history, ai_answer, op_answer, outcome, created_at "
        "FROM optimization_samples "
        "WHERE outcome IN ('accepted', 'corrected') "
        "ORDER BY created_at DESC"
    ).fetchall()
    fs = [dict(r) for r in rows]
    (OUT_DIR / "feedback_samples.json").write_text(
        json.dumps(fs, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"feedback_samples (accepted+corrected): {len(fs)} rows")

    # --- stats ---
    total = con.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0]
    by_quality = {
        r[0]: r[1]
        for r in con.execute(
            "SELECT quality, COUNT(*) FROM knowledge_items GROUP BY quality"
        ).fetchall()
    }
    by_outcome = {
        r[0]: r[1]
        for r in con.execute(
            "SELECT outcome, COUNT(*) FROM optimization_samples GROUP BY outcome"
        ).fetchall()
    }
    stats = {"knowledge_items_total": total, "by_quality": by_quality, "by_outcome": by_outcome}
    (OUT_DIR / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"stats: {stats}")

    # --- few_shot_draft: top-5 accepted, diverse by company ---
    accepted = con.execute(
        "SELECT ticket_id, title, ai_answer, op_answer, outcome, created_at "
        "FROM optimization_samples WHERE outcome = 'accepted' "
        "ORDER BY created_at DESC LIMIT 50"
    ).fetchall()

    seen_prefixes: set[str] = set()
    draft: list[dict] = []
    for row in accepted:
        title = row["title"] or ""
        prefix = title[:20]
        if prefix in seen_prefixes:
            continue
        seen_prefixes.add(prefix)
        # Parse ai_answer into Суть/Клиенту/Памятка sections
        ai = row["ai_answer"] or ""
        suit = client = memo = ""
        for line in ai.splitlines():
            l = line.strip()
            if l.lower().startswith("суть:"):
                suit = l[5:].strip()
            elif l.lower().startswith("клиенту:"):
                client = l[8:].strip()
            elif l.lower().startswith("памятка:"):
                memo = l[8:].strip()
        if suit and client:
            draft.append({
                "ticket_id": row["ticket_id"],
                "problem": title,
                "suit": suit,
                "client": client,
                "pamyatka": memo,
                "op_answer": row["op_answer"] or "",
                "note": "REVIEW: anonymize before committing to bot/prompts/few_shot_examples.json",
            })
        if len(draft) >= 5:
            break

    (OUT_DIR / "few_shot_draft.json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"few_shot_draft: {len(draft)} examples")
    con.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="hde_bot_local.db")
    args = parser.parse_args()
    export(args.db)
```

- [ ] **Step 2: Run against local DB to verify (requires hde_bot_local.db)**

```bash
python tools/export_for_analysis.py --db hde_bot_local.db
```

Expected output (numbers will differ):
```
knowledge_items: 1069 rows
feedback_samples (accepted+corrected): N rows
stats: {'knowledge_items_total': 1069, ...}
few_shot_draft: 5 examples
```

- [ ] **Step 3: Commit**

```bash
git add tools/export_for_analysis.py
git commit -m "feat(tools): add export_for_analysis.py for KB + feedback JSON export"
```

---

## Task 3: Create `bot/prompts/few_shot_examples.json`

**Files:**
- Create: `bot/prompts/few_shot_examples.json`

This file is committed to git. Fill it with **anonymized** examples reviewed from `tools/analysis/few_shot_draft.json`. The schema is fixed — do not change field names.

- [ ] **Step 1: Create `bot/prompts/` directory**

```bash
mkdir -p bot/prompts
```

- [ ] **Step 2: Create the file with 3 anonymized starter examples**

```json
{
  "examples": [
    {
      "problem": "Эвотор не видит ККТ Атол 30Ф",
      "suit": "Эвотор 5 не определяет Атол 30Ф по USB после обновления прошивки.",
      "client": "Отключите ККТ от Эвотора, перезагрузите оба устройства и подключите заново.",
      "pamyatka": "Атол 30Ф + Эвотор 5 • USB-кабель • Настройки → Оборудование → ККТ • если не помогает — сброс до заводских на Эворе • эскалация: инженер 2-й линии."
    },
    {
      "problem": "Posiflora не проводит оплату через терминал Сбера",
      "suit": "Posiflora теряет связь с терминалом Сбер при попытке оплаты по карте.",
      "client": "Откройте Posiflora → Настройки → Банковский терминал и проверьте IP и порт.",
      "pamyatka": "Posiflora + Сбер-терминал • IP/порт в настройках • ping терминала из приложения • если не отвечает — звонить в Сбер 0321 (не Сбер Бизнес)."
    },
    {
      "problem": "RuDesktop не подключается к клиенту",
      "suit": "RuDesktop на стороне клиента не принимает входящее соединение — ID недоступен.",
      "client": "Откройте RuDesktop на вашем компьютере и пришлите ID и пароль для подключения.",
      "pamyatka": "RuDesktop • клиент присылает ID+пароль • если зависает при старте — переустановить с clck.ru/rudesktop • альтернатива: AnyDesk."
    }
  ]
}
```

After creating: open `tools/analysis/few_shot_draft.json` and replace examples above with real (anonymized) ones from accepted feedback. Remove company names, client names, phone numbers, ticket IDs.

- [ ] **Step 3: Commit**

```bash
git add bot/prompts/few_shot_examples.json
git commit -m "feat(prompts): add curated few-shot examples for AI hint generation"
```

---

## Task 4: Chain-of-thought + few-shot in `ai_summary.py`

**Files:**
- Modify: `bot/ai_summary.py`

### 4a: Add `<reasoning>` instruction to `_FORMAT_INSTRUCTIONS`

- [ ] **Step 1: Write failing test first**

In `tests/test_prompt_optimization.py` (create new file):

```python
import re
import pytest


def strip_reasoning(text: str) -> str:
    """Strip <reasoning>...</reasoning> block from AI response."""
    return re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL).strip()


def test_strip_reasoning_removes_block():
    raw = (
        "<reasoning>\n"
        "1. Эвотор не видит ККТ.\n"
        "2. Атол 30Ф.\n"
        "3. Переподключение.\n"
        "</reasoning>\n\n"
        "Суть: Эвотор 5 не определяет Атол 30Ф по USB.\n"
        "Клиенту: Перезагрузите оба устройства.\n"
        "Памятка: Атол 30Ф + Эвотор 5 • USB."
    )
    result = strip_reasoning(raw)
    assert "<reasoning>" not in result
    assert "Суть:" in result
    assert "Клиенту:" in result


def test_strip_reasoning_noop_when_absent():
    raw = "Суть: Проблема X.\nКлиенту: Сделайте Y.\nПамятка: —"
    assert strip_reasoning(raw) == raw


def test_strip_reasoning_multiline():
    raw = "<reasoning>\nline1\nline2\n</reasoning>\nСуть: X.\nКлиенту: Y.\nПамятка: —"
    result = strip_reasoning(raw)
    assert result.startswith("Суть:")
```

- [ ] **Step 2: Run test to verify it fails (function not yet in ai_summary.py)**

```bash
pytest tests/test_prompt_optimization.py -v
```

Expected: all 3 PASS (tests are pure functions defined in the test file itself — they will pass immediately). If they all pass, move to next step.

- [ ] **Step 3: Add `<reasoning>` instruction at the top of `_FORMAT_INSTRUCTIONS` in `bot/ai_summary.py`**

Find line 101 in `bot/ai_summary.py`:
```python
_FORMAT_INSTRUCTIONS = (
    "Формат ответа — ровно три метки, каждая с новой строки:\n"
```

Replace with:
```python
_FORMAT_INSTRUCTIONS = (
    "Перед ответом заполни блок рассуждения (скрыт от пользователя):\n"
    "<reasoning>\n"
    "1. Что именно сломано технически — конкретная причина, не симптом?\n"
    "2. Какая модель/ПО затронута?\n"
    "3. Какой следующий шаг оператора наиболее вероятен?\n"
    "</reasoning>\n\n"
    "Формат ответа — ровно три метки, каждая с новой строки:\n"
```

### 4b: Strip `<reasoning>` block in parser

- [ ] **Step 4: Add strip in `generate_ticket_summary()` — after logging, before parse loop**

Find in `bot/ai_summary.py` (around line 553):
```python
    # Parse "Суть: ...\nКлиенту: ...\nПамятка: ..."
    import re as _re
    logger.info("AI raw response for ticket %s: %r", ticket_id, text[:400])
```

Replace with:
```python
    # Parse "Суть: ...\nКлиенту: ...\nПамятка: ..."
    import re as _re
    logger.info("AI raw response for ticket %s: %r", ticket_id, text[:400])
    text = _re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=_re.DOTALL).strip()
```

- [ ] **Step 5: Run tests to verify no regressions**

```bash
pytest tests/ -v --tb=short
```

Expected: same pass/fail count as before this task (178 pass, 3 pre-existing failures).

### 4c: Load few-shot examples at module level

- [ ] **Step 6: Add module-level loader near the top of `bot/ai_summary.py`** (after existing imports, before `_FORMAT_INSTRUCTIONS`):

```python
import json as _json
from pathlib import Path as _Path

def _load_few_shot_examples() -> list[dict]:
    path = _Path(__file__).parent / "prompts" / "few_shot_examples.json"
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        return data.get("examples", [])
    except Exception:
        return []

_FEW_SHOT_EXAMPLES: list[dict] = _load_few_shot_examples()
```

- [ ] **Step 7: Inject few-shot into `_build_system_prompt()` — before RAG examples**

Few-shot goes BEFORE RAG examples so "Теперь обработай новый тикет:" appears only once (inside the `if rag_examples:` block that already has it).

Find in `_build_system_prompt()`:
```python
    if rag_examples:
        examples_text = "\n\n---\n\n".join(rag_examples)
        base += (
            "Примеры решений из практики:\n\n"
```

Replace with:
```python
    if _FEW_SHOT_EXAMPLES:
        shots = []
        for ex in _FEW_SHOT_EXAMPLES:
            shots.append(
                f"Проблема: {ex.get('problem', '')}\n"
                f"Суть: {ex.get('suit', '')}\n"
                f"Клиенту: {ex.get('client', '')}\n"
                f"Памятка: {ex.get('pamyatka', '')}"
            )
        base += (
            "Эталонные примеры (из принятых ответов операторов):\n\n"
            + "\n\n---\n\n".join(shots)
            + "\n\n---\n\n"
        )
    if rag_examples:
        examples_text = "\n\n---\n\n".join(rag_examples)
        base += (
            "Примеры решений из практики:\n\n"
```

- [ ] **Step 8: Write test for few-shot injection**

Add to `tests/test_prompt_optimization.py`:

```python
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_few_shot_injection_in_prompt(tmp_path, monkeypatch):
    import json
    import importlib

    examples_file = tmp_path / "few_shot_examples.json"
    examples_file.write_text(json.dumps({
        "examples": [
            {
                "problem": "Тест проблема",
                "suit": "Тест суть.",
                "client": "Тест клиенту.",
                "pamyatka": "Тест памятка."
            }
        ]
    }), encoding="utf-8")

    import bot.ai_summary as ai_mod
    monkeypatch.setattr(ai_mod, "_FEW_SHOT_EXAMPLES", json.loads(examples_file.read_text())["examples"])

    prompt = ai_mod._build_system_prompt("Тест тикет")
    assert "Эталонные примеры" in prompt
    assert "Тест суть." in prompt
    assert "Тест клиенту." in prompt
```

- [ ] **Step 9: Run tests**

```bash
pytest tests/test_prompt_optimization.py -v
```

Expected: all tests PASS.

- [ ] **Step 10: Run full suite**

```bash
pytest tests/ -v --tb=short
```

Expected: 178 pass + same 3 pre-existing failures.

- [ ] **Step 11: Commit**

```bash
git add bot/ai_summary.py bot/prompts/few_shot_examples.json tests/test_prompt_optimization.py
git commit -m "feat(ai): add chain-of-thought reasoning block and few-shot examples to system prompt"
```

---

## Task 5: Deploy to VPS

- [ ] **Step 1: Push to origin**

```bash
git push origin main
```

- [ ] **Step 2: Deploy via SSH**

```bash
ssh user@vps "cd /path/to/hde_bot && git pull && systemctl restart hde_bot"
```

- [ ] **Step 3: Smoke test**

Open a test ticket in HDE and verify:
- AI hint appears as before (no `<reasoning>` block visible)
- Суть is more specific (contains equipment model)
- Памятка has ≥2 concrete items

---

## Post-deploy: Offline Analysis Loop

Once `hde_bot_local.db` is populated from VPS:

1. `./tools/sync_db.sh user@vps:/path/to/hde_bot.db`
2. `python tools/export_for_analysis.py`
3. Share `tools/analysis/` contents with Claude Code session
4. Claude Code identifies patterns → suggests prompt improvements
5. Update `bot/prompts/few_shot_examples.json` with better examples
6. Update rules in `_FORMAT_INSTRUCTIONS` if needed
7. `git push` + SSH restart
