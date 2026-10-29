import pytest


@pytest.fixture(autouse=True)
def _clean_echo_registry():
    """Реестр «вот-вот отправленного» живёт в памяти процесса - не даём
    ему протекать между тестами (одинаковые chat_id и тексты)."""
    from app.channels import echo_registry

    echo_registry.clear()
    yield
    echo_registry.clear()
