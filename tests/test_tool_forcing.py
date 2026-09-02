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
# Морфология: русская основа в регэкспе — системная ловушка
# --------------------------------------------------------------------------

def test_the_word_clients_actually_use_for_booking_is_recognised():
    """«Забронировать» — самое частое слово, которым просят бронь, и оно НЕ
    опознавалось: граница слова `\\b` съедается приставкой «за».

    Проверяем набором форм, а не одним примером: ловушка тут не в конкретном
    слове, а в том, как пишется основа.
    """
    for text in (
        "Хотим забронировать купол 5 сентября",
        "Забронируйте нам баню на 5 сентября",
        "Забронируем юрту на 5 сентября",
        "Я уже забронировала домик на 5 сентября",
        "Перебронировать бы на 5 сентября",
        "Зарезервируйте юрту на 5 сентября",
        "бронь на 5 сентября сделаете?",
    ):
        assert forced_tool_for(text) is not None, text


def test_the_short_masculine_form_is_recognised():
    """«Купол свободен» — зон мужского рода четыре из десяти, и краткая
    форма для них естественнее полной. `свободн\\w*` её не ловит."""
    for text in ("Купол свободен завтра?", "Домик свободен 5 сентября?",
                 "Шатёр свободен завтра?"):
        assert forced_tool_for(text) is not None, text


def test_a_plural_window_is_recognised_in_forcing():
    """«Есть окна на завтра?» — множественное число мимо словаря.

    ЗДЕСЬ это добавлено, а в словарь РУБЕЖА (app/agent/loop.py) — нет:
    цена ошибки разная, см. комментарии к обоим словарям.
    """
    assert forced_tool_for("есть окна на завтра?") is not None


def test_can_i_come_on_any_weekday_not_just_tomorrow():
    """Список хвостов после «можно» покрывал четыре варианта и ломался на
    любом другом дне."""
    assert forced_tool_for("Можно на субботу?") is not None
    assert forced_tool_for("Можно в субботу?") is not None
    assert forced_tool_for("можно завтра к вам?") is not None


def test_a_price_question_no_longer_swallows_the_availability_one():
    """«Сколько стоит и когда ближайшее свободное?» возвращало None: ценовая
    ветка коротила ход целиком и до веток занятости он не доходил."""
    assert forced_tool_for("Сколько стоит и когда ближайшее свободное?") == "find_next_available"


# --------------------------------------------------------------------------
# НЕ принуждаем — иначе календарь дёргается на пустом месте
# --------------------------------------------------------------------------

def test_a_promise_to_call_back_never_forces_anything():
    """Требование заказчика дословно, во всех видах.

    Ветка «дата и время вместе» признака записи не спрашивает намеренно
    (второй ход инцидента выглядел как «сегодня 16 00»), поэтому обещание
    перезвонить приходится отсекать отдельным вето — иначе «завтра в 5
    перезвоню» уходило в календарь.
    """
    for text in ("завтра перезвоню", "завтра в 5 перезвоню",
                 "я перезвоню вам в ближайшее время", "наберу вас завтра в 18:00",
                 "свяжусь с вами завтра"):
        assert forced_tool_for(text) is None, text


def test_a_past_visit_never_forces_anything():
    """«Мы были у вас в субботу в 18:00» — прошедший визит с датой и временем.
    Прежняя редакция шла с этим в календарь."""
    for text in ("мы были у вас в субботу в 18:00, спасибо",
                 "в субботу были у вас в 20:00, всё понравилось",
                 "была у вас вчера, спасибо"):
        assert forced_tool_for(text) is None, text


def test_a_cancellation_is_not_a_booking_question():
    """Отмена — не вопрос о занятости, и календарь для неё не нужен."""
    assert forced_tool_for("отмените нашу бронь на 5 сентября в 18:00") is None
    assert forced_tool_for("отказываемся от брони на завтра") is None


def test_soon_as_in_shortly_is_not_a_calendar_question():
    """«в ближайшее время» — оборот про скорость ответа, а не вопрос о датах.
    Он включал find_next_available."""
    assert forced_tool_for("напишу в ближайшее время") is None
    assert forced_tool_for("перезвоню в ближайшее время") is None
    # А настоящий вопрос про ближайшее свободное — по-прежнему включает.
    assert forced_tool_for("когда ближайшее свободное?") == "find_next_available"


def test_a_range_of_guests_is_not_a_date():
    """«Нас будет 6-8 человек» — количество гостей, а не число месяца.

    Прежняя редакция считала датой любую пару чисел через тире, и вопрос о
    цене с такой историей включал принуждение: «зона или дата известны» —
    хотя не известно ни то, ни другое.
    """
    assert forced_tool_for("сколько стоит?", "нас будет 6-8 человек") is None
    assert forced_tool_for("сколько стоит?", "нас 10-12 гостей") is None
    assert forced_tool_for("сколько стоит на 2-3 часа?") is None
    # А диапазон ДАТ остаётся датой.
    assert forced_tool_for("сколько стоит?", "думаем на 5-6 сентября") == "calculate_price"
    assert forced_tool_for("сколько стоит?", "хотим 01.09") == "calculate_price"


def test_a_reservoir_is_not_a_reservation():
    """`резерв` без оговорки поймал бы «резервуар для воды».

    Дата в тексте обязательна, иначе тест проходит и со сломанным правилом:
    без даты ветки занятости всё равно возвращают None, и разницы не видно.
    Найдено мутацией.
    """
    assert forced_tool_for("у вас есть резервуар для воды? приедем завтра") is None
    # А настоящая просьба зарезервировать — принуждает.
    assert forced_tool_for("зарезервируйте нам баню на завтра") is not None



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
