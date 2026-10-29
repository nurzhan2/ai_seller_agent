"""Гонка эха (аудит 30.10): эхо нашего ответа приходит раньше записи в базу.

Чат DZtf 29.10 15:57 - перехват «менеджер написал» без единого сообщения
менеджера, в ту же секунду, что ответ бота. При фото окно ещё шире: ответ
пишется в базу только после загрузки всех картинок.
"""

from __future__ import annotations

from app.channels import echo_registry
from tests.test_outbound_gate import _gate
from tests.test_pipeline import OUR_USER_ID, _build, _payload, _settle


async def test_the_gate_remembers_the_text_before_sending():
    gate, client = _gate()
    await gate.send_message("chat-1", "Баня свободна с 18:00")
    assert echo_registry.is_expected("chat-1", "Баня свободна с 18:00")
    assert not echo_registry.is_expected("chat-2", "Баня свободна с 18:00")


async def test_an_echo_that_beats_the_database_is_not_a_takeover():
    pipeline, store, agent, ops = _build()
    await store.get_or_create_chat("chat-1")
    echo_registry.expect("chat-1", "Отправляю фото бани 🍃")   # отправлено, в базе ещё нет

    await pipeline.handle_message(_payload(author_id=OUR_USER_ID, text="Отправляю фото бани 🍃"))
    await _settle()

    assert (await ops.store.get_flags("chat-1")).is_human_takeover is False


async def test_a_real_manager_message_is_still_a_takeover():
    pipeline, store, agent, ops = _build()
    await store.get_or_create_chat("chat-1")
    echo_registry.expect("chat-1", "Отправляю фото бани 🍃")

    await pipeline.handle_message(_payload(author_id=OUR_USER_ID, text="Мы забронировали с вами баню."))
    await _settle()

    assert (await ops.store.get_flags("chat-1")).is_human_takeover is True


def test_the_memory_expires():
    echo_registry.expect("c", "текст", now=0.0)
    assert echo_registry.is_expected("c", "текст", now=10.0)
    assert not echo_registry.is_expected("c", "текст", now=echo_registry.TTL_SECONDS + 1)
