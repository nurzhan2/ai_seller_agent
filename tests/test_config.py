"""Тесты нормализации DATABASE_URL для Railway (промт №13, 3.3).

Это самая частая причина, по которой приложение не поднимается на Railway
с первого раза: схема postgres:// вместо postgresql+asyncpg://, и
sslmode=require в query, которого asyncpg не понимает как параметр DSN.
"""

from __future__ import annotations

from app.config import normalize_database_url


def test_adds_asyncpg_driver_to_plain_postgres_scheme():
    url, connect_args = normalize_database_url("postgres://u:p@host:5432/db")
    assert url.startswith("postgresql+asyncpg://")
    assert connect_args == {}


def test_adds_asyncpg_driver_to_postgresql_scheme():
    url, connect_args = normalize_database_url("postgresql://u:p@host:5432/db")
    assert url == "postgresql+asyncpg://u:p@host:5432/db"
    assert connect_args == {}


def test_leaves_already_correct_scheme_alone():
    url, _ = normalize_database_url("postgresql+asyncpg://u:p@host:5432/db")
    assert url == "postgresql+asyncpg://u:p@host:5432/db"


def test_sslmode_require_moves_into_connect_args():
    url, connect_args = normalize_database_url(
        "postgres://u:p@host:5432/db?sslmode=require"
    )
    assert "sslmode" not in url
    assert connect_args == {"ssl": True}


def test_sslmode_disable_is_dropped_without_forcing_ssl():
    url, connect_args = normalize_database_url(
        "postgres://u:p@host:5432/db?sslmode=disable"
    )
    assert "sslmode" not in url
    assert connect_args == {}


def test_other_query_params_survive_the_round_trip():
    url, _ = normalize_database_url(
        "postgres://u:p@host:5432/db?sslmode=require&application_name=parmangal"
    )
    assert "application_name=parmangal" in url
    assert "sslmode" not in url


def test_settings_exposes_the_same_normalization():
    from app.config import Settings

    settings = Settings(database_url="postgres://u:p@host:5432/db?sslmode=require")
    url, connect_args = settings.normalized_database_url()
    assert url.startswith("postgresql+asyncpg://")
    assert connect_args == {"ssl": True}


def test_the_agent_does_not_write_first_by_default():
    """«Не пишет первым» держится КОДОМ, а не переменной Railway.

    Дефолт был True, и обещание обоих документов держалось только на
    TOUCH_ENABLED=false в переменных окружения прода. Потеряется переменная
    (новое окружение, восстановление из бэкапа, опечатка) — и агент начнёт
    писать первым молча, без единой строки в логе о том, что что-то
    изменилось. Дефолт должен быть безопасным сам по себе.
    """
    from app.config import Settings

    assert Settings(_env_file=None).touch_enabled is False
