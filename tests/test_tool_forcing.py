"""Какой инструмент принуждается на сообщении клиента.

Заведено по разбору 2026-09-02: DeepSeek вызывает инструменты примерно в
половине ходов, промт на это не влияет (усиливали дважды), а адресный
tool_choice вызов включает.

ЗДЕСЬ ПРОВЕРЯЕТСЯ ТОЛЬКО НАШЕ РЕШЕНИЕ — какое имя мы пошлём. Исполнит ли его
провайдер, эти тесты не знают и знать не могут: DeepSeek имя не исполняет, он
лишь понимает адресный tool_choice как «позови что-нибудь». Это меряется
живым прогоном (scripts/probe_tool_forcing.py), а не pytest — цифры и разбор
в докстринге app/agent/tool_forcing.py.
"""

from app.agent.tool_forcing import ZONE_WORDS, forced_tool_for


# --------------------------------------------------------------------------
# Спрашивают про занятость — принуждаем
# --------------------------------------------------------------------------

def test_the_incident_message_forces_the_calendar():
    """Текст клиента из инцидента 2026-09-01, дословно. На нём модель три
    раза подряд отвечала без единого вызова инструмента."""
    assert forced_tool_for("на сегодня есть окошко 4 часа , нас 6теро") == "check_availability"


def test_a_relative_day_without_specifics_goes_through_resolve_date():
    """«Есть окошко сегодня?» — сначала превратить «сегодня» в дату.

    Считать это в уме модели нельзя: ровно на этом она ошиблась 31 августа
    и 1 сентября.
    """
    assert forced_tool_for("Есть окошко сегодня?") == "resolve_date"


def test_a_date_and_a_time_together_force_the_calendar_without_any_keyword():
    """«сегодня 16 00» — ВТОРОЙ ход инцидента.

    Слова про занятость здесь нет: клиент отвечает на вопрос агента, и
    признак остался в предыдущей реплике. Без этой ветки принуждение не
    сработало бы ровно там, где инцидент и продолжился.
    """
    assert forced_tool_for("сегодня 16 00") == "check_availability"


def test_nearest_available_asks_the_tool_instead_of_the_client():
    assert forced_tool_for("когда ближайшее свободное?") == "find_next_available"


def test_a_concrete_date_with_a_booking_word_forces_the_calendar():
    assert forced_tool_for("можно записаться на 5 сентября в 18:00?") == "check_availability"


def test_a_price_question_forces_the_calculator_when_the_zone_is_named():
    """Цена — отдельная ветка и ПЕРВАЯ.

    Проверено тем же замером: с полным промтом модель не звала
    calculate_price так же, как не звала календарь. Ценовой рубеж ловил бы
    выдумку, но клиент вместо цены получал бы заглушку.
    """
    assert forced_tool_for("Сколько стоит баня на 4 часа?") == "calculate_price"


def test_a_price_question_with_a_date_forces_the_calculator_too():
    """Даты одной хватает: зону модель спросит одним вопросом, а вызов уже
    осмысленный."""
    assert forced_tool_for("сколько стоит 5 сентября?") == "calculate_price"
    assert forced_tool_for("почём завтра?") == "calculate_price"


def test_a_bare_price_question_forces_nothing():
    """«А цена какая?» без зоны и без даты — принуждать нечего.

    calculate_price требует zone_id и date. Принуждение здесь заставляло
    модель выдумать оба поля, получить needs_input и следом спросить у
    клиента всё разом: замер 2026-09-02 показал рост доли ответов больше чем
    с одним вопросом с 2% до 18%. Анкетность дороже, чем один сэкономленный
    виток.
    """
    assert forced_tool_for("а цена какая?") is None
    assert forced_tool_for("Стоимость?") is None


def test_the_zone_from_earlier_in_the_conversation_counts():
    """Клиент назвал зону ходом раньше — вызов снова осмыслен.

    Ровно этот случай и делает сужение безопасным: разговор почти всегда
    начинается с зоны, а «а цена какая?» приходит вторым-третьим сообщением.
    """
    assert forced_tool_for("а цена какая?", "Здравствуйте, интересует баня") == "calculate_price"
    assert forced_tool_for("Стоимость?", "хотим 5 сентября") == "calculate_price"
    # Зона объявления передаётся тем же путём — см. AgentLoop.run_turn.
    assert forced_tool_for("а цена какая?", "Купол с мягкими мешками") == "calculate_price"


def test_every_zone_in_the_catalogue_is_recognised():
    """Словарь зон живёт отдельно от базы знаний — значит, может от неё
    отстать. Здесь он с ней и сверяется: появится зона нового рода, тест
    упадёт, и сужение цены не начнёт молча пропускать её мимо."""
    from app.kb.loader import load_catalog

    kb = load_catalog()
    assert kb.catalog.zones, "каталог пуст — сверять не с чем"
    for zone in kb.catalog.zones:
        assert ZONE_WORDS.search(zone.name), f"зона {zone.id} («{zone.name}») не опознаётся"
        if zone.display_name_alt:
            assert ZONE_WORDS.search(zone.display_name_alt), (
                f"второе имя зоны {zone.id} («{zone.display_name_alt}») не опознаётся"
            )


# --------------------------------------------------------------------------
# НЕ принуждаем — иначе календарь дёргается на пустом месте
# --------------------------------------------------------------------------

def test_a_date_without_an_availability_question_forces_nothing():
    """«Завтра перезвоню» — требование заказчика дословно.

    Дата есть, вопроса про запись нет. Принуждать здесь значило бы ходить в
    календарь на каждое упоминание дня.
    """
    assert forced_tool_for("завтра перезвоню") is None


def test_a_past_visit_forces_nothing():
    assert forced_tool_for("вчера были у вас, спасибо") is None


def test_small_talk_and_other_topics_force_nothing():
    assert forced_tool_for("здравствуйте") is None
    assert forced_tool_for("а фото есть?") is None
    assert forced_tool_for("нас 6теро") is None
    assert forced_tool_for("") is None
    assert forced_tool_for("   ") is None


# --------------------------------------------------------------------------
# Порядок веток — часть поведения
# --------------------------------------------------------------------------

def test_price_wins_over_the_calendar_when_both_are_asked():
    """«Сколько стоит завтра в 18:00» — про деньги. Календарь подтянется
    следующим витком, если понадобится: принуждение снимается после первого."""
    assert forced_tool_for("сколько стоит завтра в 18:00?") == "calculate_price"


def test_nearest_beats_the_calendar_only_without_a_concrete_date():
    """«Ближайшее свободное после 10 сентября» — число названо, значит
    спрашивать «когда ближайшее» уже нечего, идём в календарь."""
    assert forced_tool_for("когда ближайшее свободное?") == "find_next_available"
    assert forced_tool_for("ближайшее свободное после 10 сентября") == "check_availability"
