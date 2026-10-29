"""Рубеж «ответ не обрывается» (app/agent/closing.py, заказчик 29.10)."""

from __future__ import annotations

from datetime import date, time
from types import SimpleNamespace

import pytest

from app.agent.closing import (
    ASK_DATE,
    ASK_GUESTS,
    ASK_TIME,
    OFFER_HOLD,
    ensure_next_step,
    next_step,
)


def _slots(**kw):
    base = dict(date=None, guests=None, hours=None, start_time=None, zone=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_without_date_we_ask_for_date_and_guests():
    text, added = ensure_next_step("Русская баня - 2500 ₽/час в будни.", "сколько стоит баня", _slots(), False)
    assert added
    assert text.endswith(ASK_DATE)


def test_with_date_but_no_time_we_ask_for_time():
    assert next_step(_slots(date=date(2026, 11, 1)), False) == ASK_TIME


def test_with_date_and_time_but_no_guests_we_ask_for_guests():
    s = _slots(date=date(2026, 11, 1), start_time=time(14, 0), hours=3)
    assert next_step(s, False) == ASK_GUESTS


def test_after_a_quote_we_offer_to_hold_by_prepayment():
    text, added = ensure_next_step("Итого 7500 ₽.", "на 3 часа", _slots(), True)
    assert added
    assert text.endswith(OFFER_HOLD)
    assert "50%" in OFFER_HOLD


def test_a_reply_that_already_asks_is_left_alone():
    text, added = ensure_next_step("На какое время смотрите?", "есть свободно?", _slots(), False)
    assert not added
    assert text == "На какое время смотрите?"


def test_a_client_who_says_thanks_is_not_pushed():
    text, added = ensure_next_step("Пожалуйста, будем ждать!", "спасибо, подумаю", _slots(), True)
    assert not added


@pytest.mark.parametrize("reply", [
    "Для брони напишите, пожалуйста, имя и телефон.",
    "Секунду, уточню свободное время и напишу.",
])
def test_a_reply_that_already_leads_somewhere_gets_no_second_tail(reply):
    assert ensure_next_step(reply, "есть время?", _slots(), True) == (reply, False)


def test_empty_text_is_not_touched():
    assert ensure_next_step("", "привет", _slots(), False) == ("", False)
