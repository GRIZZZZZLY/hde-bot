"""Auto-fill HDE ticket custom fields after AI summary.

Field & option IDs discovered empirically by scanning 450 production
tickets (HDE API does not expose select-field option lists).
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Custom field IDs (HDE /custom_fields/ endpoint)
FIELD_OKRUZHENIE = "2"
FIELD_KLASSIFIKACIYA = "3"
FIELD_ROL = "24"

# Fixed option IDs
KLASSIFIKACIYA_OBORUDOVANIE = "20"  # "Оборудование"
ROL_NE_VAZHNO = "197"               # "Не важно"

# Окружение: option_id -> human label (field_id 2)
OKRUZHENIE_OPTIONS: dict[str, str] = {
    "15": "Интернет витрина",
    "11": "POS",
    "146": "Эвотор",
    "148": "АКСИ \\ AQSI",
    "12": "Биллинг",
    "190": "Wallet",
    "10": "Админ панель",
    "145": "Атол",
    "17": "API",
    "140": "ТГ-Бот",
    "13": "Florist",
    "151": "Яндекс Пэй",
    "150": "INPAS \\ ИНПАС",
    "46": "Менеджер",
    "149": "SBER \\ СБЕР",
    "14": "Другое",
    "153": "Принтер Этикеток",
    "56": "Интеграция",
    "152": "Принтер Чеков",
    "157": "WEB касса \\ Веб касса",
    "156": "Viki Print \\ Вики принт",
}
