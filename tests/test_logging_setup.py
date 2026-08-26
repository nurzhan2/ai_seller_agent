"""app/logging_setup.py — extra={...} обязан быть виден в тексте лога.

Повод: `extra={"chat_id": ...}` используется по всему проекту как
единственный способ привязать диагностику к записи (см. app/pipeline.py).
`logging.basicConfig()` со стандартным форматом эти поля не печатает вообще
— они оседают в `record.__dict__`, но текстовый вывод (и логи Railway)
показывают голую строку без единого значения. Диагностика "pipeline:
message without text" (chat_id/type/top_level_keys) была именно такой
жертвой: код был правильным, а поле молча терялось на выводе.

`caplog` здесь не подходит — он ловит `record.__dict__` напрямую, тем же
способом, каким этот баг и остаётся незамеченным. Тесты ниже гоняют
реальный `StreamHandler` + `ExtraFieldsFormatter` и читают ИМЕННО ТЕКСТ,
который получился бы в консоли/логах Railway.
"""

from __future__ import annotations

import io
import logging

import pytest

from app.logging_setup import LOG_FORMAT, ExtraFieldsFormatter, configure_logging

LOGGER_NAME = "parmangal.test_logging_setup"


def _capture(logger: logging.Logger, log_call) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ExtraFieldsFormatter(LOG_FORMAT))
    logger.addHandler(handler)
    logger.propagate = False
    # Иначе эффективный уровень наследуется от root (по умолчанию WARNING),
    # и .info()/.warning() ниже него молча не доходят даже до хендлера —
    # тест прошёл бы «зелёным по пустой строке», ничего не проверив.
    previous_level = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        log_call(logger)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
    return stream.getvalue()


def test_extra_field_appears_in_the_formatted_line():
    """Голый logging.Formatter эту строку не поймал бы — извлечь
    chat_id обратно можно было бы только из record.__dict__, не из текста,
    ровно как это и произошло в проде."""
    logger = logging.getLogger(LOGGER_NAME + ".basic")

    output = _capture(
        logger,
        lambda lg: lg.info("pipeline: message without text", extra={"chat_id": "chat-42"}),
    )

    assert "chat_id=chat-42" in output


def test_several_extra_fields_all_appear():
    logger = logging.getLogger(LOGGER_NAME + ".several")

    output = _capture(
        logger,
        lambda lg: lg.info(
            "pipeline: message without text — type=%s", "image",
            extra={"chat_id": "chat-1", "reason": "no online booking"},
        ),
    )

    assert "chat_id=chat-1" in output
    assert "reason=no online booking" in output
    assert "type=image" in output   # из самого текста сообщения, не из extra


def test_message_without_extra_is_unchanged():
    """Ничего лишнего не приписывается, когда extra не передан — иначе
    каждая обычная строка лога обрастала бы пустым хвостом."""
    logger = logging.getLogger(LOGGER_NAME + ".none")

    output = _capture(logger, lambda lg: lg.warning("plain message"))

    assert output.strip() == "WARNING:" + logger.name + ":plain message"
    assert "|" not in output


def test_standard_record_attributes_are_not_treated_as_extra():
    """funcName/lineno/etc. — обычные атрибуты LogRecord, не extra
    оператора; формат не обязан дублировать то, что и так есть в базовой
    части строки. Регрессия здесь означала бы, что _STANDARD_RECORD_ATTRS
    разъехался с реальным набором атрибутов LogRecord (например, после
    смены версии Python)."""
    logger = logging.getLogger(LOGGER_NAME + ".standard_attrs")

    output = _capture(logger, lambda lg: lg.info("just a message"))

    assert "|" not in output


def test_configure_logging_installs_the_extra_fields_formatter(monkeypatch):
    """Интеграционная проверка на реальном пути настройки: не только сам
    форматтер умеет печатать extra, но и `configure_logging()` — то, что
    реально вызывается при старте приложения и в scripts/*.py — обязано
    его установить, а не какой-то другой."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    try:
        configure_logging(level=logging.INFO)
        stream = io.StringIO()
        assert len(root.handlers) == 1
        root.handlers[0].stream = stream

        logging.getLogger("parmangal.integration_check").info(
            "pipeline: message without text", extra={"chat_id": "chat-99"}
        )

        assert "chat_id=chat-99" in stream.getvalue()
    finally:
        root.handlers = original_handlers
        root.level = original_level
