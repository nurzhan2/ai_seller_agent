"""Гигиена логов: вычищаем секреты из query string.

Зачем это существует. Авито принимает параметры `POST /token` в QUERY STRING
(так в спеке), а httpx на уровне INFO логирует полный URL запроса. В итоге
`client_secret` попадает в обычный лог приложения — и дальше в любой сборщик
логов, куда этот лог уезжает.

Тест `test_token_never_appears_in_logs` держит это свойство. Не удаляйте
фильтр «за ненадобностью»: без него утечка возвращается молча, и заметить её
можно будет только в чужом Kibana.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from typing import Iterator

# client_secret=..., client_id=..., access_token=..., code=...
_SECRET_QUERY_PARAM = re.compile(
    r"(?i)\b(client_secret|client_id|access_token|refresh_token|secret|api_key)"
    r"=([^&\s\"'|]+)"
)

REDACTED = r"\1=***"


def redact(text: str) -> str:
    return _SECRET_QUERY_PARAM.sub(REDACTED, text)


class SecretRedactingFilter(logging.Filter):
    """Переписывает сообщение и аргументы записи, вычищая секреты.

    Именно фильтр, а не «выключить логгер httpx»: полезные строки про запросы
    остаются, исчезают только значения секретов.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._clean(v) for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._clean(a) for a in record.args)
        return True

    @staticmethod
    def _clean(value: object) -> object:
        # httpx кладёт в args объект URL, а не строку — приводим к строке,
        # иначе секрет уедет в лог через __str__ уже на форматировании.
        if isinstance(value, str):
            return redact(value)
        text = str(value)
        cleaned = redact(text)
        return cleaned if cleaned != text else value


_FILTER = SecretRedactingFilter()

# Логгеры, которые печатают URL целиком.
_RISKY_LOGGERS = ("httpx", "httpcore", "urllib3")


def configure_logging(level: int = logging.INFO) -> None:
    """Ставится один раз на старте приложения."""
    logging.basicConfig(level=level)
    for name in _RISKY_LOGGERS:
        logging.getLogger(name).addFilter(_FILTER)
    for handler in logging.getLogger().handlers:
        handler.addFilter(_FILTER)


@contextmanager
def redact_http_logs() -> Iterator[None]:
    """Гарантирует фильтр на время конкретного запроса с секретом в URL.

    Нужен отдельно от `configure_logging`, потому что тесты и сторонний код
    поднимают логирование сами и могут не вызвать настройку приложения.
    """
    loggers = [logging.getLogger(name) for name in _RISKY_LOGGERS]
    added = [lg for lg in loggers if _FILTER not in lg.filters]
    for lg in added:
        lg.addFilter(_FILTER)
    try:
        yield
    finally:
        for lg in added:
            lg.removeFilter(_FILTER)
