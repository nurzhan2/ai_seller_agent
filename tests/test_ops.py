"""Тесты операторского контура.

Логика проверяется через OpsService, без Telegram и без сети.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.config import Settings
from app.ops.bot import OpsService
from app.ops.handlers import parse_callback
from app.ops.notifications import (
    DialogCard,
    dialog_keyboard,
    render_booking_handoff,
    render_booking_notice,
    render_concession,
    render_dialog_card,
    render_digest,
    render_escalation,
    render_stats,
)
from app.ops.state import ChatFlags, InMemoryOpsStore, should_auto_return

ALLOWED_USER = 111
STRANGER = 999


@pytest.fixture
def settings():
    return Settings(telegram_allowed_users=[ALLOWED_USER], dry_run=True)


@pytest.fixture
def sent():
    return []


@pytest.fixture
def service(settings, sent):
    async def send(chat_id, text):
        sent.append((chat_id, text))

    return OpsService(store=InMemoryOpsStore(), settings=settings, send_to_avito=send)


# --------------------------------------------------------------------------
# Доступ
# --------------------------------------------------------------------------

def test_only_allowed_users_pass(service):
    assert service.is_allowed(ALLOWED_USER) is True
    assert service.is_allowed(STRANGER) is False


def test_empty_allowlist_means_nobody_not_everybody():
    """Пустая настройка не должна открывать управление ботом всем подряд."""
    service = OpsService(settings=Settings(telegram_allowed_users=[]))
    assert service.is_allowed(ALLOWED_USER) is False
    assert service.is_allowed(0) is False


# --------------------------------------------------------------------------
# Карточка диалога
# --------------------------------------------------------------------------

def test_card_marks_dry_run_as_not_sent():
    card = DialogCard("c1", "Баня", "Борис", "Сколько стоит?", "3 ч × 3500 ₽", dry_run=True)
    text = render_dialog_card(card)
    assert "НЕ ОТПРАВЛЕНО" in text
    assert "Борис" in text
    assert "3500" in text


def test_card_without_dry_run_has_no_warning():
    card = DialogCard("c1", "Баня", "Борис", "?", "ответ", dry_run=False)
    assert "НЕ ОТПРАВЛЕНО" not in render_dialog_card(card)


def test_card_shows_escalation():
    card = DialogCard(
        "c1", "Баня", None, "?", "уточню", dry_run=True,
        escalated=True, escalation_reason="цена не подтверждена",
    )
    text = render_dialog_card(card)
    assert "🔴" in text and "цена не подтверждена" in text


def test_card_is_within_telegram_limit():
    card = DialogCard("c1", "Баня", "Борис", "x" * 5000, "y" * 5000, dry_run=True)
    assert len(render_dialog_card(card)) <= 4096


def test_keyboard_offers_approval_only_in_dry_run():
    dry = dialog_keyboard("c1", dry_run=True, taken_over=False)
    live = dialog_keyboard("c1", dry_run=False, taken_over=False)
    dry_labels = [b.text for row in dry.inline_keyboard for b in row]
    live_labels = [b.text for row in live.inline_keyboard for b in row]
    assert any("Одобрить" in label for label in dry_labels)
    assert not any("Одобрить" in label for label in live_labels)


def test_keyboard_switches_takeover_and_return():
    free = dialog_keyboard("c1", dry_run=False, taken_over=False)
    taken = dialog_keyboard("c1", dry_run=False, taken_over=True)
    assert any("Взять на себя" in b.text for row in free.inline_keyboard for b in row)
    assert any("Вернуть ИИ" in b.text for row in taken.inline_keyboard for b in row)


# --------------------------------------------------------------------------
# Модерация
# --------------------------------------------------------------------------

async def test_approve_sends_once(service, sent):
    await service.queue_reply("c1", "Здравствуйте! Свободно.")
    result = await service.approve("c1", ALLOWED_USER)
    assert result["sent"] is True
    assert sent == [("c1", "Здравствуйте! Свободно.")]


async def test_approve_is_idempotent(service, sent):
    """Telegram переотправляет callback при плохой связи — клиент не должен
    получить два одинаковых сообщения."""
    await service.queue_reply("c1", "текст")
    await service.approve("c1", ALLOWED_USER)
    second = await service.approve("c1", ALLOWED_USER)
    assert second["sent"] is False
    assert len(sent) == 1


async def test_reject_sends_nothing(service, sent):
    await service.queue_reply("c1", "неудачный ответ")
    result = await service.reject("c1", ALLOWED_USER)
    assert result["sent"] is False
    assert sent == []


async def test_reject_is_idempotent(service, sent):
    await service.queue_reply("c1", "текст")
    await service.reject("c1", ALLOWED_USER)
    assert (await service.reject("c1", ALLOWED_USER))["sent"] is False
    assert sent == []


async def test_operator_edit_counts_as_edited_not_approved(service, sent):
    """Это метрика готовности к автономной работе, поэтому исправление должно
    считаться отдельно от чистого одобрения."""
    await service.queue_reply("c1", "ответ агента")
    await service.send_edited("c1", ALLOWED_USER, "мой текст")
    assert sent == [("c1", "мой текст")]
    assert service.store.moderation["edited"] == 1
    assert service.store.moderation["approved"] == 0


async def test_moderation_counters_feed_stats(service):
    await service.queue_reply("c1", "a")
    await service.approve("c1", ALLOWED_USER)
    await service.queue_reply("c2", "b")
    await service.reject("c2", ALLOWED_USER)
    assert service.store.moderation == {"approved": 1, "edited": 0, "rejected": 1}


# --------------------------------------------------------------------------
# Перехват
# --------------------------------------------------------------------------

async def test_takeover_silences_agent(service):
    await service.takeover("c1", ALLOWED_USER)
    allowed, reason = await service.should_agent_reply("c1")
    assert allowed is False
    assert "оператор" in reason


async def test_takeover_is_idempotent(service):
    first = await service.takeover("c1", ALLOWED_USER)
    second = await service.takeover("c1", ALLOWED_USER)
    assert first["changed"] is True and second["changed"] is False


async def test_return_to_ai_restores_agent(service):
    await service.takeover("c1", ALLOWED_USER)
    await service.return_to_ai("c1", ALLOWED_USER)
    allowed, _ = await service.should_agent_reply("c1")
    assert allowed is True


async def test_auto_return_after_24h():
    """Страховка «взял и забыл» — и проверяется она в режиме `permanent`,
    где сработать может ТОЛЬКО она. В `cooldown` те же сутки возвращают чат
    по истёкшему окну, и тест зеленел бы, даже если суточное правило
    сломано."""
    service = OpsService(
        store=InMemoryOpsStore(),
        settings=Settings(telegram_allowed_users=[ALLOWED_USER], dry_run=True,
                          takeover_mode="permanent"),
    )
    flags = ChatFlags(
        is_human_takeover=True,
        takeover_at=datetime.now(timezone.utc) - timedelta(hours=25),
    )
    await service.store.set_flags("c1", flags)

    assert await service.auto_return_if_stale("c1") is True
    allowed, _ = await service.should_agent_reply("c1")
    assert allowed is True
    assert any(
        a["action"] == "auto_return" and a["payload"]["reason"] == "24h без активности"
        for a in service.store.actions
    )


async def test_no_auto_return_before_timeout(sent):
    """Суточная страховка — про режим `permanent`: там перехват держится до
    кнопки, и два часа не повод возвращать чат агенту. В `cooldown` те же
    два часа означают давно истёкшее окно (см. тесты режимов ниже)."""
    service = OpsService(
        store=InMemoryOpsStore(),
        settings=Settings(telegram_allowed_users=[ALLOWED_USER], dry_run=True,
                          takeover_mode="permanent"),
    )
    flags = ChatFlags(
        is_human_takeover=True,
        takeover_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )
    await service.store.set_flags("c1", flags)
    assert await service.auto_return_if_stale("c1") is False


def test_should_auto_return_ignores_free_chats():
    assert should_auto_return(ChatFlags()) is False


# --------------------------------------------------------------------------
# Стопоры
# --------------------------------------------------------------------------

async def test_pause_stops_every_chat(service):
    await service.pause_all(ALLOWED_USER)
    allowed, reason = await service.should_agent_reply("any")
    assert allowed is False and "паузе" in reason


async def test_resume_restores(service):
    await service.pause_all(ALLOWED_USER)
    await service.resume_all(ALLOWED_USER)
    allowed, _ = await service.should_agent_reply("any")
    assert allowed is True


# --------------------------------------------------------------------------
# Аварийный рубильник (/stop, /resume) — persisted в Redis, читает
# OutboundGate на каждой отправке (см. app/channels/kill_switch.py).
# --------------------------------------------------------------------------

class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def set(self, key, value, **kwargs):
        self.store[key] = value
        return True

    async def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def service_with_redis(settings, sent):
    async def send(chat_id, text):
        sent.append((chat_id, text))

    return OpsService(
        store=InMemoryOpsStore(), settings=settings, send_to_avito=send, redis=_FakeRedis(),
    )


async def test_stop_sets_the_kill_switch(service_with_redis):
    from app.channels import kill_switch

    message = await service_with_redis.stop_sending(ALLOWED_USER, reason="проверка")
    assert "ОСТАНОВЛЕНА" in message
    assert await kill_switch.is_stopped(service_with_redis.redis) is True


async def test_resume_clears_the_kill_switch(service_with_redis):
    from app.channels import kill_switch

    await service_with_redis.stop_sending(ALLOWED_USER)
    message = await service_with_redis.resume_all(ALLOWED_USER)

    assert await kill_switch.is_stopped(service_with_redis.redis) is False
    assert "рубильник снят" in message.lower()


async def test_resume_without_a_prior_stop_does_not_mention_the_kill_switch(service_with_redis):
    message = await service_with_redis.resume_all(ALLOWED_USER)
    assert "рубильник" not in message.lower()


async def test_stop_without_redis_fails_loudly_not_silently(service):
    """`service` (фикстура без redis) — как боевой процесс без REDIS_URL:
    команда должна честно сказать, что рубильнику негде храниться, а не
    сделать вид, что отправка остановлена."""
    message = await service.stop_sending(ALLOWED_USER)
    assert "не удалось" in message.lower()


async def test_stop_is_logged_with_reason(service_with_redis):
    await service_with_redis.stop_sending(ALLOWED_USER, reason="подозрение на утечку")
    last = service_with_redis.store.actions[-1]  # type: ignore[attr-defined]
    assert last["action"] == "stop_sending"
    assert last["payload"]["reason"] == "подозрение на утечку"


async def test_reply_limit_hands_chat_to_operator(service, settings):
    flags = ChatFlags(agent_reply_count=settings.max_agent_replies_per_chat)
    await service.store.set_flags("c1", flags)
    allowed, reason = await service.should_agent_reply("c1")
    assert allowed is False and "лимит" in reason


async def test_dryrun_off_warns_loudly(service):
    message = await service.set_dry_run(ALLOWED_USER, False)
    assert "ВЫКЛЮЧЕН" in message


# --------------------------------------------------------------------------
# /moderation — переключение режима на лету, без передеплоя
# --------------------------------------------------------------------------

async def test_moderation_mode_defaults_to_concessions_only(settings):
    assert settings.moderation_mode == "concessions_only"


async def test_show_moderation_mode_reports_current(service):
    message = await service.show_moderation_mode()
    assert "concessions_only" in message


async def test_set_moderation_mode_switches_live_without_redeploy(service, settings):
    message = await service.set_moderation_mode(ALLOWED_USER, "off")

    assert "concessions_only → off" in message
    assert settings.moderation_mode == "off"          # тот же объект настроек, что видит пайплайн
    actions = [a for a in service.store.actions if a["action"] == "set_moderation_mode"]
    assert actions and actions[0]["payload"] == {"from": "concessions_only", "to": "off"}


async def test_set_moderation_mode_rejects_unknown_value(service, settings):
    message = await service.set_moderation_mode(ALLOWED_USER, "sometimes")

    assert "Использование" in message
    assert settings.moderation_mode == "concessions_only"


async def test_set_moderation_mode_is_a_noop_when_already_active(service, settings):
    message = await service.set_moderation_mode(ALLOWED_USER, "concessions_only")

    assert "Уже" in message
    assert not [a for a in service.store.actions if a["action"] == "set_moderation_mode"]


async def test_set_moderation_mode_all_three_values_are_accepted(service, settings):
    for mode in ("all", "off", "concessions_only"):
        message = await service.set_moderation_mode(ALLOWED_USER, mode)
        assert settings.moderation_mode == mode
        assert "Использование" not in message


# --------------------------------------------------------------------------
# LLM-провайдер (промт №12)
# --------------------------------------------------------------------------

async def test_show_provider_reports_current_and_no_fallback(service):
    message = await service.show_provider()
    assert "deepseek" in message
    assert "не настроен" in message


async def test_set_provider_rejects_unknown_name(service, settings):
    message = await service.set_provider(ALLOWED_USER, "openai")
    assert "Использование" in message
    assert settings.llm_provider == "deepseek"


async def test_set_provider_refuses_anthropic_and_says_why(service, settings):
    """Команда, которой можно сломать бота одним словом, не должна
    существовать: раньше `/provider anthropic` переключал на провайдера с
    ключом-заглушкой, после чего падал КАЖДЫЙ ход, а снаружи это выглядело
    как «бот сломался»."""
    message = await service.set_provider(ALLOWED_USER, "anthropic")

    assert "убран" in message
    assert settings.llm_provider == "deepseek"
    assert not [a for a in service.store.actions if a["action"] == "set_provider"]


async def test_set_provider_is_a_noop_when_already_active(service, settings):
    message = await service.set_provider(ALLOWED_USER, "deepseek")
    assert "Уже на" in message
    assert not [a for a in service.store.actions if a["action"] == "set_provider"]


# --------------------------------------------------------------------------
# Журнал действий
# --------------------------------------------------------------------------

async def test_every_action_is_logged_with_user_id(service):
    await service.takeover("c1", ALLOWED_USER)
    await service.queue_reply("c1", "t")
    await service.approve("c1", ALLOWED_USER)
    actions = service.store.actions
    assert {a["action"] for a in actions} == {"takeover", "approve"}
    assert all(a["user_id"] == ALLOWED_USER for a in actions)


# --------------------------------------------------------------------------
# Уведомления об уступках и эскалациях
# --------------------------------------------------------------------------

def test_concession_notification_shows_loss_and_provisional_flag():
    text = render_concession(
        "c1", tier=6, kind="price", trigger="price_objection",
        revenue_delta=Decimal("-3000"), provisional=True,
        offer_template="Могу зафиксировать 2500 ₽ в час, если бронируем сегодня.",
    )
    assert "-3000" in text
    assert "предварительное" in text
    assert "13.4" in text


def test_concession_without_provisional_has_no_warning():
    text = render_concession(
        "c1", 5, "price", "hours_objection", Decimal("-1500"), False, "текст"
    )
    assert "предварительное" not in text


def test_escalation_marks_urgency():
    assert "🔴🔴" in render_escalation("c1", "жалоба", "high")
    assert "🟠" in render_escalation("c1", "мелочь", "low")


def test_stats_reports_clean_approval_share():
    text = render_stats(
        dialogs=10, leads=3, escalations=2, cost_rub=Decimal("120"),
        approved=9, edited=1, rejected=0,
    )
    assert "90%" in text
    assert "120" in text


def test_stats_handles_zero_moderation():
    text = render_stats(0, 0, 0, Decimal("0"), 0, 0, 0)
    assert "—" in text


def test_digest_lists_leads_and_unanswered_topics():
    text = render_digest(
        dialogs=12,
        leads=[{"name": "Борис", "phone": "89160000000", "zone_id": "bath_russian"}],
        escalations=["цена домика не подтверждена"],
        concessions_total=Decimal("-4500"),
        concessions_count=2,
        unanswered_topics=["конная прогулка", "пробковый сбор"],
    )
    assert "Борис" in text and "89160000000" in text
    assert "конная прогулка" in text
    assert "-4500" in text


# --------------------------------------------------------------------------
# Разбор callback
# --------------------------------------------------------------------------

def test_callback_parsing():
    assert parse_callback("approve:c-123") == ("approve", "c-123")
    assert parse_callback("takeover:abc") == ("takeover", "abc")


# --------------------------------------------------------------------------
# Белый список объявлений на пути «одобрено оператором»
#
# `send_to_avito` у OpsService теперь подключён в app/main.py
# (`ops_service.send_to_avito = outbound.send_message`, сразу после сборки
# гейта) — до этой правки был подключён к `None`, /approve отвечал
# оператору «Отправлено клиенту», а сообщение не уходило вообще ни через
# гейт, ни мимо него (см. tests/test_main.py:
# test_lifespan_wires_operator_approval_through_the_outbound_gate). Тесты
# ниже проверяют логику OpsService в изоляции — что ОНА сама уважает то,
# что ей передали как send_to_avito, — поэтому собирают гейт руками, а не
# через lifespan.
# --------------------------------------------------------------------------

async def test_approved_reply_to_a_blocked_chat_is_not_delivered():
    from app.channels.outbound_gate import ListingNotAllowed, OutboundGate

    class _Client:
        def __init__(self):
            self.sent: list[tuple[str, str]] = []

        async def send_message(self, chat_id, text):
            self.sent.append((chat_id, text))
            return {"ok": True}

    async def lookup(chat_id: str):
        return "8204183112"    # вакансия менеджера — в чёрном списке

    client = _Client()
    gate = OutboundGate(client, Settings(), lookup)   # пять id заблокированы по умолчанию
    service = OpsService(
        store=InMemoryOpsStore(),
        settings=Settings(telegram_allowed_users=[ALLOWED_USER], dry_run=True),
        send_to_avito=gate.send_message,
    )
    await service.queue_reply("chat-vacancy", "Здравствуйте! Свободно.")

    with pytest.raises(ListingNotAllowed):
        await service.approve("chat-vacancy", ALLOWED_USER)

    assert client.sent == []


async def test_approved_reply_to_an_allowed_chat_is_delivered():
    from app.channels.outbound_gate import OutboundGate

    class _Client:
        def __init__(self):
            self.sent: list[tuple[str, str]] = []

        async def send_message(self, chat_id, text):
            self.sent.append((chat_id, text))
            return {"ok": True}

    async def lookup(chat_id: str):
        return "item-1"

    client = _Client()
    gate = OutboundGate(client, Settings(avito_allowed_items="item-1"), lookup)
    service = OpsService(
        store=InMemoryOpsStore(),
        settings=Settings(telegram_allowed_users=[ALLOWED_USER], dry_run=True),
        send_to_avito=gate.send_message,
    )
    await service.queue_reply("chat-1", "Здравствуйте! Свободно.")

    await service.approve("chat-1", ALLOWED_USER)

    assert client.sent == [("chat-1", "Здравствуйте! Свободно.")]


# --------------------------------------------------------------------------
# /reset — сброс счётчика ответов в одном чате
#
# До этой команды исчерпанный лимит снимался только правкой в базе руками.
# --------------------------------------------------------------------------

async def test_reset_lets_the_agent_reply_again(service):
    """Главное свойство: после сброса should_agent_reply снова пропускает."""
    flags = await service.store.get_flags("chat-1")
    flags.agent_reply_count = service.settings.max_agent_replies_per_chat
    await service.store.set_flags("chat-1", flags)
    allowed, reason = await service.should_agent_reply("chat-1")
    assert allowed is False and "лимит" in reason

    result = await service.reset_reply_count("chat-1", ALLOWED_USER)

    assert result["changed"] is True
    allowed, _ = await service.should_agent_reply("chat-1")
    assert allowed is True


async def test_reset_reports_the_previous_count(service):
    flags = await service.store.get_flags("chat-1")
    flags.agent_reply_count = 25
    await service.store.set_flags("chat-1", flags)

    result = await service.reset_reply_count("chat-1", ALLOWED_USER)

    assert "было 25" in result["message"]


async def test_reset_on_a_fresh_chat_changes_nothing(service):
    result = await service.reset_reply_count("chat-never-seen", ALLOWED_USER)

    assert result["changed"] is False


async def test_reset_does_not_touch_the_limit_itself(service):
    """Разовое «дай доработать этот диалог», а не «подними планку всем»."""
    before = service.settings.max_agent_replies_per_chat
    flags = await service.store.get_flags("chat-1")
    flags.agent_reply_count = before
    await service.store.set_flags("chat-1", flags)

    await service.reset_reply_count("chat-1", ALLOWED_USER)

    assert service.settings.max_agent_replies_per_chat == before


async def test_reset_does_not_clear_takeover(service):
    """Сброс счётчика — не «верни чат агенту»: если оператор забрал чат,
    он остаётся у оператора."""
    await service.takeover("chat-1", ALLOWED_USER)
    flags = await service.store.get_flags("chat-1")
    flags.agent_reply_count = 25
    await service.store.set_flags("chat-1", flags)

    await service.reset_reply_count("chat-1", ALLOWED_USER)

    allowed, reason = await service.should_agent_reply("chat-1")
    assert allowed is False and "оператора" in reason


async def test_reset_is_written_to_the_action_log(service):
    flags = await service.store.get_flags("chat-1")
    flags.agent_reply_count = 25
    await service.store.set_flags("chat-1", flags)

    await service.reset_reply_count("chat-1", ALLOWED_USER)

    logged = [a for a in service.store.actions if a["action"] == "reset_reply_count"]
    assert len(logged) == 1
    assert logged[0]["user_id"] == ALLOWED_USER
    assert logged[0]["payload"]["was"] == 25
    # В статистику модерации сброс НЕ попадает — он не одобрение и не отказ.
    assert service.store.moderation == {"approved": 0, "edited": 0, "rejected": 0}


# --------------------------------------------------------------------------
# Лимит ответов — настройка, а не константа
# --------------------------------------------------------------------------

def test_reply_limit_reads_the_new_env_name(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_REPLIES_PER_CHAT", "7")
    assert Settings().max_agent_replies_per_chat == 7


def test_reply_limit_still_reads_the_old_env_name(monkeypatch):
    """MAX_AGENT_REPLIES_PER_CHAT описан в docs/RAILWAY_SETUP.md и может быть
    уже выставлен — перестать его слушать значит незаметно вернуть лимит к
    25 там, где его осознанно меняли."""
    monkeypatch.delenv("AGENT_MAX_REPLIES_PER_CHAT", raising=False)
    monkeypatch.setenv("MAX_AGENT_REPLIES_PER_CHAT", "3")
    assert Settings().max_agent_replies_per_chat == 3


def test_reply_limit_defaults_to_25(monkeypatch):
    monkeypatch.delenv("AGENT_MAX_REPLIES_PER_CHAT", raising=False)
    monkeypatch.delenv("MAX_AGENT_REPLIES_PER_CHAT", raising=False)
    assert Settings().max_agent_replies_per_chat == 25


# --------------------------------------------------------------------------
# Карточка «поставьте бронь руками» (этап оплаты)
# --------------------------------------------------------------------------

HANDOFF_CARD = {
    "chat_id": "c1",
    "zone_id": "bath_russian",
    "zone_name": "Русская баня",
    "booking_date": "2026-08-29",
    "start_time": "14:00",
    "occupied_hours": 3,
    "billable_hours": 3,
    "guests": 6,
    "total": Decimal("9000"),
    "prepayment": Decimal("3000"),
    "client_name": "Иван",
    "client_phone": "+79990000000",
    "comment": "нужны веники",
    "applied_promo": None,
    "slot_confirmed_free": True,
}


def test_handoff_card_has_everything_needed_to_book_by_hand():
    """Смысл карточки ровно один: не листать переписку. Значит каждое поле
    из неё должно быть видно глазами, а не подразумеваться."""
    text = render_booking_handoff(HANDOFF_CARD)
    for fragment in [
        "Русская баня",
        "2026-08-29",
        "14:00",
        "Часы: 3",
        "Гостей: 6",
        "Иван",
        "+79990000000",
        "9000 ₽",
        "3000 ₽",
        "нужны веники",
    ]:
        assert fragment in text, fragment


def test_handoff_card_says_the_booking_does_not_exist_yet():
    """Главное отличие от уведомления о поставленной броне: там факт, здесь
    работа. Перепутать их значит оставить клиента без брони."""
    text = render_booking_handoff(HANDOFF_CARD)
    assert "ПОСТАВЬТЕ БРОНЬ РУКАМИ" in text
    assert "не ставил и не поставит" in text


def test_handoff_card_shows_promo_hours_both_ways():
    text = render_booking_handoff(
        {**HANDOFF_CARD, "occupied_hours": 6, "billable_hours": 5,
         "applied_promo": "sixth_hour_free"}
    )
    assert "занять 6" in text
    assert "оплачено 5" in text
    assert "sixth_hour_free" in text


def test_handoff_card_marks_missing_fields_instead_of_hiding_them():
    """Пропущенная строка читается как «всё собрано». Прочерк — как «нет»."""
    text = render_booking_handoff({"chat_id": "c1", "zone_id": "yurt"})
    assert "Имя: —" in text
    assert "Телефон: —" in text
    assert "Предоплата: —" in text


UNKNOWN_SLOT_WARNING = "⚠️ ЗАНЯТОСТЬ НЕ ПРОВЕРЕНА"


def test_handoff_card_warns_when_availability_is_unknown():
    """Занятость UNKNOWN — предупреждение обязано быть. Живой случай:
    house_relax не связан с YCLIENTS (`zone_service_map`), и занятость по
    нему не приходит НИКОГДА, сколько ни спрашивай."""
    text = render_booking_handoff({**HANDOFF_CARD, "slot_confirmed_free": False})

    assert UNKNOWN_SLOT_WARNING in text
    assert "сверьтесь с календарём" in text


def test_handoff_card_has_no_warning_when_the_slot_is_free():
    """И обратное: слот подтверждён свободным — предупреждения нет.
    Предупреждение, которое висит всегда, оператор перестаёт читать через
    неделю, и тогда оно не сработает там, где нужно."""
    assert UNKNOWN_SLOT_WARNING not in render_booking_handoff(HANDOFF_CARD)


def test_the_unknown_slot_warning_stands_above_the_booking_data():
    """Отдельной строкой в начале, а не пометкой в поле: оператор читает
    карточку сверху вниз и заносит бронь по ней. Предупреждение, замеченное
    после записи в календарь, уже ничего не спасает."""
    lines = render_booking_handoff({**HANDOFF_CARD, "slot_confirmed_free": False}).splitlines()

    warning_at = next(i for i, line in enumerate(lines) if UNKNOWN_SLOT_WARNING in line)
    first_field_at = next(i for i, line in enumerate(lines) if line.startswith("Зона:"))

    assert warning_at < first_field_at
    assert warning_at <= 2                       # сразу под заголовком, не в подвале


# --------------------------------------------------------------------------
# Уведомление о поставленной агентом броне
#
# Третья дыра из мутационного разбора: карточка «бронь уже стоит» не была
# покрыта ничем. Часы акции можно было слить в одно число — оператор
# перестал бы видеть, что заблокировано 6 часов, а оплачено 5, — и ни один
# тест бы не упал.
# --------------------------------------------------------------------------

BOOKED_RECORD = {
    "chat_id": "c-1",
    "record_id": "rec-42",
    "zone_id": "bath_russian",
    "booking_date": "2026-08-29",
    "start_time": "14:00",
    "occupied_hours": 3,
    "billable_hours": 3,
    "guests": 6,
    "total": Decimal("9000"),
    "client_name": "Иван",
    "client_phone": "+79990000000",
    "applied_promo": None,
}


def test_booking_notice_carries_the_facts_of_the_booking():
    text = render_booking_notice(BOOKED_RECORD)

    for fragment in ["bath_russian", "2026-08-29", "14:00", "Часы: 3", "Гостей: 6",
                     "9000 ₽", "Иван", "+79990000000", "rec-42"]:
        assert fragment in text, fragment


def test_booking_notice_says_the_booking_already_exists():
    """Противоположность карточке передачи: там работа, здесь факт. Кнопок
    нет намеренно — одобрять уже нечего."""
    text = render_booking_notice(BOOKED_RECORD)

    assert "БРОНЬ ПОСТАВЛЕНА АГЕНТОМ" in text
    assert "Подтверждать не нужно" in text


def test_booking_notice_shows_promo_hours_both_ways():
    """Акция «6-й час в подарок»: занято 6, оплачено 5. Одно число вместо
    двух — и шестой час выглядит как ошибка агента, которую пойдут
    «исправлять» в календаре."""
    text = render_booking_notice(
        {**BOOKED_RECORD, "occupied_hours": 6, "billable_hours": 5,
         "applied_promo": "sixth_hour_free"}
    )

    assert "занято 6" in text
    assert "оплачено 5" in text
    assert "sixth_hour_free" in text


def test_booking_notice_does_not_split_hours_when_they_match():
    """Без акции два одинаковых числа рядом только сбивают с толку."""
    text = render_booking_notice(BOOKED_RECORD)

    assert "Часы: 3" in text
    assert "оплачено" not in text


def test_booking_notice_marks_what_the_agent_did_not_collect():
    """Пустое поле должно быть видно прочерком: бронь уже стоит, и оператор
    по этой карточке решает, чего в ней не хватает."""
    text = render_booking_notice(
        {**BOOKED_RECORD, "client_name": None, "client_phone": None, "record_id": None}
    )

    assert "Клиент: —, —" in text
    assert "ID записи в YCLIENTS: —" in text


# --------------------------------------------------------------------------
# TAKEOVER_MODE в операторском контуре
# --------------------------------------------------------------------------

def _service(mode: str, minutes: int = 15) -> OpsService:
    return OpsService(
        store=InMemoryOpsStore(),
        settings=Settings(
            telegram_allowed_users=[ALLOWED_USER],
            takeover_mode=mode,
            takeover_cooldown_minutes=minutes,
        ),
    )


async def _took_over(service: OpsService, chat_id: str, minutes_ago: float) -> None:
    await service.store.set_flags(chat_id, ChatFlags(
        is_human_takeover=True,
        takeover_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
    ))


async def test_mode_off_keeps_the_agent_answering_over_a_manager():
    service = _service("off")
    await _took_over(service, "c1", minutes_ago=0)

    allowed, reason = await service.should_agent_reply("c1")

    assert allowed is True, reason


async def test_mode_cooldown_silences_the_agent_inside_the_window():
    service = _service("cooldown", minutes=15)
    await _took_over(service, "c1", minutes_ago=5)

    allowed, reason = await service.should_agent_reply("c1")

    assert allowed is False
    assert reason == "чат у оператора"


async def test_mode_cooldown_returns_the_chat_to_the_agent_after_the_window():
    """«После окна продолжает сам» — и флаг при этом снимается, иначе /chat
    вечно показывает «у оператора» на чате, где агент давно отвечает."""
    service = _service("cooldown", minutes=15)
    await _took_over(service, "c1", minutes_ago=20)

    allowed, _ = await service.should_agent_reply("c1")

    assert allowed is True
    flags = await service.store.get_flags("c1")
    assert flags.is_human_takeover is False
    assert flags.takeover_at is None
    assert any(a["action"] == "auto_return" for a in service.store.actions)


async def test_mode_permanent_keeps_the_chat_until_the_button():
    service = _service("permanent")
    await _took_over(service, "c1", minutes_ago=60 * 5)

    allowed, _ = await service.should_agent_reply("c1")
    assert allowed is False

    await service.return_to_ai("c1", ALLOWED_USER)
    allowed, _ = await service.should_agent_reply("c1")
    assert allowed is True


async def test_a_manager_message_extends_the_silence_window():
    """Окно считается от ПОСЛЕДНЕГО сообщения менеджера: пока он пишет,
    агент молчит, сколько бы времени ни прошло с первого сообщения."""
    service = _service("cooldown", minutes=15)
    await _took_over(service, "c1", minutes_ago=14)

    await service.note_operator_message("c1")

    allowed, _ = await service.should_agent_reply("c1")
    assert allowed is False
    flags = await service.store.get_flags("c1")
    assert (datetime.now(timezone.utc) - flags.takeover_at) < timedelta(seconds=5)


async def test_note_operator_message_raises_the_same_flag_as_the_button():
    """Для системы «человек в чате» — одно состояние, а не два похожих:
    сообщение менеджера и кнопка «Взять на себя» поднимают один флаг, а
    сколько он держится, решает режим на границе исходящих."""
    service = _service("permanent")

    await service.note_operator_message("c1")

    flags = await service.store.get_flags("c1")
    assert flags.is_human_takeover is True
    assert flags.takeover_at is not None
    allowed, _ = await service.should_agent_reply("c1")
    assert allowed is False


# --------------------------------------------------------------------------
# Ручной hold: отдельный флаг, кулдаун его не трогает
# --------------------------------------------------------------------------

class _HoldStore:
    """Минимальная замена dialog_store для /hold и /unhold."""

    def __init__(self):
        self.holds: dict[str, bool] = {}

    async def set_chat_manual_hold(self, chat_id: str, value: bool) -> bool:
        self.holds[chat_id] = value
        return value


async def test_hold_and_unhold_set_the_manual_flag():
    holds = _HoldStore()
    service = OpsService(
        store=InMemoryOpsStore(),
        settings=Settings(telegram_allowed_users=[ALLOWED_USER]),
        manual_hold_setter=holds.set_chat_manual_hold,
    )

    await service.hold("c1", ALLOWED_USER)
    assert holds.holds["c1"] is True

    await service.unhold("c1", ALLOWED_USER)
    assert holds.holds["c1"] is False
    assert [a["action"] for a in service.store.actions] == ["hold", "unhold"]


async def test_hold_refuses_honestly_without_a_database():
    """Без доступа к базе команда обязана сказать «не могу», а не сделать
    вид, что чат заткнут: оператор ставит hold по инциденту."""
    service = OpsService(
        store=InMemoryOpsStore(),
        settings=Settings(telegram_allowed_users=[ALLOWED_USER]),
    )

    answer = await service.hold("c1", ALLOWED_USER)

    assert "недоступен" in answer


async def test_manual_hold_is_not_touched_by_the_cooldown_in_any_mode():
    """Главное свойство ручного hold: он живёт до /unhold. Кулдаун
    истекает, чат возвращается агенту — а hold остаётся, и граница
    исходящих по-прежнему не пропускает ни одного сообщения.

    Флаг для 65 чатов инцидента 2026-08-28: там нужно было заткнуть насмерть,
    а не «на 15 минут»."""
    holds = _HoldStore()
    for mode in ("off", "cooldown", "permanent"):
        service = OpsService(
            store=InMemoryOpsStore(),
            settings=Settings(
                telegram_allowed_users=[ALLOWED_USER],
                takeover_mode=mode,
                takeover_cooldown_minutes=15,
            ),
            manual_hold_setter=holds.set_chat_manual_hold,
        )
        await service.hold("c1", ALLOWED_USER)
        await _took_over(service, "c1", minutes_ago=60)      # окно давно истекло

        await service.should_agent_reply("c1")               # ленивый возврат
        await service.return_to_ai("c1", ALLOWED_USER)       # и явный возврат

        assert holds.holds["c1"] is True, f"режим {mode} снял ручной hold"


async def test_the_in_memory_store_holds_a_chat_like_the_real_one():
    """InMemoryDialogStore — то, на чём стоят тесты конвейера. Если его
    `set_chat_manual_hold` разойдётся с боевой реализацией, тесты будут
    зелёными на поведении, которого в бою нет."""
    from app.dialog_store import InMemoryDialogStore

    store = InMemoryDialogStore()
    await store.get_or_create_chat("c1")

    assert await store.get_chat_manual_hold("c1") is False
    await store.set_chat_manual_hold("c1", True)
    assert await store.get_chat_manual_hold("c1") is True
    await store.set_chat_manual_hold("c1", False)
    assert await store.get_chat_manual_hold("c1") is False


async def test_the_outbound_gate_holds_the_chat_even_when_the_takeover_expired():
    """Тот же инвариант, но на границе: истёкший перехват не открывает
    дорогу сообщению в чат, стоящий на ручном hold."""
    from app.channels.outbound_gate import OutboundGate, TakeoverState

    async def manual_hold(chat_id):
        return True

    async def takeover(chat_id):
        return TakeoverState(is_human_takeover=False, takeover_at=None)

    class _Client:
        async def send_message(self, chat_id, text):
            raise AssertionError("отправки быть не должно")

    gate = OutboundGate(
        _Client(),
        Settings(takeover_mode="cooldown", avito_blocked_items="none"),
        manual_hold_lookup=manual_hold,
        takeover_lookup=takeover,
    )

    assert await gate.is_allowed("c1") is False
