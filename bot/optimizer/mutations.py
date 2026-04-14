"""Build prompts for asking LLMs to propose mutations of FORMAT_INSTRUCTIONS."""
from __future__ import annotations


_SYSTEM_TEMPLATE = """Ты эксперт по улучшению промптов для AI-ассистентов технической поддержки кассового оборудования (АТОЛ, Эвотор, Штрих-М, Viki, эквайринг).

Текущая инструкция форматирования ответов:
---
{current_instructions}
---

Твоя задача: предложи улучшенную версию этой инструкции на основе примеров работы ассистента.

При улучшении секции «Клиенту:» строго соблюдай эти правила стиля:
- Не начинать с «Добрый день!», «Здравствуйте!» или любого приветствия — сразу по делу
- Не использовать фразы: «Надеюсь, это поможет», «Если у вас есть вопросы», «Спасибо за обращение», «Пожалуйста, обратитесь к нам снова»
- Не использовать списки там, где достаточно одного предложения
- Не писать «Пожалуйста, выполните следующие шаги:» — просто описывать действия
- Писать как живой сотрудник поддержки, не как шаблон или чат-бот
- Без вводных фраз-заглушек: «Конечно!», «Разумеется!», «Безусловно!», «Отличный вопрос!»
- Не дублировать смысл: если мысль уже выражена, не повторять её другими словами

Верни ТОЛЬКО новый текст инструкции, без пояснений и без markdown-форматирования."""


_USER_TEMPLATE = """Примеры где оператор принял ответ AI (хорошие):
{good_block}

Примеры где оператор отклонил или исправил ответ AI (плохие):
{bad_block}

Предложи улучшенную инструкцию форматирования."""


def build_mutation_prompt(
    current_instructions: str,
    good_examples: list[dict],
    bad_examples: list[dict],
) -> tuple[str, str]:
    """Returns (system_prompt, user_prompt) for a mutation request.

    Args:
        current_instructions: current _FORMAT_INSTRUCTIONS text
        good_examples: list of dicts with ai_answer, op_answer (outcome accepted/sent)
        bad_examples: list of dicts with ai_answer, op_answer (outcome rejected/corrected)
    """
    good_lines = []
    for i, ex in enumerate(good_examples[:5], 1):
        good_lines.append(f"{i}. AI предложил: {ex['ai_answer'][:200]}")
        if ex.get("op_answer"):
            good_lines.append(f"   Оператор отправил: {ex['op_answer'][:200]}")

    bad_lines = []
    for i, ex in enumerate(bad_examples[:5], 1):
        bad_lines.append(f"{i}. AI предложил: {ex['ai_answer'][:200]}")
        if ex.get("op_answer"):
            bad_lines.append(f"   Оператор исправил на: {ex['op_answer'][:200]}")

    system = _SYSTEM_TEMPLATE.format(current_instructions=current_instructions)
    user = _USER_TEMPLATE.format(
        good_block="\n".join(good_lines) or "(нет примеров)",
        bad_block="\n".join(bad_lines) or "(нет примеров)",
    )
    return system, user
