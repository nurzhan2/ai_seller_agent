"""Админка: FastAPI + Jinja2 + HTMX, без SPA.

Самая важная страница здесь — /admin/readiness. Она показывает, какие зоны
агент уже может считать сам, а какие эскалирует, и какие вопросы это
разблокируют. По мере ответов заказчика зоны там «зеленеют» — это рабочий
инструмент коммуникации, а не отчёт для галочки.
"""

from __future__ import annotations

import csv
import io
import secrets
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.booking.mapping import InMemoryZoneMapping, coverage_report
from app.config import get_settings
from app.kb.editor import human_value
from app.kb.loader import KnowledgeBase, audit_readiness, global_blockers, load_catalog

router = APIRouter(prefix="/admin", tags=["admin"])
security = HTTPBasic()


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    settings = get_settings()
    expected_user = getattr(settings, "admin_user", "") or ""
    expected_password = ""
    secret = getattr(settings, "admin_password", None)
    if secret is not None:
        expected_password = secret.get_secret_value()

    if not expected_user or not expected_password:
        # Незаданный пароль не должен означать «вход свободный».
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ADMIN_USER / ADMIN_PASSWORD не заданы — админка отключена",
        )

    ok_user = secrets.compare_digest(credentials.username, expected_user)
    ok_password = secrets.compare_digest(credentials.password, expected_password)
    if not (ok_user and ok_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверные учётные данные",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


_STYLE = """
<style>
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f7f9;color:#1a1a1a}
.wrap{max-width:1100px;margin:0 auto;padding:24px}
nav a{margin-right:14px;color:#0b5ed7;text-decoration:none;font-size:14px}
h1{font-size:22px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e3e5e8;border-radius:8px;overflow:hidden}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid #eef0f2;font-size:14px}
th{background:#fafbfc;font-size:13px;color:#555}
.yes{color:#1e8449;font-weight:600}
.no{color:#c0392b;font-weight:600}
.q{display:inline-block;background:#eef1f5;border-radius:4px;padding:1px 6px;font-size:12px;margin:1px}
.note{color:#666;font-size:13px;margin:8px 0 18px}
</style>
"""

_NAV = """
<nav>
  <a href="/admin/readiness">Готовность зон</a>
  <a href="/admin/catalog">Каталог</a>
  <a href="/admin/dialogs">Диалоги</a>
  <a href="/admin/leads">Лиды</a>
  <a href="/admin/concessions">Уступки</a>
  <a href="/admin/costs">Расход</a>
  <a href="/admin/booking">Бронирование</a>
</nav>
"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"{_STYLE}<div class='wrap'>{_NAV}<h1>{title}</h1>{body}</div>")


def _kb(request: Request) -> KnowledgeBase:
    kb = getattr(request.app.state, "kb", None)
    return kb if kb is not None else load_catalog()


@router.get("/readiness", response_class=HTMLResponse)
async def readiness(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    kb = _kb(request)
    rows = audit_readiness(kb)
    answered = {q.id for q in kb.catalog.open_questions if q.status == "answered"}

    body = [
        "<p class='note'>ready_for_pricing — агент может назвать базовую цену. "
        "ready_for_dialog — может ответить на все типовые вопросы по зоне. "
        "Это независимые колонки: зона может считаться, но не отвечать про акции.</p>",
        "<table><tr><th>зона</th><th>ценовых полей</th><th>спорных</th>"
        "<th>цена</th><th>диалог</th><th>что разблокирует</th></tr>",
    ]
    for row in rows:
        blockers = "".join(f"<span class='q'>{q}</span>" for q in row.dialog_blockers)
        body.append(
            f"<tr><td>{row.zone_id}<br><small>{row.zone_name}</small></td>"
            f"<td>{row.pricing_fields_total}</td>"
            f"<td>{row.pricing_fields_disputed}</td>"
            f"<td class='{'yes' if row.ready_for_pricing else 'no'}'>"
            f"{'да' if row.ready_for_pricing else 'нет'}</td>"
            f"<td class='{'yes' if row.ready_for_dialog else 'no'}'>"
            f"{'да' if row.ready_for_dialog else 'нет'}</td>"
            f"<td>{blockers or '—'}</td></tr>"
        )
    body.append("</table>")

    globals_ = global_blockers(kb)
    body.append("<h1>Общие блокеры</h1>")
    body.append("<p class='note'>Эти вопросы гейтят все зоны одинаково.</p><table>")
    for question in globals_:
        body.append(f"<tr><td><span class='q'>{question.id}</span></td><td>{question.text}</td></tr>")
    body.append("</table>")
    body.append(f"<p class='note'>Отвечено вопросов: {len(answered)}.</p>")
    return _page("Готовность зон", "".join(body))


@router.get("/catalog", response_class=HTMLResponse)
async def catalog(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    kb = _kb(request)
    body = [
        "<p class='note'>Спорные поля перечислены отдельно. По мере ответов "
        "заказчика они закрываются здесь, и зона начинает считаться автоматически.</p>",
        "<table><tr><th>вопрос</th><th>раздел</th><th>текст</th><th>блокирует</th><th>статус</th></tr>",
    ]
    for question in kb.catalog.open_questions:
        body.append(
            f"<tr><td><span class='q'>{question.id}</span></td>"
            f"<td>{question.section or ''}</td><td>{question.text}</td>"
            f"<td class='{'no' if question.blocking else ''}'>"
            f"{'да' if question.blocking else 'нет'}</td>"
            f"<td class='{'yes' if question.status == 'answered' else ''}'>{question.status}</td></tr>"
        )
    body.append("</table>")

    body.append("<h1>Правки из Telegram</h1>")
    body.append(
        "<p class='note'>YAML в git остаётся базой — это слой изменений поверх "
        "него, живёт в БД (файловая система контейнера на Railway эфемерная). "
        "Кто, когда, что было, что стало.</p>"
    )
    editor = getattr(request.app.state, "catalog_editor", None)
    if editor is None:
        body.append("<p class='note'>Журнал правок не подключён.</p>")
    else:
        records = await editor.store.list_journal(limit=200)
        if not records:
            body.append("<p class='note'>Правок ещё не было.</p>")
        else:
            last_active_id = next((r.id for r in records if r.is_active), None)
            body.append(
                "<table><tr><th>когда</th><th>путь</th><th>было</th><th>стало</th>"
                "<th>кто</th><th>статус</th><th></th></tr>"
            )
            for record in records:
                when = record.created_at.strftime("%Y-%m-%d %H:%M") if record.created_at else "—"
                status_html = (
                    f"откачено (user {record.reverted_by})" if not record.is_active else "действует"
                )
                revert_button = ""
                if editor is not None and record.id == last_active_id:
                    revert_button = (
                        "<form method='post' action='/admin/catalog/revert' style='margin:0'>"
                        "<button type='submit'>Откатить</button></form>"
                    )
                body.append(
                    f"<tr><td>{when}</td><td><code>{record.path}</code></td>"
                    f"<td>{human_value(record.previous_value)}</td>"
                    f"<td>{human_value(record.value)}</td>"
                    f"<td>{record.changed_by}</td>"
                    f"<td class='{'yes' if record.is_active else ''}'>{status_html}</td>"
                    f"<td>{revert_button}</td></tr>"
                )
            body.append("</table>")

    return _page("Каталог и спорные поля", "".join(body))


@router.post("/catalog/revert")
async def catalog_revert(request: Request, _: str = Depends(require_admin)) -> RedirectResponse:
    """Откат последней действующей правки. Та же логика, что у кнопки в
    Telegram (app/ops/menu_service.py) — разными путями к одному
    `CatalogEditor.revert_last`, чтобы поведение не разошлось.

    `user_id=0` — сентинел «через админку, не Telegram»: `changed_by`/
    `reverted_by` в БД типа BigInteger под настоящий user_id, а у входа
    через HTTP Basic числового id нет.
    """
    editor = getattr(request.app.state, "catalog_editor", None)
    if editor is not None:
        result = await editor.revert_last(user_id=0)
        on_kb_reloaded = getattr(request.app.state, "on_kb_reloaded", None)
        if result is not None and on_kb_reloaded is not None:
            on_kb_reloaded(result.kb)
    return RedirectResponse(url="/admin/catalog", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/booking", response_class=HTMLResponse)
async def booking(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    kb = _kb(request)
    mapping = getattr(request.app.state, "zone_mapping", None) or InMemoryZoneMapping()
    report = coverage_report(mapping, [z.id for z in kb.catalog.zones])

    provider = getattr(request.app.state, "booking_provider", None)
    services_count = len(await provider.get_services()) if provider is not None else None

    # «Покрытие каталога» вводило в заблуждение: 0% читалось как «у
    # заказчика пуст каталог YCLIENTS», хотя пустая связка — это только
    # zone_service_map (наша сторона), а не сам каталог услуг заказчика.
    # Показываем обе цифры отдельно, чтобы это было видно на глаз.
    if services_count is None:
        services_line = "<p class='note'>система бронирования не подключена — сколько услуг в YCLIENTS, неизвестно</p>"
    else:
        services_line = f"<p>Услуг видно в YCLIENTS: <b>{services_count}</b></p>"

    body = [
        f"<p class='note'>{report['note']}</p>",
        services_line,
        f"<p>Зоны, связанные с услугами YCLIENTS: <b>{report['coverage']:.0%}</b> "
        f"({len(report['mapped'])} из {report['total_zones']})</p>",
        "<table><tr><th>зона</th><th>заведена в YCLIENTS</th></tr>",
    ]
    for zone in kb.catalog.zones:
        mapped = zone.id in report["mapped"]
        body.append(
            f"<tr><td>{zone.id} — {zone.name}</td>"
            f"<td class='{'yes' if mapped else 'no'}'>{'да' if mapped else 'нет'}</td></tr>"
        )
    body.append("</table>")
    return _page("Бронирование", "".join(body))


@router.get("/dialogs", response_class=HTMLResponse)
async def dialogs(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    provider = getattr(request.app.state, "dialog_provider", None)
    if provider is None:
        return _page("Диалоги", "<p class='note'>Источник диалогов не подключён.</p>")
    rows = await provider.list_dialogs()
    body = ["<table><tr><th>чат</th><th>зона</th><th>режим</th><th>сообщений</th></tr>"]
    for row in rows:
        mode = "оператор" if row.get("is_human_takeover") else "ИИ"
        body.append(
            f"<tr><td>{row.get('chat_id')}</td><td>{row.get('zone_id') or '—'}</td>"
            f"<td>{mode}</td><td>{row.get('messages', 0)}</td></tr>"
        )
    body.append("</table>")
    return _page("Диалоги", "".join(body))


@router.get("/leads", response_class=HTMLResponse)
async def leads(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    provider = getattr(request.app.state, "lead_provider", None)
    if provider is None:
        return _page("Лиды", "<p class='note'>Источник лидов не подключён.</p>")
    rows = await provider.list_leads()
    body = [
        "<p class='note'><a href='/admin/leads.csv'>Скачать CSV</a></p>",
        "<table><tr><th>имя</th><th>телефон</th><th>зона</th><th>дата</th><th>гостей</th></tr>",
    ]
    for row in rows:
        body.append(
            f"<tr><td>{row.get('name') or '—'}</td><td>{row.get('phone')}</td>"
            f"<td>{row.get('zone_id') or '—'}</td><td>{row.get('date') or '—'}</td>"
            f"<td>{row.get('guests') or '—'}</td></tr>"
        )
    body.append("</table>")
    return _page("Лиды", "".join(body))


@router.get("/leads.csv")
async def leads_csv(request: Request, _: str = Depends(require_admin)) -> StreamingResponse:
    provider = getattr(request.app.state, "lead_provider", None)
    rows = await provider.list_leads() if provider else []

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["name", "phone", "zone_id", "date", "guests", "notes"])
    for row in rows:
        writer.writerow(
            [row.get("name"), row.get("phone"), row.get("zone_id"),
             row.get("date"), row.get("guests"), row.get("notes")]
        )
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=leads.csv"},
    )


@router.get("/concessions", response_class=HTMLResponse)
async def concessions(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    provider = getattr(request.app.state, "concession_provider", None)
    if provider is None:
        return _page("Уступки", "<p class='note'>Журнал уступок не подключён.</p>")
    rows = await provider.list_concessions()
    total = sum(Decimal(str(r.get("revenue_delta") or 0)) for r in rows)
    body = [
        f"<p class='note'>Всего уступок: {len(rows)}, недополучено {total} ₽</p>",
        "<table><tr><th>чат</th><th>ступень</th><th>триггер</th>"
        "<th>недополучено</th><th>база расчёта</th><th>правило</th></tr>",
    ]
    for row in rows:
        provisional = "предварительное" if row.get("provisional_policy") else "подтверждено"
        body.append(
            f"<tr><td>{row.get('dialog_id')}</td><td>{row.get('tier')}</td>"
            f"<td>{row.get('trigger') or '—'}</td><td>{row.get('revenue_delta')}</td>"
            f"<td>{row.get('revenue_delta_basis') or '—'}</td><td>{provisional}</td></tr>"
        )
    body.append("</table>")
    return _page("Журнал уступок", "".join(body))


@router.get("/costs", response_class=HTMLResponse)
async def costs(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    provider = getattr(request.app.state, "cost_provider", None)
    settings = get_settings()
    if provider is None:
        return _page("Расход", "<p class='note'>Источник расходов не подключён.</p>")
    rows = await provider.list_costs()
    body = [
        f"<p class='note'>Дневной лимит: {settings.daily_cost_limit_rub} ₽. "
        "При превышении агент автоматически уходит на паузу.</p>",
        "<table><tr><th>дата</th><th>провайдер</th><th>модель</th><th>диалогов</th>"
        "<th>расход, ₽</th><th>на диалог</th></tr>",
    ]
    for row in rows:
        body.append(
            f"<tr><td>{row.get('date')}</td><td>{row.get('llm_provider') or '—'}</td>"
            f"<td>{row.get('model')}</td>"
            f"<td>{row.get('dialogs')}</td><td>{row.get('cost_rub')}</td>"
            f"<td>{row.get('cost_per_dialog')}</td></tr>"
        )
    body.append("</table>")
    return _page("Расход на модели", "".join(body))


@router.get("/prompt", response_class=HTMLResponse)
async def prompt(request: Request, _: str = Depends(require_admin)) -> HTMLResponse:
    from app.agent.prompts import build_system_prompt

    blocks = build_system_prompt(_kb(request))
    body = ["<p class='note'>Системный промт целиком. Секция тона правится в app/agent/prompts.py.</p>"]
    for block in blocks:
        cached = " (кешируется)" if block.get("cache_control") else ""
        body.append(f"<h1>Блок{cached}</h1><pre style='white-space:pre-wrap'>{block['text']}</pre>")
    return _page("Системный промт", "".join(body))
