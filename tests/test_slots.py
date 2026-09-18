"""Разбор того, что клиент уже сказал за весь диалог.

Случаи взяты из жалобы заказчика 2026-09-18 и из живых инцидентов, а не
придуманы: каждый тест ниже — либо дословная реплика клиента, либо форма,
которая в этих репликах встречалась.
"""

from __future__ import annotations

from datetime import date, time

from app.agent.slots import (
    Slots,
    asked_slots,
    build_context_hint,
    describe_slots,
    extract_slots,
    next_missing_slot,
)

TODAY = date(2026, 9, 18)   # пятница


def slots_of(text, history=None, **kwargs):
    return extract_slots(history or [], text, today=TODAY, **kwargs)


# --------------------------------------------------------------------------
# Дата
# --------------------------------------------------------------------------

def test_the_complaint_message_yields_the_date():
    """Дословная реплика из жалобы заказчика."""
    slots = slots_of("хочу арендовать 20 сентября, есть свободное время и сколько стоит?")

    assert slots.date == date(2026, 9, 20)


def test_a_relative_day_resolves_against_today():
    assert slots_of("на сегодня есть окошко?").date == TODAY
    assert slots_of("а завтра?").date == date(2026, 9, 19)


def test_a_date_said_three_messages_ago_is_still_known():
    """ГЛАВНЫЙ СЛУЧАЙ ЖАЛОБЫ: параметры берутся из ВСЕЙ переписки."""
    history = [
        {"role": "user", "content": "Здравствуйте, интересует гриль-домик на 20 сентября"},
        {"role": "assistant", "content": "Здравствуйте! Сколько вас будет?"},
        {"role": "user", "content": "нас 6теро"},
        {"role": "assistant", "content": "На сколько часов планируете?"},
    ]
    slots = slots_of("а цена какая?", history)

    assert slots.date == date(2026, 9, 20)
    assert slots.guests == 6
    assert slots.zone == "гриль"


def test_the_agents_own_words_are_not_read_as_the_clients():
    """Приветствие агента перечисляет все зоны разом — если считать это за
    «клиент назвал зону», условие выполнялось бы всегда."""
    history = [
        {"role": "assistant",
         "content": "Здравствуйте! У нас баня, купол, гриль-домик или шатёр на 20 сентября."},
    ]
    slots = slots_of("а что посоветуете?", history)

    assert slots.zone == ""
    assert slots.date is None


def test_the_last_mention_wins():
    history = [{"role": "user", "content": "нас будет 6"}]
    assert slots_of("хотя нет, нас 8 человек", history).guests == 8


# --------------------------------------------------------------------------
# Гости
# --------------------------------------------------------------------------

def test_guest_forms_from_real_messages():
    assert slots_of("нас 6теро").guests == 6
    assert slots_of("будет 12 человек").guests == 12
    assert slots_of("на 10 гостей").guests == 10
    assert slots_of("нас будет 4").guests == 4
    assert slots_of("приедем вшестером").guests == 6


def test_a_date_is_not_mistaken_for_a_guest_count():
    """«20 сентября» — двадцатое число, а не двадцать гостей. Единица
    измерения обязательна везде, кроме связки «нас N»."""
    assert slots_of("хотим на 20 сентября").guests is None


# --------------------------------------------------------------------------
# Длительность и время начала
# --------------------------------------------------------------------------

def test_duration_and_start_time_are_told_apart_by_the_preposition():
    """«на 4 часа» — длительность, «в 16 часов» — начало."""
    assert slots_of("на 4 часа").hours == 4
    assert slots_of("на 4 часа").start_time is None

    assert slots_of("приедем в 16 часов").start_time == time(16, 0)
    assert slots_of("приедем в 16 часов").hours is None


def test_a_range_gives_both_start_and_duration():
    slots = slots_of("баня с 13 до 17, вшестером")

    assert slots.start_time == time(13, 0)
    assert slots.hours == 4
    assert slots.guests == 6


def test_the_incident_message_with_a_spaced_time():
    """«сегодня 16 00» — дословно из инцидента 2026-09-01."""
    slots = slots_of("сегодня 16 00")

    assert slots.date == TODAY
    assert slots.start_time == time(16, 0)


def test_a_spaced_date_is_not_read_as_a_time():
    """«20 09» — двадцатое сентября, а не 20:09. Минуты у записи через
    пробел ограничены четвертями часа именно ради этого."""
    assert slots_of("приедем 20 09").start_time is None


def test_evening_hours_are_shifted():
    assert slots_of("мы в 6 вечера подъедем").start_time == time(18, 0)
    assert slots_of("в 9 утра можно?").start_time == time(9, 0)


def test_duration_in_words():
    assert slots_of("на пару часов").hours == 2
    assert slots_of("хотим на три часа").hours == 3


# --------------------------------------------------------------------------
# Зона
# --------------------------------------------------------------------------

def test_the_listing_zone_counts_as_told():
    slots = slots_of("а свободно?", listing_zone="Гриль-домик («Зелёная зона»)")

    assert slots.zone == "Гриль-домик («Зелёная зона»)"
    assert slots.zone_from_listing is True


def test_the_clients_own_word_beats_the_listing():
    """Пришёл с объявления бани, спрашивает про шатёр — верить надо клиенту."""
    slots = slots_of("а шатёр свободен?", listing_zone="Баня «Русский стиль»")

    assert slots.zone == "шатёр"
    assert slots.zone_from_listing is False


# --------------------------------------------------------------------------
# О чём уже спрашивали
# --------------------------------------------------------------------------

def test_asked_slots_reads_only_questions():
    history = [
        {"role": "assistant", "content": "Посчитаю на 20 сентября для 6 гостей."},
        {"role": "assistant", "content": "На сколько часов планируете?"},
    ]
    assert asked_slots(history) == ("hours",)


def test_asked_slots_finds_every_asked_parameter():
    history = [
        {"role": "assistant", "content": "На какое число планируете отдых?"},
        {"role": "user", "content": "20 сентября"},
        {"role": "assistant", "content": "Сколько вас будет?"},
        {"role": "user", "content": "не знаю пока"},
        {"role": "assistant", "content": "Со скольки планируете начать?"},
    ]
    assert set(asked_slots(history)) == {"date", "guests", "start_time"}


def test_the_clients_own_questions_are_not_counted():
    history = [{"role": "user", "content": "А на сколько часов можно взять?"}]
    assert asked_slots(history) == ()


# --------------------------------------------------------------------------
# Подсказка
# --------------------------------------------------------------------------

def test_no_hint_when_nothing_is_known():
    assert build_context_hint(Slots(), ()) is None


def test_the_hint_lists_what_is_known():
    slots = Slots(date=date(2026, 9, 20), guests=6)
    hint = build_context_hint(slots, ())

    assert "20 сентября" in hint
    assert "2026-09-20" in hint
    assert "гостей — 6" in hint
    assert "НЕ переспрашивай" in hint


def test_the_hint_names_only_the_questions_left_unanswered():
    """Про гостей спрашивали и получили ответ — это не повторный вопрос.
    Про часы спрашивали, ответа нет — вот его повторять нельзя."""
    slots = Slots(date=date(2026, 9, 20), guests=6)
    hint = build_context_hint(slots, ("guests", "hours"))

    assert "длительность" in hint
    assert "УЖЕ СПРАШИВАЛА" in hint
    # «гостей» встречается в перечне известного, но не в перечне повторов.
    repeated_block = hint.split("УЖЕ СПРАШИВАЛА")[1]
    assert "гостей" not in repeated_block


def test_the_hint_is_marked_as_internal():
    """Клиент этого текста видеть не должен — пометка обязательна."""
    hint = build_context_hint(Slots(date=date(2026, 9, 20)), ())
    assert hint.startswith("[служебное]")


def test_describe_slots_distinguishes_the_clients_word_from_the_catalogue_name():
    said = describe_slots(Slots(zone="гриль", zone_from_listing=False))
    listed = describe_slots(Slots(zone="Гриль-домик", zone_from_listing=True))

    assert "клиент сказал" in said
    assert "клиент сказал" not in listed


# --------------------------------------------------------------------------
# Чего не хватает
# --------------------------------------------------------------------------

def test_next_missing_slot_follows_priority_not_call_order():
    slots = Slots(guests=6)
    assert next_missing_slot(slots, needed=("hours", "date")) == "date"


def test_next_missing_slot_is_none_when_everything_is_known():
    slots = Slots(
        date=date(2026, 9, 20), guests=6, hours=4,
        start_time=time(13, 0), zone="гриль",
    )
    assert next_missing_slot(slots) is None
