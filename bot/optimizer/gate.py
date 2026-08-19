"""Apply-гейт: парные дельты вместо разницы двух средних (спека §11.7).

Baseline и кандидат считаются на ОДНИХ кейсах, значит сравнивать надо
по-кейсово: `dᵢ = scoreᵢ(candidate) − scoreᵢ(baseline)`. Разница средних теряет
ровно ту информацию, которая и решает — согласованное ли это улучшение или один
кейс вырос, а три просели.

Почему ушёл прежний `_MIN_IMPROVEMENT = 0.03`: он подобран под другую шкалу, но
хуже то, что по конструкции он видит только шум судьи и игнорирует

- разброс между кейсами (26 кейсов — маленькая выборка);
- множественный выбор: мы берём МАКСИМУМ из нескольких мутаций, а максимум
  смещён вверх даже при нулевом реальном эффекте (winner's curse).

Поэтому три условия вместо одного порога: средняя дельта выше практически
значимого минимума, нижняя граница bootstrap-интервала не в существенном
минусе, и ни одной критической пер-осевой регрессии.
"""
from __future__ import annotations

import statistics

import numpy as np

# Практически значимый минимум. НЕ измерен: поставлен как заведомо
# консервативный, пересчитывается замером шума судьи (повторный baseline-прогон
# с отключённым кешем вердиктов, --resample-judge).
MIN_MEAN_DELTA = 0.02

# Насколько нижняя граница интервала может уйти в минус: небольшой минус при
# положительном среднем — нормальная картина на 26 кейсах, существенный — нет.
MAX_CI_DOWNSIDE = -0.02

CI_ALPHA = 0.05
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 42

# Падение оси относительно baseline, которое считается регрессией. Диагноз и
# полезность критичны: по ним запрет apply даже при выросшем среднем.
CRITICAL_AXES = ("diagnosis", "actionability")
AXIS_REGRESSION_TOLERANCE = 0.05

MIN_PAIRED_CASES = 5


def paired_deltas(baseline: dict[str, float], candidate: dict[str, float]) -> list[float]:
    """Дельты по кейсам, присутствующим в ОБОИХ прогонах (иначе пары нет)."""
    shared = sorted(set(baseline) & set(candidate))
    return [candidate[key] - baseline[key] for key in shared]


def bootstrap_ci_low(
    deltas: list[float],
    *,
    alpha: float = CI_ALPHA,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> float | None:
    """Нижняя граница доверительного интервала средней дельты.

    Фиксированный seed: гейт обязан быть воспроизводимым, иначе один и тот же
    кандидат то проходит, то нет.
    """
    if len(deltas) < 2:
        return None
    rng = np.random.default_rng(seed)
    sample = np.asarray(deltas, dtype=np.float64)
    means = rng.choice(sample, size=(resamples, sample.size), replace=True).mean(axis=1)
    return float(np.quantile(means, alpha))


def axis_regressions(
    baseline_axes: dict[str, float | None],
    candidate_axes: dict[str, float | None],
    *,
    axes: tuple[str, ...] = CRITICAL_AXES,
    tolerance: float = AXIS_REGRESSION_TOLERANCE,
) -> list[str]:
    """Критические оси, просевшие больше допуска."""
    regressed = []
    for axis in axes:
        before, after = baseline_axes.get(axis), candidate_axes.get(axis)
        if before is None or after is None:
            continue
        if after < before - tolerance:
            regressed.append(f"{axis}: {before:.3f} → {after:.3f}")
    return regressed


def apply_gate(
    baseline: dict,
    candidate: dict,
    *,
    min_mean_delta: float = MIN_MEAN_DELTA,
    max_ci_downside: float = MAX_CI_DOWNSIDE,
    min_cases: int = MIN_PAIRED_CASES,
) -> dict:
    """Решение о применении кандидата. Принимает результаты judge.holdout_detail.

    Возвращает {"passed": bool, "mean_delta", "ci_low", "n", "regressions",
    "reasons": [...]} — причины отказа перечисляются все, а не первая: отчёт
    оператору должен объяснять, чего именно не хватило.
    """
    deltas = paired_deltas(baseline.get("cases", {}), candidate.get("cases", {}))
    mean_delta = statistics.fmean(deltas) if deltas else None
    ci_low = bootstrap_ci_low(deltas)
    regressions = axis_regressions(baseline.get("axes", {}), candidate.get("axes", {}))

    reasons: list[str] = []
    if len(deltas) < min_cases:
        reasons.append(
            f"мало парных кейсов: {len(deltas)} < {min_cases}"
        )
    if mean_delta is None or mean_delta <= min_mean_delta:
        reasons.append(
            f"средняя дельта {0.0 if mean_delta is None else mean_delta:+.3f} "
            f"не выше минимума {min_mean_delta:+.3f}"
        )
    if ci_low is not None and ci_low < max_ci_downside:
        reasons.append(
            f"нижняя граница интервала {ci_low:+.3f} ниже допустимой "
            f"{max_ci_downside:+.3f} — улучшение не согласовано по кейсам"
        )
    if regressions:
        reasons.append("регрессия критических осей: " + "; ".join(regressions))

    return {
        "passed": not reasons,
        "mean_delta": mean_delta,
        "ci_low": ci_low,
        "n": len(deltas),
        "regressions": regressions,
        "reasons": reasons,
    }
