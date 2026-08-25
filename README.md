# KNUS Service Desk — приём заявок (Telegram + веб-портал)

Бот: [@knus_marketing_bot](https://t.me/knus_marketing_bot)

Мультиотдельный приём заявок для университета. Заявитель выбирает **отдел-исполнитель**
(маркетинг, ДИТ, …), бот создаёт задачу в плане Microsoft Planner **этого** отдела, кладёт файлы
в его библиотеку SharePoint и шлёт уведомление в **его** чат. Отделы настраиваются в
`departments.yaml` без правки кода.

Два канала приёма, общая логика и хранилище:
- **Telegram** — диалог с кнопками (`@knus_marketing_bot`), long polling;
- **Веб-портал** (KNUS Digital) — форма, которая шлёт заявку в `POST /api/tickets`.

Плюс: нормализация описания через LLM (Qwen3 на OVMS), гибридное хранение файлов (чат + SharePoint),
раздел «Мои заявки» со статусом из Planner, HTTP API со статистикой. Вебхук и открытые порты не нужны.

## Возможности

- Маршрутизация заявок по отделам-исполнителям (реестр `departments.yaml`).
- Разделение «кому» (отдел-исполнитель) и «от кого» (подразделение заявителя).
- Типы обращений свои у каждого отдела.
- Назначение исполнителей в Planner (`assignees` в реестре; требует права `User.Read.All`).
- «Мои заявки» со статусом из Planner в реальном времени.
- Гибридные вложения: мгновенно в чат по `file_id` + одна загрузка в SharePoint.
- HTTP API: статистика с разрезом по отделам + приём заявок с веб-портала.
- Состояние диалога переживает перезапуск (PicklePersistence на volume).
- Страховка: jsonl-лог каждой заявки + fallback при сбое Planner.

## Структура

```
main.py              точка входа, сборка Application, long polling + HTTP API
departments.yaml     РЕЕСТР отделов-исполнителей (маршрутизация; правится без пересборки)
bot/config.py        чтение .env
bot/departments.py   загрузка/валидация реестра (DepartmentRegistry)
bot/router.py        TicketRouter: по отделу → план/сегмент/чат/исполнители
bot/constants.py     справочники: подразделения-заявители, типы обращений, приоритеты, метки
bot/handlers.py      ConversationHandler — вся машина состояний Telegram-диалога
bot/ticket.py        модель заявки, номер, форматирование текстов
bot/llm.py           нормализация описания (OpenAI-совместимый эндпоинт)
bot/graph.py         Microsoft Graph (мультиплановый): Planner + SharePoint
bot/db.py            SQLite: связь пользователь→заявки, агрегаты и статусный кеш
bot/api.py           HTTP API (aiohttp): /health, /api/stats, GET+POST /api/tickets, рефрешер
bot/storage.py       jsonl-логи заявок и fallback
```

## Сценарий (Telegram)

`/start` → **главное меню**: «📝 Создать заявку» / «📋 Мои заявки».

Создание заявки: **отдел-исполнитель** → ваше подразделение → тип обращения (список зависит
от отдела) → описание → материалы → дедлайн (`ДД.ММ.ГГГГ` или «Без срока») → приоритет →
контакт → сводка → ✅ Отправить / ✏️ Исправить / ❌ Отмена. Если включён один отдел — шаг выбора
исполнителя пропускается.

На каждом шаге есть «⬅️ Назад» — возврат без потери введённого. Один шаг рисуется одной функцией
`show_*`, поэтому «вперёд» и «назад» показывают его одинаково. «Исправить» на сводке открывает
меню полей; в режиме правки «Назад» превращается в «⬅️ К сводке».

`/cancel` сбрасывает диалог, `/start` открывает меню, `/my` — статус своих заявок.

## Отделы-исполнители (`departments.yaml`)

Куда уходит заявка — определяет **выбранный отдел-исполнитель**, а не .env. Отделы описаны
в `departments.yaml` в корне проекта. В контейнере файл смонтирован как `/app/departments.yaml`
(volume в `docker-compose.yml`), поэтому правится на хосте **без пересборки образа**.

**Поля отдела:** `name`, `enabled`, `planner_plan_id` (обязателен у включённого),
`planner_bucket_id` (пусто → поиск по имени), `planner_bucket_name`, `notification_chat_id`,
`assignees` (email-ы; пусто = общая очередь), `request_types` (какие типы показывать),
`bucket_status` (необязательный маппинг сегмент доски → статус для заявителя).

**Что заполнить для нового отдела (например ДИТ):**
- `notification_chat_id` — **обязательно** (иначе уведомления не пойдут). Можно вписать сюда, либо
  оставить пустым и задать через .env — см. ниже.
- `assignees` — по желанию: email-ы саппорта, задача в Planner назначится на них. Пусто = общая
  очередь. Для назначения нужно право **User.Read.All** (Application) + admin consent; без него
  задача создастся без исполнителя, заявка не упадёт.

**chat_id из .env.** Пустой `notification_chat_id` любого отдела подхватывается из переменной
`<КОД_ОТДЕЛА>_CHAT_ID`: `marketing → MARKETING_CHAT_ID`, `it → IT_CHAT_ID`. Если заполнено и в yaml,
и в .env — приоритет у yaml.

**Как применить изменения:** отредактировать `departments.yaml` на сервере и выполнить
`docker compose restart marketing-bot` (файл читается при старте, пересборка не нужна).

**Совместимость.** У отдела `marketing` пустые `planner_plan_id`/`bucket`/`chat` подхватываются
из .env (`PLANNER_PLAN_ID`, `PLANNER_BUCKET_*`, `MARKETING_CHAT_ID`). Если файла нет/он битый/нет
включённых отделов — бот откатывается в режим одного отдела (маркетинг из .env), прод не ломается.

**Метки Planner** (цветные теги типов) применяются только к плану, где они настроены
(`categoryDescriptions` — сейчас маркетинг). У остальных отделов задачи создаются без тегов.

**Номер заявки** получает префикс по отделу: `MKT-…` для маркетинга, `IT-…` для ДИТ.

## Переменные .env

Скопируй `.env.example` в `.env` и заполни:

| Переменная | Что это | Значение |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Токен от @BotFather | обязательно |
| `MARKETING_CHAT_ID` | Чат уведомлений маркетинга (fallback для `marketing`) | обязательно, см. ниже |
| `IT_CHAT_ID` | Чат уведомлений ДИТ (fallback для `it`) | вписать chat_id ДИТ |
| `<КОД>_CHAT_ID` | Общее правило: чат отдела `<код>`, если пуст в yaml | по числу отделов |
| `MAX_ATTACHMENTS` | Лимит файлов на заявку | `10` |
| `GRAPH_TENANT_ID` / `GRAPH_CLIENT_ID` / `GRAPH_CLIENT_SECRET` | Приложение в Entra | обязательно |
| `PLANNER_PLAN_ID` / `PLANNER_BUCKET_ID` / `PLANNER_BUCKET_NAME` | План маркетинга (fallback) | обязательно |
| `PLANNER_ENABLED` | `false` — заявки только в jsonl, без Planner | `true` |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` / `LLM_TIMEOUT` | Qwen3 на OVMS | обязательно |
| `LLM_NORMALIZE` | Включить нормализацию описания | `true` |
| `DATA_DIR` | Каталог данных внутри контейнера | `/data` |
| `REQUESTS_DB_PATH` | SQLite «Мои заявки» + API — обязательно на volume | `/data/requests.db` |
| `DEPARTMENTS_CONFIG` | Путь к реестру отделов (задаётся в compose) | `/app/departments.yaml` |
| `API_PORT` | Порт HTTP API внутри сети (не публикуется наружу) | `8080` |
| `API_TOKEN` | Bearer-токен для API/веб-портала; пусто → API отдаёт 401 | сгенерировать |
| `STATUS_REFRESH_SECONDS` | Период фонового обновления статусов | `90` |
| `LOG_LEVEL` | Уровень логов | `INFO` |

`.env`, `.env.bak*`, `*.bak` в `.gitignore` — секреты в репозиторий не попадают.

**Права приложения в Entra (Application + admin consent):** `Tasks.ReadWrite.All` (задачи Planner),
`Group.ReadWrite.All` (группа плана), `Sites.ReadWrite.All` (файлы в SharePoint),
`User.Read.All` (назначение исполнителей — опционально).

## Как получить chat_id (для любого отдела)

Telegram принимает для лички только **числовой** id — `@username` не сработает.

1. Открой в Telegram [@knus_marketing_bot](https://t.me/knus_marketing_bot), нажми **Start**.
2. Отправь `/myid` — бот ответит `chat_id: 123456789`.
3. Подставь число в `MARKETING_CHAT_ID` / `IT_CHAT_ID` в `.env` (или в `notification_chat_id`
   отдела в `departments.yaml`) и перезапусти бота.

Для группы: добавь бота в группу и отправь `/myid` там — id группы отрицательный
(например `-1001234567890`). Бот должен быть участником группы, иначе отправка упадёт.
Личка: получатель должен сам первым нажать Start у бота, иначе Telegram не даст боту ему написать.

## «Мои заявки»

Кнопка «📋 Мои заявки» на `/start` и команда `/my`. Список — inline-кнопки
`№… · тип · краткая суть`, по 8 на страницу с навигацией «⬅️/➡️». По нажатию бот читает задачу
из Planner **в момент просмотра** (не кеширует). Кнопка «⬅️ К списку» возвращает обратно.

Статус определяется **сегментом** доски отдела (у каждого отдела своя доска и свой маппинг
`bucket_status` в реестре; по умолчанию общий). Отметка «Завершено» (`percentComplete=100`)
перекрывает сегмент. Незнакомый сегмент показывается как есть; если и его нет — запасной путь
по проценту (`0 → 🆕 Новая`, `1–99 → 🔧 В работе`, `100 → ✅ Готово`). Имена сегментов кешируются
на 5 минут, сам статус — никогда.

План для чтения статуса берётся по отделу-исполнителю заявки (у ДИТ-заявки — план ДИТ). Если
задачу удалили из плана или Graph ответил ошибкой — «❔ Статус недоступен». Заявка остаётся в списке.

Раздел живёт вне `ConversationHandler`, поэтому `/my` работает и посреди заполнения заявки.
Связь пользователь→заявка — в SQLite `/data/requests.db`, пишется при **успешном** создании задачи.
Заявку отдаём только её автору (`id` проверяется вместе с `telegram_user_id`). Заявки, поданные
до внедрения раздела, в списке не показываются (лежат в `requests.jsonl` и Planner).

## Материалы к заявке (гибридное хранение)

Один файл — два маршрута, скачивается **ровно один раз**:

1. **Чат отдела** — файл пересылается по `file_id` (Telegram) / отправляется из формы (веб).
   Мгновенно, для Telegram — без скачивания.
2. **SharePoint + Planner** — файл загружается в библиотеку группы плана отдела, ссылка
   прикрепляется к задаче как reference. Временный файл удаляется сразу после загрузки.

**Где файлы:** библиотека группы плана отдела → `Заявки маркетинга/<номер заявки>/<имя файла>`
(имя корневой папки — общая константа). Одноимённые файлы не затирают друг друга
(`conflictBehavior=rename`). Файлы >4 МБ грузятся через upload session кусками по 3.2 МБ.

**Большой файл (>20 МБ, только Telegram).** Bot API не отдаёт такие через `getFile`. Заявка не
падает: файл уходит в чат отдела по `file_id`, а в Planner помечается «слишком большой для
загрузки — только в чате отдела».

**Сбой SharePoint.** Задача создаётся, файлы в чате уже есть, в описании — «материалы в чате
отдела (SharePoint недоступен)». Ошибка Graph пишется в лог (401/403 → подсказка про
`Sites.ReadWrite.All` + consent).

## HTTP API

Рядом с ботом в том же процессе и asyncio-loop поднимается HTTP-сервер (aiohttp) на
`0.0.0.0:API_PORT` — для панели в галерее `ai.knus.edu.kz` и приёма заявок с веб-портала. Наружу
порт **не публикуется** (`expose`, не `ports`): доступен только контейнерам сети деплоя как
`marketing-bot:8080`. Бот и API работают одновременно, polling не прерывается.

Все эндпоинты, кроме `/health`, требуют `Authorization: Bearer <API_TOKEN>`. Пустой `API_TOKEN`
→ всё, кроме `/health`, отвечает `401`.

### Эндпоинты

- `GET /health` → `{"status":"ok"}` — без токена, для healthcheck.
- `GET /api/stats` → сводка: `total`, `today`, `by_status {new,in_progress,done}`,
  `by_target` (по отделам-исполнителям), `by_department` (по подразделениям-заявителям),
  `by_type`, `by_priority {urgent,normal,low}`, `by_day` (30 дней, пропуски заполнены нулями),
  `avg_per_day`. Разрез: `?department_target=it`.
- `GET /api/tickets` → список с фильтрами и пагинацией. Параметры (необязательны):
  `target_department` (=`department_target`), `department`, `type`, `status`
  (new/in_progress/done), `priority`, `date_from`, `date_to` (ГГГГ-ММ-ДД), `q`, `page`,
  `per_page` (до 100). Ответ: `{total, page, per_page, items:[{id, created_at, target_department,
  department, request_type, task_type, summary, deadline, priority, status, contact,
  planner_task_id, planner_url, attachments_count, source, submitter_name, submitter_email}]}`.
- `POST /api/tickets` → **приём заявки с веб-портала** (KNUS Digital). `multipart/form-data`.
  Авторизация: `Bearer <API_TOKEN>` + заголовки `X-KNUS-User-Email` (обязательно, домен
  `@knus.edu.kz`, иначе `403`) и `X-KNUS-User-Name` (URL-кодированный). Поля формы:
  `target_department` (код отдела, по умолчанию `marketing`), `department` (+`department_other`),
  `task_type` (+`task_type_other`; должен входить в типы отдела), `description` (10–5000),
  `deadline` (`ГГГГ-ММ-ДД`/`ДД.ММ.ГГГГ`/пусто), `priority` (urgent/normal/low), `contact`, файлы
  (до `MAX_ATTACHMENTS`, ≤20 МБ каждый). Создаёт задачу в плане отдела, грузит файлы в SharePoint,
  шлёт уведомление в чат отдела, пишет в SQLite (`source='web'`). Ответ
  `201 {ok, id, planner_url, attachments, warnings}` или `4xx/5xx` с `{error, message}`.

### Как статус попадает в API

Статус в API — три состояния по `percentComplete`: `0 → new`, `1–99 → in_progress`, `100 → done`
(отдельно от бота, где статус детальнее — по сегменту). API **не** ходит в Planner на каждый
запрос: фоновая задача раз в `STATUS_REFRESH_SECONDS` (по умолчанию 90) группирует активные
заявки по планам отделов и обходит **каждый план одним** запросом, пишет `status` +
`status_updated_at` в SQLite. Заявки `done` из обхода выпадают. План недоступен — рефрешер
пропускает его до следующего цикла, API отдаёт последний известный статус.

Таблица `requests` при старте мягко доливается новыми колонками (через `PRAGMA table_info`,
идемпотентно — старые строки целы). Старым заявкам проставляется `target_department='marketing'`.

### Проверка из другого контейнера сети

```bash
TOKEN=<значение API_TOKEN из .env>
curl -s http://marketing-bot:8080/health
curl -s -H "Authorization: Bearer $TOKEN" http://marketing-bot:8080/api/stats | jq
curl -s -H "Authorization: Bearer $TOKEN" \
  "http://marketing-bot:8080/api/tickets?department_target=it&status=in_progress" | jq
```

С хоста напрямую порт недоступен (не опубликован) — проверять из контейнера в той же сети.

## Состояние диалога переживает перезапуск

Шаг диалога и черновик заявки сохраняются на volume в `/data/ptb_state.pkl` (`PicklePersistence`).
Без этого при каждом передеплое состояние в памяти обнулялось, и у тех, кто не дозаполнил заявку,
кнопки шагов переставали отвечать. `bot_data` намеренно не сохраняется (там httpx-клиенты и
asyncio-локи — не пиклятся). Любой устаревший callback теперь отвечает всплывающим окном
«Эта кнопка устарела, нажмите /start», а не молчит.

Если после аварийного завершения бот не стартует из-за битого `ptb_state.pkl` — удалите файл
(потеряются только незавершённые черновики; поданные заявки в SQLite/Planner не тронуты).

## Логи и данные

`./data` смонтирован в контейнер как `/data`:

- `requests.jsonl` — **каждая** отправленная заявка (`status`: `planner_ok`/`planner_failed`/`local_only`);
- `planner_fallback.jsonl` — заявки, которые не удалось создать в Planner, с текстом ошибки;
- `requests.db` — SQLite (связь пользователь→заявки, агрегаты, статусный кеш);
- `ptb_state.pkl` — состояние диалога.

## Про выбор bucket'а (сегмента)

Graph отдаёт сегменты **не** в порядке интерфейса Planner, поэтому «первый» — лотерея. Сегмент
выбирается так: `planner_bucket_id` из реестра, если задан → поиск по `planner_bucket_name` →
первый сегмент плана (последний резерв, с `WARNING` в логе).

## Деплой контейнера

Единый образ, long polling, внешняя сеть `deploy_default` (задана в `docker-compose.yml`;
если у платформы сеть называется иначе — поправь `networks.platform.name`). Порты наружу не
публикуются — контейнеру нужен исходящий доступ к `api.telegram.org`, `login.microsoftonline.com`,
`graph.microsoft.com` и к LLM-серверу во внутренней сети.

```bash
# 1. Сеть существует?
docker network ls | grep deploy_default

# 2. Заполнить .env (MARKETING_CHAT_ID, IT_CHAT_ID, Graph creds, API_TOKEN и т.д.)

# 3. Сборка и запуск
docker compose up -d --build

# 4. Логи
docker compose logs --tail 30 marketing-bot
```

При успешном старте: `Реестр отделов загружен: marketing(on), it(on)`,
`Отделы-исполнители: … (chat=да)`, `Бот запущен: @knus_marketing_bot`, `HTTP API слушает`.
У каждого отдела должно быть `chat=да` — иначе уведомления не пойдут.

Обновление: `git pull && docker compose up -d --build`. Правки только в `departments.yaml`
применяются без пересборки: `docker compose restart marketing-bot`.

## Локальная проверка (без Planner)

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Linux: .venv/bin/pip

# в .env: PLANNER_ENABLED=false, LLM_NORMALIZE=false, DATA_DIR=./data,
#         REQUESTS_DB_PATH=./data/requests.db, DEPARTMENTS_CONFIG=./departments.yaml

.venv/Scripts/python main.py                    # Linux: .venv/bin/python main.py
```

С `PLANNER_ENABLED=false` заявка нигде не создаётся, только пишется в `./data/requests.jsonl`
(`local_only`); уведомление всё равно уходит в чат. Недоступный LLM просто пропускает нормализацию,
недоступный Planner — пишет заявку в `planner_fallback.jsonl`.
