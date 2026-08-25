"""app/ops/menu_service.py — логика инлайн-меню, без Telegram и без сети.

Хендлеры (app/ops/handlers.py) — тонкая обёртка над этим сервисом, как и
OpsService; здесь проверяется поведение, там — только разбор callback_data.
"""

from __future__ import annotations

import pytest

from app.kb.editor import CatalogEditor
from app.kb.override_store import InMemoryOverrideStore
from app.config import Settings
from app.ops.bot import OpsService
from app.ops.menu_service import MenuService
from app.ops.state import InMemoryOpsStore

ALLOWED_USER = 111
OTHER_ALLOWED_USER = 222
STRANGER = 999


@pytest.fixture
def settings():
    return Settings(telegram_allowed_users=[ALLOWED_USER, OTHER_ALLOWED_USER], dry_run=True)


@pytest.fixture
def ops(settings):
    return OpsService(store=InMemoryOpsStore(), settings=settings)


@pytest.fixture
def editor():
    return CatalogEditor(InMemoryOverrideStore())


@pytest.fixture
def reloaded():
    calls = []
    return calls


@pytest.fixture
def menu(editor, settings, ops, reloaded):
    return MenuService(
        editor=editor, settings=settings, ops_service=ops,
        on_kb_reloaded=lambda kb: reloaded.append(kb),
    )


# --------------------------------------------------------------------------
# Доступ — только TELEGRAM_ALLOWED_USERS
# --------------------------------------------------------------------------

async def test_stranger_is_denied_the_root_menu(menu):
    reply = await menu.root(STRANGER)
    assert "Нет доступа" in reply.text


async def test_stranger_cannot_open_a_section(menu):
    reply = await menu.open_section(STRANGER, "prices")
    assert "Нет доступа" in reply.text


async def test_stranger_cannot_start_an_edit(menu):
    reply = await menu.start_edit(STRANGER, "we_hour", "dome_bags")
    assert "Нет доступа" in reply.text


async def test_stranger_cannot_confirm_even_with_a_valid_token(menu, editor):
    """Ключевой сценарий задачи: правка от чужого user_id отклоняется —
    даже если у него как-то оказался валидный токен подтверждения."""
    allowed_preview = await menu.start_edit(ALLOWED_USER, "we_hour", "dome_bags")
    assert "Нет доступа" not in allowed_preview.text
    await menu.receive_value(ALLOWED_USER, "1800")
    token = menu.pending[ALLOWED_USER].token

    reply = await menu.confirm(STRANGER, token)

    assert "Нет доступа" in reply.text
    assert await editor.store.list_active() == []      # ничего не сохранилось


async def test_stranger_cannot_revert(menu):
    reply = await menu.revert_last(STRANGER)
    assert "Нет доступа" in reply.text


async def test_stranger_receive_value_is_denied_not_ignored(menu):
    """Без pending-состояния чужой текст просто игнорируется (None) — это
    не про доступ. Но если у постороннего КАК-ТО завёлся pending (не должно
    происходить, поскольку start_edit его для чужих не создаёт), доступ
    всё равно проверяется."""
    menu.pending[STRANGER] = menu.pending.get(STRANGER)
    from app.ops.menu_service import PendingEdit
    menu.pending[STRANGER] = PendingEdit(token="x", field_key="we_hour", zone_id="dome_bags")

    reply = await menu.receive_value(STRANGER, "1800")

    assert reply is not None
    assert "Нет доступа" in reply.text


async def test_allowed_user_can_edit_end_to_end(menu, editor):
    await menu.start_edit(ALLOWED_USER, "we_hour", "dome_bags")
    preview_reply = await menu.receive_value(ALLOWED_USER, "1800")
    assert "было" in preview_reply.text and "1500" in preview_reply.text
    token = menu.pending[ALLOWED_USER].token

    reply = await menu.confirm(ALLOWED_USER, token)

    assert "Сохранено" in reply.text
    active = await editor.store.list_active()
    assert len(active) == 1
    assert active[0].changed_by == ALLOWED_USER


# --------------------------------------------------------------------------
# Навигация
# --------------------------------------------------------------------------

async def test_root_shows_the_five_menu_items(menu):
    reply = await menu.root(ALLOWED_USER)
    labels = [b.text for row in reply.markup.inline_keyboard for b in row]
    for expected in ("💰 Цены и услуги", "🕐 График работы", "📊 Статистика",
                     "⚙️ Режим работы", "📋 Диалоги"):
        assert expected in labels


async def test_open_prices_lists_zones(menu):
    reply = await menu.open_section(ALLOWED_USER, "prices")
    labels = [b.text for row in reply.markup.inline_keyboard for b in row]
    assert any("Купол" in label for label in labels)


async def test_open_zone_shows_current_values(menu):
    reply = await menu.open_zone(ALLOWED_USER, "dome_bags")
    assert "1500" in reply.text          # weekend_per_hour текущее значение


async def test_open_schedule_shows_working_window(menu):
    reply = await menu.open_section(ALLOWED_USER, "schedule")
    assert "09:00" in reply.text and "23:00" in reply.text


async def test_open_mode_shows_current_settings(menu, settings):
    reply = await menu.open_section(ALLOWED_USER, "mode")
    assert settings.moderation_mode in reply.text


async def test_stats_without_provider_says_so(menu):
    reply = await menu.open_section(ALLOWED_USER, "stats")
    assert "недоступна" in reply.text


async def test_dialogs_without_provider_says_so(menu):
    reply = await menu.open_section(ALLOWED_USER, "dialogs")
    assert "не подключён" in reply.text


# --------------------------------------------------------------------------
# Ввод значения
# --------------------------------------------------------------------------

async def test_receive_value_without_pending_state_returns_none(menu):
    """Обычное сообщение в чате — не ответ на запрос значения."""
    assert await menu.receive_value(ALLOWED_USER, "привет") is None


async def test_invalid_value_keeps_the_pending_state_for_retry(menu):
    await menu.start_edit(ALLOWED_USER, "we_hour", "dome_bags")

    reply = await menu.receive_value(ALLOWED_USER, "минус пятьсот")

    assert "⚠️" in reply.text
    assert ALLOWED_USER in menu.pending      # можно прислать другое значение

    good = await menu.receive_value(ALLOWED_USER, "1800")
    assert "станет" in good.text


async def test_cancel_clears_pending_state():
    editor = CatalogEditor(InMemoryOverrideStore())
    settings = Settings(telegram_allowed_users=[ALLOWED_USER])
    ops = OpsService(store=InMemoryOpsStore(), settings=settings)
    menu = MenuService(editor=editor, settings=settings, ops_service=ops)

    await menu.start_edit(ALLOWED_USER, "we_hour", "dome_bags")
    await menu.cancel(ALLOWED_USER, "whatever")

    assert ALLOWED_USER not in menu.pending


async def test_confirm_with_stale_token_is_rejected(menu):
    """Токен привязан к КОНКРЕТНОМУ preview — «Сохранить» на устаревшей
    карточке не должно применить чужое значение."""
    await menu.start_edit(ALLOWED_USER, "we_hour", "dome_bags")
    await menu.receive_value(ALLOWED_USER, "1800")

    reply = await menu.confirm(ALLOWED_USER, "not-the-real-token")

    assert "неактуальна" in reply.text


# --------------------------------------------------------------------------
# Перезагрузка KB после правки/отката
# --------------------------------------------------------------------------

async def test_confirm_triggers_kb_reload_callback(menu, reloaded):
    await menu.start_edit(ALLOWED_USER, "we_hour", "dome_bags")
    await menu.receive_value(ALLOWED_USER, "1800")
    token = menu.pending[ALLOWED_USER].token

    await menu.confirm(ALLOWED_USER, token)

    assert len(reloaded) == 1
    zone = next(z for z in reloaded[0].catalog.zones if z.id == "dome_bags")
    assert zone.pricing["weekend_per_hour"]["value"] == 1800


async def test_revert_triggers_kb_reload_callback(menu, reloaded):
    await menu.start_edit(ALLOWED_USER, "we_hour", "dome_bags")
    await menu.receive_value(ALLOWED_USER, "1800")
    token = menu.pending[ALLOWED_USER].token
    await menu.confirm(ALLOWED_USER, token)
    reloaded.clear()

    await menu.revert_last(ALLOWED_USER)

    assert len(reloaded) == 1
    zone = next(z for z in reloaded[0].catalog.zones if z.id == "dome_bags")
    assert zone.pricing["weekend_per_hour"]["value"] == 1500


# --------------------------------------------------------------------------
# Режим работы
# --------------------------------------------------------------------------

async def test_set_moderation_updates_settings(menu, settings):
    await menu.set_moderation(ALLOWED_USER, "off")
    assert settings.moderation_mode == "off"


async def test_toggle_dry_run(menu, settings):
    await menu.toggle(ALLOWED_USER, "dry_off")
    assert settings.dry_run is False
    await menu.toggle(ALLOWED_USER, "dry_on")
    assert settings.dry_run is True


async def test_toggle_pause(menu, settings):
    await menu.toggle(ALLOWED_USER, "pause")
    assert settings.agent_paused is True
    await menu.toggle(ALLOWED_USER, "resume")
    assert settings.agent_paused is False
