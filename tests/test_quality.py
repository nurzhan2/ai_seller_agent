"""Тесты харнесса качества.

Главное здесь — доказать, что жёсткие проверки ЛОВЯТ нарушения. Прогон,
показывающий 100% чистых ходов, ничего не стоит, если правила не срабатывают
на заведомо плохом ответе. Поэтому на каждое правило есть пара: плохой ответ
должен падать, хороший — проходить.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.quality.asserts import (
    MAX_REPLY_LENGTH,
    TurnUnderTest,
    check_turn,
    summarize,
)
from app.quality.judge import Judge, JudgeScore, JudgeSummary
from app.quality.report import RunResult, TurnRecord, compare_to_baseline, render_html

ZONES = ["bath_russian", "dome_bags", "tent"]


def rules(turn: TurnUnderTest) -> set[str]:
    return {v.rule for v in check_turn(turn)}


# --------------------------------------------------------------------------
# 1. Цена только через инструмент
# --------------------------------------------------------------------------

def test_price_without_tool_call_is_caught():
    turn = TurnUnderTest(text="Баня стоит 3500 ₽ в час.", tool_calls=[])
    assert "price_without_tool" in rules(turn)


def test_price_with_tool_call_passes():
    turn = TurnUnderTest(
        text="3 ч × 3500 ₽ = 10500 ₽",
        tool_calls=["calculate_price"],
        tool_amounts=["10500", "3500"],
    )
    assert "price_without_tool" not in rules(turn)


def test_text_without_money_needs_no_tool():
    turn = TurnUnderTest(text="Здравствуйте! На какое число планируете?")
    assert rules(turn) == set()


# --------------------------------------------------------------------------
# 2. Названные суммы — из ответов инструментов
#
# Правило переехало в app/agent/loop.py и работает в проде рубежом перед
# отправкой; здесь оно только вызывается. Заодно оно перестало быть правилом
# «любая сумма обязана равняться total»: полный прогон 2026-09-02 показал два
# ПРАВИЛЬНЫХ ответа, которые та редакция рубила, — оба ниже.
# --------------------------------------------------------------------------

def test_invented_amount_is_caught():
    """Агент назвал сумму, которой движок не возвращал."""
    turn = TurnUnderTest(
        text="Выйдет 9000 ₽.",
        tool_calls=["calculate_price"],
        tool_amounts=["10500"],
    )
    assert "price_mismatch" in rules(turn)


def test_matching_amount_passes():
    turn = TurnUnderTest(
        text="Итого 10500 ₽ за 3 часа.",
        tool_calls=["calculate_price"],
        tool_amounts=["10500"],
    )
    assert "price_mismatch" not in rules(turn)


def test_the_prepayment_is_not_an_invented_amount():
    """«Юрта на сутки — 4000 ₽, предоплата 3000 ₽» — из прогона 2026-09-02.

    3000 — это `prepayment` того же расчёта. Прежнее правило сверяло только
    с `total` и объявляло предоплату выдумкой.
    """
    turn = TurnUnderTest(
        text="Юрта на сутки — 4000 ₽, предоплата 3000 ₽.",
        tool_calls=["calculate_price"],
        tool_amounts=["4000", "3000"],
    )
    assert "price_mismatch" not in rules(turn)


def test_an_extra_priced_by_its_own_tool_is_not_invented_either():
    """«Набор из 6 штук за 500 ₽» — цена допа из get_extras, оттуда же из
    прогона. Расчёта аренды в этом ходу не было вовсе, и требовать
    совпадения с ним не с чем."""
    turn = TurnUnderTest(
        text="Да, шампуры есть — набор из 6 штук за 500 ₽.",
        tool_calls=["get_extras"],
        tool_amounts=["500", "6"],
    )
    assert "price_mismatch" not in rules(turn)


# --------------------------------------------------------------------------
# 3. При blocked — ни одной суммы
# --------------------------------------------------------------------------

def test_amount_after_blocked_is_caught():
    turn = TurnUnderTest(
        text="Ориентировочно 14500 ₽, но уточню у менеджера.",
        tool_calls=["calculate_price"],
        quote_statuses=["blocked"],
    )
    assert "amount_after_blocked" in rules(turn)


def test_blocked_without_numbers_passes():
    turn = TurnUnderTest(
        text="Уточню у менеджера и вернусь с ответом.",
        tool_calls=["calculate_price", "escalate_to_human"],
        quote_statuses=["blocked"],
    )
    assert "amount_after_blocked" not in rules(turn)


def test_bare_number_after_blocked_is_also_caught():
    """Даже без значка рубля — «около 9500» это названная сумма."""
    turn = TurnUnderTest(
        text="Обычно это около 9500, но нужно уточнить.",
        tool_calls=["calculate_price"],
        quote_statuses=["blocked"],
    )
    assert "amount_after_blocked" in rules(turn)


# --------------------------------------------------------------------------
# 4. Обещание брони
# --------------------------------------------------------------------------

def test_booking_promise_without_manager_is_caught():
    turn = TurnUnderTest(text="Отлично, забронировал на субботу!")
    assert "booking_promise" in rules(turn)


def test_booking_with_manager_mention_passes():
    turn = TurnUnderTest(
        text="Придержу это время, менеджер свяжется и подтвердит."
    )
    assert "booking_promise" not in rules(turn)


# --------------------------------------------------------------------------
# 5. Реквизиты
# --------------------------------------------------------------------------

def test_phone_number_is_caught():
    turn = TurnUnderTest(text="Переведите на 89265631898, пожалуйста.")
    assert "phone_leak" in rules(turn)


def test_bank_name_is_caught():
    turn = TurnUnderTest(text="Оплата на Озон Банк.")
    assert "bank_leak" in rules(turn)


def test_card_number_is_caught():
    turn = TurnUnderTest(text="Карта 4276 1600 1234 5678")
    assert "card_leak" in rules(turn)


def test_clean_text_has_no_credential_flags():
    turn = TurnUnderTest(text="По оплате с вами свяжется менеджер.")
    assert rules(turn) == set()


# --------------------------------------------------------------------------
# 6. Производная ставка при акции
# --------------------------------------------------------------------------

def test_derived_hourly_rate_under_promo_is_caught():
    """6 часов за 7500 ₽ не должны превращаться в «1250 ₽ в час»."""
    turn = TurnUnderTest(
        text="Получается 1250 ₽ в час.",
        tool_calls=["calculate_price"],
        tool_amounts=["1250"],
        applied_promo="sixth_hour_free",
    )
    assert "derived_rate" in rules(turn)


def test_line_breakdown_under_promo_passes():
    turn = TurnUnderTest(
        text="5 ч × 1500 ₽ = 7500 ₽, шестой час в подарок.",
        tool_calls=["calculate_price"],
        tool_amounts=["7500", "1500"],
        applied_promo="sixth_hour_free",
        concession_granted=True,
    )
    assert "derived_rate" not in rules(turn)


# --------------------------------------------------------------------------
# 7. Скидка только после request_concession
# --------------------------------------------------------------------------

def test_unauthorised_discount_is_caught():
    turn = TurnUnderTest(text="Могу сделать скидку, если решите сегодня.")
    assert "unauthorised_discount" in rules(turn)


def test_authorised_discount_passes():
    turn = TurnUnderTest(
        text="Могу зафиксировать эту скидку, если бронируем сегодня.",
        concession_granted=True,
    )
    assert "unauthorised_discount" not in rules(turn)


# --------------------------------------------------------------------------
# 8-9. Длина и количество вопросов
# --------------------------------------------------------------------------

def test_too_long_reply_is_caught():
    turn = TurnUnderTest(text="а" * (MAX_REPLY_LENGTH + 1))
    assert "too_long" in rules(turn)


def test_reply_at_limit_passes():
    turn = TurnUnderTest(text="а" * MAX_REPLY_LENGTH)
    assert "too_long" not in rules(turn)


def test_questionnaire_is_caught():
    """Анкета из трёх вопросов — способ потерять клиента."""
    turn = TurnUnderTest(text="Какая дата? Сколько гостей? Во сколько приедете?")
    assert "too_many_questions" in rules(turn)


def test_single_question_passes():
    turn = TurnUnderTest(text="Подскажите, на какое число планируете?")
    assert "too_many_questions" not in rules(turn)


# --------------------------------------------------------------------------
# 10. Выдуманные зоны
# --------------------------------------------------------------------------

def test_invented_service_is_caught():
    turn = TurnUnderTest(text="У нас есть бассейн и хамам.", known_zone_ids=ZONES)
    assert "unknown_zone" in rules(turn)


def test_real_zones_pass():
    turn = TurnUnderTest(
        text="Есть баня, купол и шатёр — что вам ближе?", known_zone_ids=ZONES
    )
    assert "unknown_zone" not in rules(turn)


# --------------------------------------------------------------------------
# Сводка и отчёт
# --------------------------------------------------------------------------

def test_summarize_counts_rules():
    violations = check_turn(
        TurnUnderTest(text="Забронировал! Переведите на 89265631898.")
    )
    counts = summarize(violations)
    assert counts["phone_leak"] == 1
    assert counts["booking_promise"] == 1


def test_report_puts_failures_first():
    good = TurnRecord("d1", "баня", "вопрос", "ответ менеджера", "чистый ответ")
    bad = TurnRecord(
        "d2", "купол", "вопрос", "ответ", "Забронировал!",
        violations=check_turn(TurnUnderTest(text="Забронировал!")),
    )
    result = RunResult(turns=[good, bad])
    html = render_html(result)
    assert html.index("d2") < html.index("d1"), "FAIL должны быть сверху"
    assert "booking_promise" in html


def test_report_marks_offline_run():
    html = render_html(RunResult(turns=[], model_available=False))
    assert "без обращения к модели" in html


def test_run_result_pass_rate():
    good = TurnRecord("d1", "z", "c", "m", "a")
    bad = TurnRecord("d2", "z", "c", "m", "a", violations=check_turn(TurnUnderTest(text="Забронировал!")))
    data = RunResult(turns=[good, bad]).as_json()
    assert data["total_turns"] == 2
    assert data["failed_turns"] == 1
    assert data["pass_rate"] == 0.5


# --------------------------------------------------------------------------
# Сравнение с эталоном
# --------------------------------------------------------------------------

def test_regression_in_pass_rate_is_reported():
    problems = compare_to_baseline(
        {"pass_rate": 0.8, "violations_by_rule": {}},
        {"pass_rate": 1.0, "violations_by_rule": {}},
    )
    assert any("чистых ходов упала" in p for p in problems)


def test_new_violation_type_is_reported():
    problems = compare_to_baseline(
        {"pass_rate": 1.0, "violations_by_rule": {"phone_leak": 2}},
        {"pass_rate": 1.0, "violations_by_rule": {}},
    )
    assert any("phone_leak" in p for p in problems)


def test_no_regression_reports_nothing():
    same = {"pass_rate": 1.0, "violations_by_rule": {}}
    assert compare_to_baseline(same, same) == []


def test_judge_score_drop_is_reported():
    problems = compare_to_baseline(
        {"pass_rate": 1.0, "violations_by_rule": {}, "judge": {"overall": 3.5}},
        {"pass_rate": 1.0, "violations_by_rule": {}, "judge": {"overall": 4.2}},
    )
    assert any("средний балл" in p for p in problems)


# --------------------------------------------------------------------------
# Судья
# --------------------------------------------------------------------------

class _FailingClient:
    class messages:
        @staticmethod
        async def create(**kwargs):
            raise RuntimeError("api down")


class _BadJsonClient:
    class messages:
        @staticmethod
        async def create(**kwargs):
            class B:
                type = "text"
                text = "я не умею в json"

            class R:
                content = [B()]

            return R()


async def test_judge_survives_api_failure():
    """Недоступный судья не должен ронять прогон."""
    score = await Judge(_FailingClient()).score("вопрос", "ответ", "ответ агента")
    assert score.failed is True


async def test_judge_survives_non_json_reply():
    score = await Judge(_BadJsonClient()).score("вопрос", "ответ", "ответ агента")
    assert score.failed is True


async def test_judge_skips_empty_agent_reply():
    score = await Judge(_FailingClient()).score("вопрос", "ответ", "   ")
    assert score.failed is True
    assert "не ответил" in score.why


def test_failed_scores_are_excluded_from_averages():
    summary = JudgeSummary(
        scores=[
            JudgeScore(tone=4, usefulness=4, deal_progress=4, naturalness=4),
            JudgeScore(failed=True),
        ]
    )
    data = summary.as_dict()
    assert data["judged"] == 1
    assert data["overall"] == 4.0


def test_empty_summary_does_not_divide_by_zero():
    assert JudgeSummary().as_dict()["overall"] == 0.0
