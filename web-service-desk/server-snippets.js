/* ============================================================================
 * KNUS Service Desk — маршруты для coursehub/app (server.js).
 *
 * ЭТО ЗАГОТОВКА ДЛЯ РЕПО coursehub/app, НЕ для marketing-bot.
 * Вставь фрагменты в server.js рядом с существующим прокси knus-bot (~1944-1993)
 * и AGENT_CATALOG. Точки интеграции, которые надо сверить с твоим кодом, помечены  // ⚠️
 *
 * Требуется вверху server.js (обычно уже есть):
 *   const http = require("http");
 *   const { URL } = require("url");
 * ENV (.env веб-приложения):
 *   MARKETING_BOT_API_URL   (дефолт http://marketing-bot:8080)
 *   MARKETING_BOT_API_TOKEN  (= API_TOKEN бота)
 * ==========================================================================*/

const MARKETING_BOT_API_URL =
  process.env.MARKETING_BOT_API_URL || "http://marketing-bot:8080";
const MARKETING_BOT_API_TOKEN = process.env.MARKETING_BOT_API_TOKEN || "";

// Доступ к панели ДИТ: админ ИЛИ выданный агент ITTickets.
// ⚠️ Сверь имена полей сессии со своим requireSession (isAdmin / allowedAgents).
function canSeeItTickets(session) {
  if (!session) return false;
  if (session.isAdmin) return true;
  const allowed = session.allowedAgents || session.agents || [];
  return Array.isArray(allowed) && allowed.includes("ITTickets");
}

// --- GET-прокси к боту: собирает JSON и отдаёт как есть -----------------------
function botApiGet(pathWithQuery, res) {
  const target = new URL(pathWithQuery, MARKETING_BOT_API_URL);
  const client = target.protocol === "https:" ? require("https") : http;
  const upstream = client.request(
    target,
    {
      method: "GET",
      headers: { Authorization: "Bearer " + MARKETING_BOT_API_TOKEN },
    },
    (up) => {
      let body = "";
      up.on("data", (c) => (body += c));
      up.on("end", () => {
        res.status(up.statusCode || 502);
        res.type("application/json").send(body || "{}");
      });
    }
  );
  upstream.on("error", (err) => {
    console.error("bot api get error:", err.message);
    res.status(502).json({ error: "upstream_unavailable" });
  });
  upstream.end();
}

/* ===========================================================================
 * СТРАНИЦА 1. Форма подачи заявки — доступна ЛЮБОМУ авторизованному.
 * ==========================================================================*/

// Отдаём HTML формы
app.get("/agents/service-desk", requireSession, (req, res) => {
  res.sendFile(path.join(__dirname, "views", "service-desk.html")); // ⚠️ путь к views
});

// Справочник отделов+типов для наполнения формы
app.get("/api/service-desk/registry", requireSession, (req, res) => {
  botApiGet("/api/registry", res);
});

// Приём заявки: ПОТОКОВЫЙ проксинг multipart/form-data (файлы!) на бота.
// Тело req передаём как есть (сохраняя boundary в content-type), добавляем заголовки
// авторизации и личность заявителя из сессии.
app.post("/api/service-desk/submit", requireSession, (req, res) => {
  const session = req.session; // ⚠️ имя объекта сессии
  const email = session && session.email; // ⚠️
  const name = (session && session.name) || ""; // ⚠️
  if (!email) return res.status(401).json({ error: "no_session" });

  const target = new URL("/api/tickets", MARKETING_BOT_API_URL);
  const client = target.protocol === "https:" ? require("https") : http;

  const headers = {
    Authorization: "Bearer " + MARKETING_BOT_API_TOKEN,
    "X-KNUS-User-Email": email,
    "X-KNUS-User-Name": encodeURIComponent(name),
  };
  // сохраняем content-type с boundary и длину — без них multipart не распарсится
  if (req.headers["content-type"]) headers["content-type"] = req.headers["content-type"];
  if (req.headers["content-length"]) headers["content-length"] = req.headers["content-length"];

  const upstream = client.request(target, { method: "POST", headers }, (up) => {
    let body = "";
    up.on("data", (c) => (body += c));
    up.on("end", () => {
      res.status(up.statusCode || 502);
      res.type("application/json").send(body || "{}");
    });
  });
  upstream.on("error", (err) => {
    console.error("service-desk submit proxy error:", err.message);
    res.status(502).json({ error: "upstream_unavailable" });
  });
  // ⚠️ ВАЖНО: НЕ должно быть express.json()/bodyParser, поглощающего это тело до маршрута.
  // Если глобальный парсер стоит раньше — исключи этот путь (см. README).
  req.pipe(upstream);
});

/* ===========================================================================
 * СТРАНИЦА 2. Панель статистики ДИТ — админ или роль ITTickets.
 * department_target=it зашит жёстко: панель ДИТ не может запросить чужой отдел.
 * ==========================================================================*/

app.get("/agents/it-tickets", requireSession, (req, res) => {
  if (!canSeeItTickets(req.session)) return res.status(403).send("Доступ запрещён");
  res.sendFile(path.join(__dirname, "views", "it-tickets.html")); // ⚠️ путь к views
});

app.post("/api/admin/it-tickets/stats", requireSession, (req, res) => {
  if (!canSeeItTickets(req.session)) return res.status(403).json({ error: "forbidden" });
  botApiGet("/api/stats?department_target=it", res);
});

app.post("/api/admin/it-tickets/list", requireSession, (req, res) => {
  if (!canSeeItTickets(req.session)) return res.status(403).json({ error: "forbidden" });
  const b = req.body || {};
  const q = new URLSearchParams({ department_target: "it" });
  for (const k of ["department", "type", "status", "priority", "date_from", "date_to", "q", "page", "per_page"]) {
    if (b[k] !== undefined && b[k] !== null && String(b[k]).length) q.set(k, String(b[k]));
  }
  botApiGet("/api/tickets?" + q.toString(), res);
});

/* ===========================================================================
 * Записи каталога — добавь в массив AGENT_CATALOG (сверь форму объекта!). // ⚠️
 * ==========================================================================*/

// {
//   code: "ServiceDeskForm",
//   title: "Подать заявку",
//   href: "/agents/service-desk",
//   audience: "Для сотрудников",
//   summary: "Заявка в отдел-исполнитель (маркетинг, ДИТ): категория, описание, файлы, срок.",
//   description: "Единое окно заявок KNUS Service Desk — выбор отдела, тип обращения, вложения, срок.",
//   yearlyLimit: null,
//   access: { mode: "public" }, // видят ВСЕ авторизованные; если нет режима public — см. README
// },
// {
//   code: "ITTickets",
//   title: "Заявки ДИТ",
//   href: "/agents/it-tickets",
//   audience: "Для ДИТ",
//   summary: "Статистика и список заявок в ДИТ: типы, статусы, приоритеты.",
//   description: "Дашборд заявок ДИТ — метрики, графики, список со статусами из Planner.",
//   yearlyLimit: null,
//   access: { mode: "matchAny", rules: [{ attribute: "extensionAttribute10", flag: "it" }] },
// },
