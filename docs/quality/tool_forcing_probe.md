# Живой замер принуждения инструмента

Провайдер: **deepseek**, модель **deepseek-v4-pro**, по **5** повторов на случай в каждом плече.

«Принуждение» — прод после правки: `tool_choice {"type": "tool", "name": ...}` на первом витке. «Контроль» — тот же ход с заглушённым `forced_tool_for`, то есть прод до неё. Одно плечо без другого ничего не доказывает: у DeepSeek вызов инструмента недетерминирован.

Столбец «позвал именно его» читать отдельно: провайдер вправе понять просьбу как «позови хоть что-нибудь», и тогда предыдущий столбец полон, а этот пуст. У последнего случая принуждения нет вовсе, и успех там — пустой столбец «позвал хоть что-то».

ЦИФРЫ — ЭТО ОДИН ПРОГОН В ОДИН МОМЕНТ, а не свойство модели. Поведение чужого слоя совместимости меняется во времени: 2026-09-02 случай `window_relative_day` дал 0 вызовов из 5, а он же через час — 5 из 5, на том же коде и том же тексте. Поэтому принуждение не защита, а способ дать последнему рубежу что пропускать; защита — сам рубеж.

| случай | просим | позвал хоть что-то | позвал именно его | без принуждения (контроль) |
|---|---|---|---|---|
| `incident_window_today` | check_availability | 5/5 | 3/5 | 0/5 |
| `incident_second_turn` | check_availability | 4/5 | 4/5 | 0/5 |
| `window_relative_day` | resolve_date | 5/5 | 0/5 | 0/5 |
| `nearest_available` | find_next_available | 5/5 | 0/5 | 0/5 |
| `concrete_booking` | check_availability | 5/5 | 0/5 | 0/5 |
| `price_with_hours` | calculate_price | 5/5 | 2/5 | 0/5 |
| `price_bare` | _ничего_ | 0/5 | — | 0/5 |
| `price_after_zone` | calculate_price | 5/5 | 4/5 | 0/5 |
| `callback_tomorrow` | _ничего_ | 0/5 | — | 0/5 |

## По случаям

### `incident_window_today`

Клиент: «на сегодня есть окошко 4 часа , нас 6теро»

дословный текст клиента из инцидента 2026-09-01: дата, время и вопрос про занятость в одном сообщении.

**С принуждением:**

1. `check_availability, check_availability, check_availability, escalate_to_human`
   - аргументы: `{"zone_id": "bath_russian", "date": "2026-09-02", "start_time": "18:00", "hours": 4}`
   - ответ: Здравствуйте! На сегодня уточню занятость у менеджера и вернусь с ответом. ☀️  Подскажите только, чтобы подобрать под вашу компанию из 6 человек: вам баня, купол или гриль-домик интересует?
2. `check_availability, check_availability, check_availability, check_availability, check_availability, check_availability, check_availability, check_availability, escalate_to_human`
   - аргументы: `{"zone_id": "bath_russian", "date": "2026-09-02", "start_time": "00:00", "hours": 4}`
   - ответ: Здравствуйте! Меня зовут Иришка. Уточню у менеджера свободное время на сегодня и вернусь к вам с ответом — дайте пару минут 🌸
3. `find_next_available, find_next_available, find_next_available, escalate_to_human  ← просили `check_availability`, первым позвал другой`
   - ответ: Здравствуйте! Уточню у менеджера по свободным окошкам на сегодня и вернусь к вам с ответом. Подскажите, кстати, что вас интересует — баня, купол или гриль-домик?
4. `check_availability`
   - аргументы: `{"zone_id": "dummy", "date": "2026-09-02", "start_time": "14:00", "hours": 4}`
   - ответ: Здравствуйте! Подскажите, что вас интересует: баня, купол или гриль-домик? А я пока уточню занятость на сегодня по вашему времени.
5. `find_next_available, escalate_to_human  ← просили `check_availability`, первым позвал другой`
   - ответ: Пока не знаю, какая зона вам нужна, но по свободному времени сегодня уточню у менеджера и вернусь с ответом. А вы пока подскажите, что вас интересует — баня, купол, гриль-домик или шатёр?

**Без принуждения (контроль):**

1. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
2. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
3. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
4. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
5. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given

### `incident_second_turn`

Клиент: «сегодня 16 00»

ВТОРОЙ ход того же инцидента: слова про занятость в самом сообщении нет, признак остался в предыдущей реплике.

**С принуждением:**

1. `— ни одного —`
   - ответ: Конечно, сейчас передам менеджеру — он свяжется с вами.
2. `check_availability, escalate_to_human`
   - аргументы: `{"date": "2026-09-02", "start_time": "16:00", "hours": 4}`
   - ответ: Уточню занятость на сегодня 16:00 и вернусь с ответом. Если появятся вопросы — пишите!
3. `check_availability, check_availability, check_availability, escalate_to_human`
   - аргументы: `{"zone_id": "bath_knight", "date": "2026-09-02", "start_time": "16:00", "hours": 4}`
   - ответ: Сейчас уточню наличие на сегодня, 2 сентября, к 16:00, и вернусь с ответом. Какую баню предпочитаете — «Русский стиль», «Гараж» или «Рыцарскую»?
4. `check_availability, check_availability, check_availability, escalate_to_human`
   - аргументы: `{"zone_id": "bath_russian", "date": "2026-09-02", "start_time": "16:00", "hours": 4}`
   - ответ: Уточню у менеджера и вернусь к вам с ответом — спасибо, что подождали! 💫
5. `check_availability, escalate_to_human`
   - аргументы: `{"zone_id": "all", "date": "2026-09-02", "start_time": "16:00", "hours": 4}`
   - ответ: Уже уточняю у менеджера по свободному времени на сегодня с 16:00 — скоро вернусь к вам с ответом.

**Без принуждения (контроль):**

1. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
2. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
3. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
4. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
5. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given

### `window_relative_day`

Клиент: «Есть окошко сегодня?»

относительная дата без времени — сначала разобрать «сегодня» в число, считать это в уме модели нельзя (инциденты 31.08 и 01.09).

**С принуждением:**

1. `check_availability  ← просили `resolve_date`, первым позвал другой`
   - ответ: Здравствуйте! Меня зовут Иришка — администратор ПарМангала. ☀️  Подскажите, что вас интересует: баня, купол, гриль-домик или шатёр? Тогда подберу свободное время на сегодня.
2. `check_availability  ← просили `resolve_date`, первым позвал другой`
   - ответ: Здравствуйте! С удовольствием помогу — подскажите, что вас интересует: баня, купол, гриль-домик или шатёр? 🏕️
3. `check_availability  ← просили `resolve_date`, первым позвал другой`
   - ответ: Здравствуйте! Подскажите, что вас интересует — баня, купол, гриль-домик или шатёр? И на какое время планируете отдых?
4. `find_next_available, escalate_to_human  ← просили `resolve_date`, первым позвал другой`
   - ответ: Здравствуйте! Подскажите, что вас интересует: баня, купол, гриль-домик или шатёр? А по свободному окну на сегодня уточню у менеджера и вернусь с ответом.
5. `check_availability, escalate_to_human  ← просили `resolve_date`, первым позвал другой`
   - ответ: Уточню у менеджера и вернусь с ответом. А пока подскажите, пожалуйста, что вас интересует — так смогу дать точные варианты. 🌸

**Без принуждения (контроль):**

1. `— ни одного —`
2. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
3. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
4. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
5. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given

### `nearest_available`

Клиент: «когда ближайшее свободное?»

числа клиент не называет — спрашивать его у клиента незачем.

**С принуждением:**

1. `get_zones  ← просили `find_next_available`, первым позвал другой`
   - ответ: Здравствуйте! Меня зовут Иришка. Подскажите, что вас интересует: баня, купол, гриль-домик или шатёр?
2. `get_zones  ← просили `find_next_available`, первым позвал другой`
   - ответ: Здравствуйте! Меня зовут Иришка, подскажите, что вас интересует: баня, купол, гриль-домик или шатёр? Тогда подберу ближайшие свободные даты 🌸
3. `get_zones  ← просили `find_next_available`, первым позвал другой`
   - ответ: Здравствуйте! Подскажите, что вас интересует: баня, купол, гриль-домик или шатёр? 🌞
4. `get_zones  ← просили `find_next_available`, первым позвал другой`
   - ответ: Здравствуйте! Меня зовут Иришка. Подскажите, что вас интересует — баня, купол, гриль-домик или шатёр?
5. `get_zones  ← просили `find_next_available`, первым позвал другой`
   - ответ: Здравствуйте! Меня зовут Иришка, подскажите, что вас интересует: баня, купол, гриль-домик или шатёр? 🍀

**Без принуждения (контроль):**

1. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
2. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
3. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
4. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
5. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given

### `concrete_booking`

Клиент: «можно записаться на 5 сентября в 18:00?»

конкретная дата плюс слово про запись.

**С принуждением:**

1. `resolve_date  ← просили `check_availability`, первым позвал другой`
   - ответ: Здравствуйте! Меня зовут Иришка. Подскажите, что вас интересует — баня, купол, гриль-домик или шатёр?  5 сентября — это, насколько я понимаю, суббота. Уточню по дате позже, а пока подскажите зону, что
2. `resolve_date  ← просили `check_availability`, первым позвал другой`
   - ответ: Здравствуйте! Меня зовут Иришка, администратор «ПарМангала». Подскажите, что вас интересует: баня, купол, гриль-домик или шатёр?  А по дате — уточните, пожалуйста, число: это 5 сентября 2026 года? 😊
3. `resolve_date  ← просили `check_availability`, первым позвал другой`
   - ответ: Здравствуйте! Меня зовут Иришка. Подскажите, что вас интересует: баня, купол, гриль-домик или шатёр?
4. `resolve_date  ← просили `check_availability`, первым позвал другой`
   - ответ: Здравствуйте! Меня зовут Иришка, подскажите, что вас интересует: баня, купол, гриль-домик или шатёр?
5. `resolve_date  ← просили `check_availability`, первым позвал другой`
   - ответ: Здравствуйте! Меня зовут Иришка. Подскажите, а что вас интересует — баня, купол, гриль-домик или шатёр? И уточните, на какое число: 5 сентября выпадает на субботу, всё верно?

**Без принуждения (контроль):**

1. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
2. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
3. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
4. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
5. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given

### `price_with_hours`

Клиент: «Сколько стоит баня на 4 часа?»

цена: без вызова инструмента её нельзя называть вообще.

**С принуждением:**

1. `calculate_price`
   - аргументы: `{"zone_id": "bath_russian", "date": "2026-09-02", "hours": 4}`
   - ответ: Здравствуйте! Меня зовут Иришка, администратор «ПарМангала». Подскажите, какая баня вас интересует — «Русский стиль», «Гараж» или «Рыцарская»?  И со скольки планируете заезд?
2. `calculate_price`
   - аргументы: `{"zone_id": "?need_input", "date": "2026-09-02"}`
   - ответ: Здравствуйте! Меня зовут Иришка. У нас три бани — «Русский стиль» (до 12 чел.), «Гараж» (до 10 чел.) и «Рыцарская» (до 6 чел.). Подскажите, какая вас интересует и на какую дату планируете?
3. `get_zones  ← просили `calculate_price`, первым позвал другой`
   - ответ: Здравствуйте! Меня зовут Иришка. Подскажите, какая баня вас интересует — «Русский стиль», «Гараж» или «Рыцарская»?
4. `get_zones  ← просили `calculate_price`, первым позвал другой`
   - ответ: Здравствуйте! Меня зовут Иришка. Какая баня вас интересует — «Русский стиль», «Гараж» или «Рыцарская»?
5. `invoke_accept  ← просили `calculate_price`, первым позвал другой`
   - ответ: Здравствуйте! Меня зовут Иришка, администратор «ПарМангала». Подскажите, что вас интересует: баня, купол, гриль-домик или шатёр?

**Без принуждения (контроль):**

1. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
2. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
3. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
4. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
5. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given

### `price_bare`

Клиент: «а цена какая?»

цена без зоны и без даты — принуждать НЕЧЕГО. calculate_price требует zone_id и date; принуждение здесь заставляло модель выдумать оба поля, получить needs_input и спросить у клиента всё разом. Замер 2026-09-02: доля ответов больше чем с одним вопросом 18% против 2% без принуждения.

**С принуждением:**

1. `— ни одного —`
   - ответ: Здравствуйте! Меня зовут Иришка, администратор «ПарМангала». Подскажите, что вас интересует: баня, купол, гриль-домик или шатёр?
2. `— ни одного —`
   - ответ: Здравствуйте! Меня зовут Иришка. Цена зависит от зоны и дня — подскажите, что вас интересует: баня, купол, гриль-домик или шатёр?
3. `— ни одного —`
   - ответ: Здравствуйте! Подскажите, что вас интересует: баня, купол, гриль-домик или шатёр?
4. `— ни одного —`
   - ответ: Здравствуйте! Меня зовут Иришка. Подскажите, что вас интересует: баня, купол, гриль-домик или шатёр? От этого зависит стоимость.
5. `— ни одного —`
   - ответ: Здравствуйте! Меня зовут Иришка, администратор ПарМангала. Подскажите, что вас интересует: баня, купол, гриль-домик или шатёр?

**Без принуждения (контроль):**

1. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
2. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
3. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
4. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
5. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given

### `price_after_zone`

Клиент: «а цена какая?»

тот же вопрос, но зона уже названа ходом раньше — вызов снова осмыслен. Именно этот случай и делает сужение безопасным: разговор почти всегда начинается с зоны.

**С принуждением:**

1. `calculate_price`
   - аргументы: `{"zone_id": "bath_russian", "date": "2026-09-02"}`
   - ответ: Подскажите, на сколько часов планируете баню?
2. `calculate_price`
   - аргументы: `{"zone_id": "bath_russian", "date": "2026-09-02"}`
   - ответ: Подскажите, на сколько часов планируете баню? И на какое число — сегодня или другой день?
3. `get_zones  ← просили `calculate_price`, первым позвал другой`
   - ответ: У нас три бани: «Русский стиль», «Гараж» и «Рыцарская». Чтобы назвать цену, подскажите, пожалуйста, на какую дату и на сколько часов планируете?
4. `calculate_price`
   - аргументы: `{"zone_id": "bath_russian", "date": "2026-09-02"}`
   - ответ: На сколько часов хотите баню?
5. `calculate_price`
   - аргументы: `{"zone_id": "bath_russian", "date": "2026-09-02"}`
   - ответ: Подскажите, на сколько часов хотите баню?

**Без принуждения (контроль):**

1. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
2. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
3. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
4. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
5. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given

### `callback_tomorrow`

Клиент: «завтра перезвоню»

требование заказчика дословно: дата есть, вопроса про запись нет — календарь дёргать не за что.

**С принуждением:**

1. `— ни одного —`
   - ответ: Конечно, сейчас передам менеджеру — он свяжется с вами.
2. `— ни одного —`
   - ответ: Здравствуйте! Хорошо, жду вашего звонка 🌸 А чтобы заранее подготовиться — что вас интересует: баня, купол, гриль-домик или шатёр?
3. `— ни одного —`
   - ответ: Конечно, сейчас передам менеджеру — он свяжется с вами.
4. `— ни одного —`
   - ответ: Здравствуйте! Хорошо, буду ждать вашего звонка 🌸  Если захотите уточнить что-то раньше — пишите. Подскажите, кстати, что вас интересует: баня, купол, гриль-домик или шатёр?
5. `— ни одного —`
   - ответ: Здравствуйте! Договорились, будем на связи 🌸  Подскажите, чтобы я сориентировалась: что вас интересует — баня, купол, гриль-домик или шатёр?

**Без принуждения (контроль):**

1. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
2. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
3. `— ни одного —`
   - ответ: Конечно, сейчас передам менеджеру — он свяжется с вами.
4. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given
5. `— ни одного —` · ОШИБКА: TypeError: probe.<locals>.<lambda>() takes 1 positional argument but 2 were given

