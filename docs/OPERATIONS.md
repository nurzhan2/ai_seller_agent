# Эксплуатация: что делать по сигналу самопроверки

Бот сам проверяет себя каждый день в 09:00 МСК и пишет в Telegram оператору
(`TELEGRAM_OPS_CHAT_ID`). Вручную: `/selfcheck` в боте или в консоли Railway
`python -m scripts.selfcheck_now` (`--send` — ещё и в Telegram).

«✅ всё в порядке» — ничего делать не нужно.

| Сигнал | Причина | Что сделать |
|---|---|---|
| Объявление «…» ни с чем не сопоставлено | Новое объявление, по заголовку зону не понять | Строка в `item_zone_map` (`item_id`, `zone_id` или `category`) |
| Цена объявления ниже минимальной цены зоны | Объявление переименовали / сменили объект (так было с беседками 08.10) | Исправить `zone_id`/`category` в `item_zone_map`, сбросить `chats.zone_id` по этому `item_id` |
| Ресурс N в YCLIENTS не найден | Ресурс удалили/пересоздали в YCLIENTS | Новый `staff_id` в `zone_service_map` или `enabled=false` |
| YCLIENTS не отдал расписание | Сбой YCLIENTS или токена | Повторить `/selfcheck`; если держится — проверить токены |
| Правка каталога #N действует N дн. | Цену поменяли через `/menu` и забыли | Временная — откатить в `/menu`; постоянная — внести в `app/kb/catalog.yaml` и откатить правку |
| Тестовое фото не загрузилось | Авито не принимает/не отдаёт картинки | Проверить токен Авито, `app/media/bundled` |
| Бот не ответил клиенту | Сбой хода или отправки | Логи Railway по `chat_id` |
| Проверка не выполнилась | Упал внешний сервис | Повторить позже; если повторяется — логи |
| railway.toml отключается 01.12.2026 | Railway убирает Config as Code | `railway config migrate --service ai_seller_agent`, проверить, что restartPolicy сохранилась, удалить напоминание из `app/ops/selfcheck.py` |

## Деплой

- Пуш в `main` → GitHub Actions (pytest) и Railway (сборка) параллельно.
  Красный крестик в GitHub — откатить коммит.
- Версии библиотек зафиксированы в `constraints.txt` (из прод-контейнера).
  Обновлять осознанно: `pip install -U -r requirements.txt` → `pytest` →
  `pip freeze > constraints.txt`.
- Живой прогон агента без отправки клиентам: `python -m scripts.live_probe [item_id]`.

## Известные решения заказчика, которые ещё не приняты

- Юрта: ресурс удалён из YCLIENTS, `zone_service_map.yurt.enabled=false`.
- Бот молчит в чате после вмешательства менеджера (`TAKEOVER_MODE=permanent`,
  возврат через `TAKEOVER_AUTO_RETURN_HOURS=72`).
- Автобронь: у токена YCLIENTS нет прав на записи (403 на `/records`).
