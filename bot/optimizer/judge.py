"""LLM-судья кандидата промпта против фактического ответа оператора (спека §11).

Идея взята из `llm-as-a-verifier`, но его главный приём — мат. ожидание по
logprob-распределению score-токенов — физически недоступен: Groq отдаёт 400 на
`logprobs`/`top_logprobs`, а `n` обязан быть 1. Поэтому применены два приёма из
трёх:

1. **Декомпозиция критериев и шкала 1–20.** Булево «верно/неверно» склеивает
   «почти верно» и «мимо» в один бит, а именно в этой зоне лежат различия между
   кандидатами промпта. 0–10 судьи сваливают в круглые 7 и 8; 20 делений дают
   разрешение, которым усреднение может воспользоваться — при условии ЯКОРЕЙ
   (без них судья просто переедет на «14, 16, 18»).
2. **k повторов с усреднением** — выборочное среднее вместо мат. ожидания по
   распределению. Границы приёма честные: повторы снижают шум СУДЬИ, а не
   неопределённость датасета (26 кейсов × 3 повтора — по-прежнему 26 кейсов);
   «ошибка падает как 1/√k» верно лишь при независимых повторах, а ответы LLM
   бывают коррелированы. Смысл есть только при temperature > 0 — при 0 три
   вызова дадут одно и то же, и усреднять будет нечего.

Что судья НЕ оценивает: формат, длину, безопасность и пустоту — это код
(`checks.hard_check`). Отдавать регексп LLM значит платить за шум.

Модель — `OPTIMIZER_JUDGE_MODEL`, обязана быть другого семейства, чем
генератор, иначе судья поощряет собственный стиль (self-preference).
"""
from __future__ import annotations

import hashlib
import json
import logging
import statistics
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

from ..llm_semaphore import LLM_SEMAPHORE
from .checks import CheckResult, hard_check
from .evaluator import _generate_answer, combined_score

logger = logging.getLogger(__name__)

_URL = "https://api.groq.com/openai/v1/chat/completions"

# Версии участвуют в ключе кеша вердиктов: правка рубрики или схемы обязана
# инвалидировать старые вердикты, иначе кеш отдаст оценку по прежним правилам.
RUBRIC_VERSION = "v2"
SCHEMA_VERSION = "1"

AXES = ("diagnosis", "equipment", "actionability", "human")

# Диагноз тяжелее стиля: неверная причина делает ответ бесполезным, неровный тон —
# нет. Сумма веса применимых осей нормируется, поэтому абсолютные значения важны
# только соотношением.
AXIS_WEIGHTS: dict[str, float] = {
    "diagnosis": 0.40,
    "actionability": 0.30,
    "human": 0.15,
    "equipment": 0.15,
}

SCALE_MIN, SCALE_MAX = 1, 20

JUDGE_REPEATS = 3
# Стартовая гипотеза, НЕ измеренный оптимум: значение подбирается замером по
# стабильности, согласию с ручной разметкой и частоте битого JSON.
JUDGE_TEMPERATURE = 0.6

# Грубо неверный диагноз нельзя отмыть форматом и живым тоном: ставим потолок
# общему баллу кейса. Порог в шкале 1–20 (5 = «крупная ошибка, отправлять нельзя»).
DIAGNOSIS_FAIL_MAX = 5
DIAGNOSIS_FAIL_CAP = 0.3

# Вес судьи в holdout-скоре. Пока 0.5: вторая половина (combined_score) несёт
# ЕДИНСТВЕННЫЙ человеческий сигнал в формуле — веса исходов из ночной сверки,
# тогда как судья сам LLM. Сдвигать к 0.3/0.7 — после того как новый судья
# покажет разброс меньше старого (§11.8).
JUDGE_WEIGHT = 0.5

CallFn = Callable[..., Awaitable[str]]


def _load_style_guide() -> str:
    path = Path(__file__).resolve().parents[1] / "prompts" / "style_guide_ru.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


# --- рубрика ---------------------------------------------------------------

_ANCHORS = (
    " 1 — полностью неверно или опасно\n"
    " 5 — крупная ошибка, отправлять нельзя\n"
    "10 — частично полезно, нужна существенная правка\n"
    "15 — хороший ответ, правка косметическая\n"
    "20 — корректно и готово к отправке"
)


def build_judge_prompt(
    history: str, title: str, candidate: str, op_answer: str
) -> tuple[str, str]:
    """(system, user) для одной оценки. Оси, якоря, контракт N/A."""
    system = (
        "Ты строгий судья качества ответов AI-ассистента поддержки кассового "
        "оборудования (АТОЛ, Эвотор, Штрих-М, Viki, эквайринг).\n"
        "Ответ ассистента имеет формат «Суть / Клиенту / Памятка».\n"
        "Оценивай ТОЛЬКО смысл. Формат, длину, безопасность и пустоту проверяет "
        "код — их не оценивай и в баллах не учитывай.\n\n"
        "Оси, каждая по шкале 1–20:\n"
        "diagnosis — верно ли понята причина проблемы (сверяй с ответом оператора)\n"
        "equipment — правильно ли названы модель и ПО\n"
        "actionability — получил ли клиент конкретный следующий шаг\n"
        "human — звучит ли текст как живой оператор (стайлгайд ниже)\n\n"
        f"Якоря шкалы, одинаковые для всех осей:\n{_ANCHORS}\n"
        "Промежуточные значения используй свободно, не сваливайся в круглые числа.\n\n"
        "applicable=false ставь, только когда ось к этому тикету неприменима:\n"
        "- equipment: в истории не упомянуты ни модель, ни ПО;\n"
        "- diagnosis: тикет требует не причины, а процедурного действия или "
        "запроса данных.\n"
        "ВАЖНО: если модели в истории нет, а кандидат её НАЗВАЛ — это выдумка, "
        "то есть низкий diagnosis, а НЕ equipment=false.\n"
        "При applicable=false ставь score=null.\n\n"
        "Верни СТРОГО JSON без пояснений вне него:\n"
        '{"diagnosis": {"score": 1-20 или null, "applicable": true/false, '
        '"reason": "кратко"}, "equipment": {...}, "actionability": {...}, '
        '"human": {...}}\n\n'
        "Стайлгайд для оси human:\n"
        f"{_load_style_guide()}"
    )
    user = (
        f"Тема тикета: {title}\n\n"
        f"История (фрагмент):\n{history[-2000:]}\n\n"
        f"Ответ кандидата:\n{candidate}\n\n"
        f"Реальный ответ оператора:\n{op_answer}"
    )
    return system, user


# --- разбор и агрегация (чистые функции) ------------------------------------

def normalize(score: float) -> float:
    """1 → 0.0, 20 → 1.0.

    Именно `(s-1)/19`, а не `s/20`: иначе «полностью неверно» получает 0.05 и
    перестаёт быть нулём, а нижняя часть шкалы теряет смысл.
    """
    return (float(score) - SCALE_MIN) / (SCALE_MAX - SCALE_MIN)


def parse_verdict(raw: str) -> dict | None:
    """Валидированный вердикт или None.

    Отклоняем: пропущенную ось, строку вместо числа, значение вне 1–20,
    applicable=true без балла. Ноль доверия формату ответа модели: битый вердикт
    должен выпасть из усреднения, а не проехать нулём или дефолтом.
    """
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None

    parsed: dict[str, dict] = {}
    for axis in AXES:
        item = obj.get(axis)
        if not isinstance(item, dict):
            return None
        applicable = item.get("applicable")
        if not isinstance(applicable, bool):
            return None
        score = item.get("score")
        if not applicable:
            parsed[axis] = {"score": None, "applicable": False,
                            "reason": str(item.get("reason", ""))[:200]}
            continue
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return None
        if not (SCALE_MIN <= float(score) <= SCALE_MAX):
            return None
        parsed[axis] = {"score": float(score), "applicable": True,
                        "reason": str(item.get("reason", ""))[:200]}
    return parsed


def aggregate_verdict(verdict: dict) -> float | None:
    """Взвешенное среднее применимых осей в [0,1]. None — применимых осей нет.

    Неприменимая ось исключается из ЗНАМЕНАТЕЛЯ, а не считается нейтральной:
    иначе отсутствие оборудования в тикете тянуло бы балл к середине.
    """
    total = weight_sum = 0.0
    for axis, item in verdict.items():
        if axis not in AXIS_WEIGHTS or not item.get("applicable"):
            continue
        weight = AXIS_WEIGHTS[axis]
        total += weight * normalize(item["score"])
        weight_sum += weight
    if weight_sum <= 0:
        return None
    return total / weight_sum


def axis_stats(verdicts: list[dict]) -> dict[str, dict]:
    """По оси: среднее в шкале 1–20, среднее нормированное, разброс, число оценок.

    Разброс сохраняется не для красоты: без него нельзя ни поставить порог
    гейта, ни заметить, что судья деградировал.
    """
    stats: dict[str, dict] = {}
    for axis in AXES:
        raw = [
            v[axis]["score"] for v in verdicts
            if axis in v and v[axis].get("applicable")
        ]
        if not raw:
            stats[axis] = {"mean_raw": None, "mean": None, "spread": None, "n": 0}
            continue
        stats[axis] = {
            "mean_raw": statistics.fmean(raw),
            "mean": normalize(statistics.fmean(raw)),
            "spread": (max(raw) - min(raw)) if len(raw) > 1 else 0.0,
            "n": len(raw),
        }
    return stats


def apply_caps(score: float, stats: dict[str, dict], hard: CheckResult) -> float:
    """Некомпенсируемые ограничения. Порядок: fatal → потолок диагноза → штраф.

    Полная компенсация между осями запрещена: неверный факт не отмывается
    форматом и живым тоном — так же, как по-кейсовый safety-гейт в
    golden.compare_to_baseline не отмывается хорошим средним.
    """
    if hard.failed:
        return 0.0
    diagnosis = stats.get("diagnosis", {}).get("mean_raw")
    if diagnosis is not None and diagnosis <= DIAGNOSIS_FAIL_MAX:
        score = min(score, DIAGNOSIS_FAIL_CAP)
    return max(0.0, min(1.0, score * hard.penalty))


def verdict_cache_key(
    *,
    candidate: str,
    reference: str,
    judge_model: str,
    temperature: float,
    reasoning_effort: str,
    repeat_index: int,
    rubric_version: str = RUBRIC_VERSION,
    schema_version: str = SCHEMA_VERSION,
    style_guide: str | None = None,
) -> str:
    """Полный ключ кеша вердикта (§11.6).

    Индекса повтора мало: без версии рубрики, модели и температуры кеш отдал бы
    вердикт, вынесенный по прежним правилам, и правка рубрики выглядела бы как
    отсутствие эффекта.
    """
    guide = _load_style_guide() if style_guide is None else style_guide
    parts = [
        hashlib.sha1(candidate.encode("utf-8")).hexdigest(),
        hashlib.sha1(reference.encode("utf-8")).hexdigest(),
        judge_model,
        rubric_version,
        hashlib.sha1(guide.encode("utf-8")).hexdigest(),
        f"{temperature}",
        reasoning_effort or "",
        schema_version,
        str(repeat_index),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


# --- вызов судьи -----------------------------------------------------------

async def _call_groq_judge(
    system: str, user: str, *, temperature: float = JUDGE_TEMPERATURE
) -> str:
    from ..config import config

    payload = {
        "model": config.optimizer_judge_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": 700,
        "response_format": {"type": "json_object"},
    }
    async with LLM_SEMAPHORE, aiohttp.ClientSession() as session:
        async with session.post(
            _URL,
            headers={"Authorization": f"Bearer {config.groq_api_key}"},
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()
    choices = data.get("choices")
    if not choices:
        error = data.get("error", {})
        msg = error.get("message") if isinstance(error, dict) else str(data)
        raise RuntimeError(f"Groq judge returned no choices: {msg}")
    return choices[0]["message"]["content"].strip()


async def judge_axes(
    history: str,
    title: str,
    candidate: str,
    op_answer: str,
    *,
    repeats: int = JUDGE_REPEATS,
    temperature: float = JUDGE_TEMPERATURE,
    _call_fn: CallFn | None = None,
    cache=None,
) -> dict:
    """k оценок одного кейса. Сырые вердикты сохраняются целиком.

    Один битый повтор из трёх не роняет кейс — усредняются валидные; если
    валидных нет, `score` = None и кейс в среднее не попадает.
    """
    from ..config import config

    call = _call_fn or _call_groq_judge
    system, user = build_judge_prompt(history, title, candidate, op_answer)

    verdicts: list[dict] = []
    invalid = 0
    for index in range(repeats):
        key = None
        if cache is not None:
            key = verdict_cache_key(
                candidate=candidate,
                reference=op_answer,
                judge_model=config.optimizer_judge_model,
                temperature=temperature,
                reasoning_effort=config.groq_reasoning_effort,
                repeat_index=index,
            )
            hit = cache.get(key)
            if hit is not None:
                parsed = parse_verdict(hit)
                if parsed is not None:
                    verdicts.append(parsed)
                    continue
        try:
            raw = await call(system, user, temperature=temperature)
        except Exception as exc:
            logger.warning("judge call %d failed: %s", index, exc)
            invalid += 1
            continue
        parsed = parse_verdict(raw)
        if parsed is None:
            invalid += 1
            logger.warning("judge verdict %d unparseable: %.120s", index, raw)
            continue
        verdicts.append(parsed)
        if cache is not None and key is not None:
            cache.put(key, raw)

    stats = axis_stats(verdicts)
    per_repeat = [aggregate_verdict(v) for v in verdicts]
    usable = [s for s in per_repeat if s is not None]
    return {
        "score": statistics.fmean(usable) if usable else None,
        "score_repeats": per_repeat,
        "spread": (max(usable) - min(usable)) if len(usable) > 1 else 0.0,
        "axes": stats,
        "verdicts": verdicts,
        "invalid": invalid,
        "judge_model": config.optimizer_judge_model,
        "temperature": temperature,
    }


async def judge_case(
    sample: dict,
    generated: str,
    *,
    repeats: int = JUDGE_REPEATS,
    temperature: float = JUDGE_TEMPERATURE,
    _call_fn: CallFn | None = None,
    _hard_fn=None,
    cache=None,
) -> dict:
    """Полная оценка одного кейса: hard-checks → k осей → нормировка → caps.

    Порядок обязателен (§11.4): fatal-провал экономит k вызовов судьи, а caps
    применяются ПОСЛЕ усреднения повторов, но ДО усреднения по кейсам.
    """
    hard_fn = _hard_fn or hard_check
    hard = hard_fn(generated, action_type=sample.get("action_type"))
    if hard.failed:
        return {
            "score": 0.0, "hard": hard, "axes": {}, "spread": 0.0,
            "invalid": 0, "verdicts": [], "judged": False,
        }

    result = await judge_axes(
        sample.get("history", ""), sample.get("title", ""), generated,
        sample.get("op_answer") or "", repeats=repeats, temperature=temperature,
        _call_fn=_call_fn, cache=cache,
    )
    if result["score"] is None:
        return {**result, "score": None, "hard": hard, "judged": False}
    return {
        **result,
        "score": apply_caps(result["score"], result["axes"], hard),
        "hard": hard,
        "judged": True,
    }


# --- holdout ---------------------------------------------------------------

async def holdout_detail(
    samples: list[dict],
    format_instructions: str,
    *,
    repeats: int = JUDGE_REPEATS,
    temperature: float = JUDGE_TEMPERATURE,
    _generate_fn=None,
    _judge_case_fn=None,
    cache=None,
) -> dict:
    """По-кейсовые скоры для парных дельт (§11.7) плюс агрегаты.

    Возвращает {"score": float, "cases": {sample_key: score}, "axes": {...},
    "judged": n, "invalid": n}. Сравнение baseline и кандидата обязано быть
    по-кейсовым: они считаются на ОДНИХ кейсах, и разница средних теряет
    информацию, которая как раз и решает — согласованно ли улучшение.
    """
    raw_generate = _generate_fn or _generate_answer
    judge_one = _judge_case_fn or judge_case

    memo: dict[tuple[str, str], str] = {}

    async def generate(history: str, title: str, instructions: str) -> str:
        key = (history, title)
        if key not in memo:
            memo[key] = await raw_generate(history, title, instructions)
        return memo[key]

    base = await combined_score(
        samples, format_instructions, max_samples=None, _generate_fn=generate
    )

    cases: dict[str, float] = {}
    axis_totals: dict[str, list[float]] = {axis: [] for axis in AXES}
    invalid = 0
    for sample in samples:
        if not sample.get("op_answer"):
            continue  # эталона нет — судить не по чему (та же логика, что not_comparable)
        try:
            generated = await generate(
                sample.get("history", ""), sample.get("title", ""), format_instructions
            )
            result = await judge_one(
                sample, generated, repeats=repeats, temperature=temperature, cache=cache
            )
        except Exception as exc:
            logger.warning("judge failed for sample %s: %s", sample.get("ticket_id"), exc)
            continue
        invalid += result.get("invalid", 0)
        if result.get("score") is None:
            continue
        key = str(sample.get("id") or sample.get("ticket_id") or len(cases))
        cases[key] = float(result["score"])
        for axis, stat in (result.get("axes") or {}).items():
            if stat.get("mean") is not None:
                axis_totals[axis].append(stat["mean"])

    judge_mean = statistics.fmean(cases.values()) if cases else None
    score = base if judge_mean is None else (
        (1 - JUDGE_WEIGHT) * base + JUDGE_WEIGHT * judge_mean
    )
    return {
        "score": score,
        "combined": base,
        "judge_mean": judge_mean,
        "cases": cases,
        "axes": {
            axis: (statistics.fmean(vals) if vals else None)
            for axis, vals in axis_totals.items()
        },
        "judged": len(cases),
        "invalid": invalid,
    }


async def holdout_score(
    samples: list[dict],
    format_instructions: str,
    *,
    _generate_fn=None,
    _judge_case_fn=None,
    cache=None,
) -> float:
    """Финальный скор на holdout в [0,1] — контракт для agent.py и харнесса."""
    detail = await holdout_detail(
        samples, format_instructions,
        _generate_fn=_generate_fn, _judge_case_fn=_judge_case_fn, cache=cache,
    )
    return detail["score"]
