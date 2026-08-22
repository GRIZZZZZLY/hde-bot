"""Бюджет запроса под TPM: тикет не должен остаться без суммарки из-за 413.

Прод-лог за сутки: 50 ошибок «413 Request too large» и 123 отказа 429 при лимите
8000 TPM. Провайдер считает против лимита prompt + max_tokens, поэтому здесь
проверяется и обрезка входа, и то, что резерв на выход не раздут.
"""
import pytest

from bot import ai_summary
from bot.ai_summary import (
    _MAX_HISTORY_CHARS,
    _MAX_PROMPT_CHARS,
    _RAG_EXCERPT_LIMIT,
    _budget_history,
    _fit_prompt,
)
from bot.config import config
from bot.hde_api import HDEPost, HDETicketInfo


def _info(client_id="7"):
    return HDETicketInfo(
        client_id=client_id, client_name="Клиент", owner_id="99", owner_name="Оператор"
    )


def _posts(n: int, size: int, client_id="7"):
    """n сообщений по size символов: первое клиентское, дальше вперемешку."""
    out = []
    for i in range(n):
        out.append(HDEPost(
            post_id=i + 1,
            user_id=client_id if i % 2 == 0 else "99",
            text=f"сообщение{i} " + "ы" * size,
            date_created="2026-08-22 10:00:00",
        ))
    return out


# --- бюджет истории ---------------------------------------------------------

def test_history_trimmed_to_budget():
    history = _budget_history(_posts(40, 500), _info())
    assert len(history) <= _MAX_HISTORY_CHARS + 200  # +маркер пропуска


def test_history_keeps_first_client_message_and_marker():
    history = _budget_history(_posts(40, 500), _info())
    assert "сообщение0" in history          # первый вопрос клиента остался
    assert "пропущено" in history           # и видно, что середина вырезана


def test_short_history_untouched():
    """Обычный тикет не должен меняться вообще — это не оптимизация ради оптимизации."""
    posts = _posts(4, 50)
    assert _budget_history(posts, _info()) == ai_summary._build_history_text(posts, _info())


def test_history_never_cuts_mid_message():
    """Обрубок реплики модель достраивает и выдумывает — режем целыми."""
    history = _budget_history(_posts(40, 500), _info())
    for line in history.splitlines():
        if line.startswith(("Клиент:", "Сотрудник:", "Коллега:")):
            assert line.endswith("ы")  # реплика доехала до конца


# --- бюджет всего запроса ---------------------------------------------------

def _assemble(examples):
    """Имитация _build_system_prompt: статика + примеры RAG."""
    base = "СТАТИКА" * 1000  # ~7k символов, как реальные инструкции
    if not examples:
        return base
    return base + "".join(examples)


def test_fit_prompt_keeps_small_request_as_is():
    system, history = _fit_prompt("коротко", "история", _assemble)
    assert (system, history) == ("коротко", "история")


def test_fit_prompt_drops_rag_first():
    """RAG — самое объёмное и самое заменимое; история важнее."""
    examples = ["П" * 5000, "Р" * 5000]
    system = _assemble(examples)
    history = "И" * 3000

    fitted_system, fitted_history = _fit_prompt(system, history, _assemble)

    assert len(fitted_system) + len(fitted_history) <= _MAX_PROMPT_CHARS
    assert fitted_history == history          # историю не тронули
    assert "П" not in fitted_system           # примеры ушли


def test_fit_prompt_trims_history_only_when_dropping_rag_is_not_enough():
    huge_static = "С" * (_MAX_PROMPT_CHARS - 1000)

    def assemble(examples):
        return huge_static if not examples else huge_static + "".join(examples)

    system, history = _fit_prompt(
        assemble(["П" * 5000]), "И" * 9000, assemble
    )

    assert len(system) + len(history) <= _MAX_PROMPT_CHARS + 100
    assert "начало переписки пропущено" in history
    assert history.rstrip().endswith("И")     # оставлены СВЕЖИЕ реплики


# --- резерв на выход --------------------------------------------------------

def test_summary_max_tokens_is_not_bloated():
    """max_tokens резервируется против TPM. Прод-замер: макс. ответ 612 символов."""
    assert 300 <= config.groq_summary_max_tokens <= 1200


@pytest.mark.parametrize("value,expected", [("", 700), ("512", 512)])
def test_summary_max_tokens_from_env(monkeypatch, value, expected):
    monkeypatch.setenv("GROQ_SUMMARY_MAX_TOKENS", value)
    from bot.config import Config
    assert Config.from_env().groq_summary_max_tokens == expected


def test_rag_excerpt_limit_matches_agent():
    """Легаси-путь и агент обрезают примеры одинаково — иначе один из них снова 413."""
    from bot.agent.context import _EXCERPT_LIMIT
    assert _RAG_EXCERPT_LIMIT == _EXCERPT_LIMIT
