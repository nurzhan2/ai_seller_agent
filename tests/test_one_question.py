"""Рубеж одного вопроса: обрезка не должна ломать текст.

Отдельный файл, а не пара тестов в test_agent.py, по той же причине, по
которой отдельный модуль: это правило про ФОРМУ ответа, и оно обязано
работать на любом тексте, а не только на том, который вернула модель в
конкретном сценарии.
"""

from __future__ import annotations

import pytest

from app.agent.loop import (
    AVAILABILITY_GUARD_HANDOFF,
    AVAILABILITY_GUARD_LADDER,
    GUARD_RAIL_FALLBACK,
    HANDED_TO_MANAGER,
)
from app.agent.one_question import (
    count_questions,
    normalize_question_marks,
    split_sentences,
    trim_to_one_question,
)
from app.pipeline import IMAGE_WITHOUT_TEXT_REPLY


# --------------------------------------------------------------------------
# Счёт вопросов
# --------------------------------------------------------------------------

def test_a_single_question_is_counted_once():
    assert count_questions("На какое число планируете?") == 1


def test_repeated_marks_are_one_question():
    """«Правда???» — один вопрос. Считать знаки напрямую нельзя."""
    assert count_questions("Правда??? Приезжайте.") == 1
    assert trim_to_one_question("Правда??? Приезжайте.") == "Правда? Приезжайте."


def test_a_long_question_with_options_is_one_question():
    """Уточнение бани из промта: знак один, вопрос один, резать нечего."""
    text = "Вы про какую баню — «Русский стиль», «Гараж» или «Рыцарскую»?"
    assert count_questions(text) == 1
    assert trim_to_one_question(text) == text


def test_two_questions_are_counted_as_two():
    assert count_questions("На какое число? И на сколько гостей?") == 2


def test_no_questions_at_all():
    assert count_questions("Тариф 2000 ₽ в час.") == 0
    assert trim_to_one_question("Тариф 2000 ₽ в час.") == "Тариф 2000 ₽ в час."


@pytest.mark.parametrize("text", ["", None])
def test_empty_input_does_not_crash(text):
    assert count_questions(text) == 0
    assert trim_to_one_question(text) == ""


# --------------------------------------------------------------------------
# Обрезка
# --------------------------------------------------------------------------

def test_a_reply_with_one_question_is_untouched():
    """Главная форма ответа из требования заказчика — проходит как есть."""
    text = (
        "Гриль-домик на 20 сентября свободен с 13:00. По цене: выходной "
        "тариф 2000 ₽/час, минимум 3 часа. На сколько часов планируете?"
    )
    assert trim_to_one_question(text) == text


def test_the_second_question_is_cut_off():
    text = "На какое число планируете? И на сколько гостей?"
    assert trim_to_one_question(text) == "На какое число планируете?"


def test_an_explanation_belonging_to_the_cut_question_does_not_dangle():
    """ГЛАВНЫЙ СЛУЧАЙ: «вопрос — вопрос — пояснение ко второму».

    Если вырезать только второй вопрос, пояснение «От трёх — это минимум»
    осталось бы висеть после первого вопроса, к которому оно не относится.
    Режем по началу второго вопроса, вместе со всем, что за ним.
    """
    text = "На какое число планируете? И на сколько часов? От трёх — это минимум."
    trimmed = trim_to_one_question(text)

    assert trimmed == "На какое число планируете?"
    assert "минимум" not in trimmed


def test_a_statement_between_the_kept_and_the_cut_question_survives():
    """Пояснение ПОСЛЕ оставленного вопроса — это ответ по существу, и
    терять его нельзя: иначе рубеж лечил бы допрос молчанием про деньги."""
    text = (
        "Свободно 20 сентября. На сколько часов планируете? "
        "Выходной тариф 2000 ₽/час, минимум 3 часа. А сколько вас будет?"
    )
    trimmed = trim_to_one_question(text)

    assert trimmed == (
        "Свободно 20 сентября. На сколько часов планируете? "
        "Выходной тариф 2000 ₽/час, минимум 3 часа."
    )
    assert count_questions(trimmed) == 1
    assert "2000" in trimmed


def test_three_questions_leave_exactly_one():
    text = "На какое число? На сколько часов? Сколько гостей?"
    assert trim_to_one_question(text) == "На какое число?"


def test_paragraph_breaks_are_preserved():
    """Модель разбивает ответ на абзацы — обрезка не должна их склеивать."""
    text = "Здравствуйте!\n\nНа какое число планируете?\n\nИ на сколько гостей?"
    assert trim_to_one_question(text) == "Здравствуйте!\n\nНа какое число планируете?"


def test_trimmed_text_never_becomes_empty():
    assert trim_to_one_question("? ?") != ""


def test_split_sentences_reports_offsets_into_the_original_text():
    text = "Первое. Второе?"
    parts = split_sentences(text)

    assert [chunk for _, chunk in parts] == ["Первое.", "Второе?"]
    assert [text[offset:offset + len(chunk)] for offset, chunk in parts] == [
        "Первое.", "Второе?",
    ]


def test_normalize_touches_nothing_but_repeated_marks():
    assert normalize_question_marks("Как дела?! Хорошо.") == "Как дела?! Хорошо."


# --------------------------------------------------------------------------
# Собственные тексты системы
# --------------------------------------------------------------------------

def test_the_systems_own_replies_are_never_trimmed():
    """Подстановки рубежей, отбивки и шаблонный ответ на фото обязаны
    проходить рубеж одного вопроса нетронутыми — иначе он режет сам себя.

    Тот же принцип, что и у test_the_guard_replies_do_not_trip_the_guard_
    itself в test_agent.py: подстановка не должна попадать под собственное
    правило.
    """
    own_texts = [
        *(text for ladder in AVAILABILITY_GUARD_LADDER.values() for text in ladder),
        AVAILABILITY_GUARD_HANDOFF,
        GUARD_RAIL_FALLBACK,
        HANDED_TO_MANAGER,
        IMAGE_WITHOUT_TEXT_REPLY,
    ]
    for text in own_texts:
        assert count_questions(text) <= 1, text
        assert trim_to_one_question(text) == text, text


def test_no_ladder_question_hides_a_second_question_behind_the_conjunction():
    """«на какое число И на сколько гостей?» — знак один, вопроса два.

    Ровно эта формулировка стояла первой ступенью лестницы и была тем
    самым допросом, на который жаловался заказчик. Счётчик вопросительных
    знаков её не ловит, поэтому проверяется отдельно и по смыслу.
    """
    for ladder in AVAILABILITY_GUARD_LADDER.values():
        for text in ladder:
            assert " и на " not in text.lower(), text
