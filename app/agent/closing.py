"""Рубеж «ответ не обрывается»: каждый ответ ведёт к брони.

Заказчик 2026-10-29 (Максим Отт): «Часто отвечает на вопрос и замолкает: не
спрашивает дату и сколько человек, не называет итоговую сумму и не
предлагает забронировать. Нужно, чтобы каждый ответ заканчивался вопросом
про дату и гостей или предложением закрепить время по предоплате».

Промт это просит, модель исполняет через раз — поэтому код: если в ответе
нет вопроса, дописываем ОДИН следующий шаг по тому, что клиент уже сказал.
Не трогаем прощания и паузы клиента («спасибо, напишу позже») — там вопрос
выглядит навязчиво.
"""

from __future__ import annotations

import re
from typing import Any

CLIENT_PAUSE = re.compile(
    r"\b(спасибо|благодар\w*|напишу|подума\w*|посовету\w*|уточн\w* и|отмен\w*|"
    r"не надо|не нужно|позже|до свидания|всего доброго)",
    re.IGNORECASE,
)

# Ответ уже ведёт дальше без знака вопроса: просьба («напишите имя и
# телефон») или обещание вернуться («уточню и напишу») - второй хвост лишний.
ALREADY_LEADS = re.compile(
    r"\b(напишите|оставьте|пришлите|подскажите|скажите|выберите|уточню|"
    r"проверю|напишу|вернусь|сообщу|секунду|минуту)\b",
    re.IGNORECASE,
)

ASK_DATE = "На какую дату планируете и сколько будет гостей?"
ASK_GUESTS = "Сколько вас будет?"
ASK_TIME = "Во сколько хотите приехать и на сколько часов?"
OFFER_HOLD = "Закрепить это время за вами? Бронь подтверждается по предоплате 50%."


def next_step(slots: Any, priced: bool) -> str:
    if priced:
        return OFFER_HOLD
    if getattr(slots, "date", None) is None:
        return ASK_DATE
    if getattr(slots, "start_time", None) is None or not getattr(slots, "hours", None):
        return ASK_TIME
    if getattr(slots, "guests", None) is None:
        return ASK_GUESTS
    return OFFER_HOLD


def ensure_next_step(text: str, client_text: str, slots: Any, priced: bool) -> tuple[str, bool]:
    """(текст, дописали ли шаг)."""
    body = (text or "").rstrip()
    if not body or "?" in body or ALREADY_LEADS.search(body):
        return text, False
    if CLIENT_PAUSE.search(client_text or ""):
        return text, False
    return f"{body}\n\n{next_step(slots, priced)}", True
