# Веб-страницы KNUS Service Desk — для coursehub/app

Эти файлы **не часть бота** — их нужно перенести в репозиторий веб-приложения `coursehub/app`
(Node/Express, `server.js` + `views/`). Бот (`marketing-bot`) уже отдаёт нужный API в сети
`deploy_default` как `http://marketing-bot:8080`.

## Что куда

| Файл | Куда в coursehub/app |
|---|---|
| `service-desk.html` | `views/service-desk.html` |
| `it-tickets.html` | `views/it-tickets.html` |
| `server-snippets.js` | фрагменты вставить в `server.js` (рядом с прокси knus-bot) |

## ENV веб-приложения (.env)

```
MARKETING_BOT_API_URL=http://marketing-bot:8080
MARKETING_BOT_API_TOKEN=<тот же API_TOKEN, что у бота>
```

## Точки интеграции — сверить с твоим server.js (в snippets помечены // ⚠️)

1. **Объект сессии.** Код читает `req.session.email`, `req.session.name`, `req.session.isAdmin`,
   `req.session.allowedAgents`. Если у тебя другие имена (напр. `req.user`, `session.user.mail`) —
   поправь в `server-snippets.js` (функция `canSeeItTickets` и обработчик `submit`).
2. **`requireSession`** — используется как middleware; имя должно совпадать с твоим.
3. **`path` и `views`** — `res.sendFile(path.join(__dirname,"views",...))`. Проверь, что `path`
   импортирован и вьюхи лежат в `views/`.
4. **Глобальный body-parser.** Маршрут `POST /api/service-desk/submit` **потоково** проксирует
   `multipart/form-data` (файлы). Если в server.js стоит глобальный `express.json()` /
   `bodyParser` ДО маршрутов — он не тронет multipart (парсит только json/urlencoded), но убедись,
   что нет глобального `multer`/`busboy`, поглощающего тело. При проблемах — навесь маршрут ДО
   любых body-парсеров.
5. **AGENT_CATALOG.** Добавь две записи (в конце `server-snippets.js`, закомментированы). Свери
   форму объекта с существующими карточками.
   - `ServiceDeskForm` — `access:{mode:"public"}` (видят ВСЕ авторизованные). Если у тебя нет
     режима `public` — маршрут `/agents/service-desk` и так под `requireSession` без ролевой
     проверки, так что достаточно, чтобы карточка показывалась всем (без `allowedAgents`-флага).
   - `ITTickets` — `access:{mode:"matchAny", rules:[{attribute:"extensionAttribute10", flag:"it"}]}`.
6. **Ссылки в шапке.** В HTML стоят `/app` (галерея) и `/logout` — поправь под свои реальные пути.
7. **Стиль.** Страницы самодостаточны (свой CSS, тёмная тема). Если хочешь строго стиль галереи —
   перенеси классы/переменные из `views/knus-bot-sessions.html` или `app.html`.

## Как проверить

1. Пересобрать веб: `docker compose up -d --build web`.
2. **Форма (любой авторизованный):** зайти под обычным пользователем → карточка «Подать заявку» →
   `/agents/service-desk`. Выбрать отдел (подтянутся из `/api/registry`), тип, заполнить, приложить
   файл, отправить. Ждём `✅ Заявка … принята` + ссылку на Planner. Уведомление придёт в чат отдела,
   задача — в его план.
3. **Панель ДИТ (роль it или админ):** карточка «Заявки ДИТ» → `/agents/it-tickets`. Плашки,
   графики и список должны наполниться данными ДИТ. Под пользователем без роли it и без админки —
   `/agents/it-tickets` отдаёт 403, а карточка не показывается.

## Проверка API бота напрямую (из контейнера сети)

```bash
TOKEN=<API_TOKEN бота>
curl -s -H "Authorization: Bearer $TOKEN" http://marketing-bot:8080/api/registry | jq
curl -s -H "Authorization: Bearer $TOKEN" "http://marketing-bot:8080/api/stats?department_target=it" | jq
```

## Зависимость от бота

Форма опирается на `GET /api/registry` — он **уже добавлен** в `marketing-bot` (`bot/api.py`).
Убедись, что на сервере развёрнута свежая версия бота (`git pull && docker compose up -d --build`
в репозитории бота).
