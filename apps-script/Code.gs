/**
 * Resumen diario personal.
 * Se ejecuta en Google Apps Script; no requiere servidor ni contraseña de aplicación.
 */
const TIME_ZONE = "Europe/Berlin";
const MAX_STORIES = 5;
const MAX_AGE_DAYS = 7;

const TOPICS = [
  ["IA y tecnología", "inteligencia artificial tecnología modelos IA", 8],
  ["Prácticas y empleo tecnológico", "prácticas empleo software engineering España Europa 2027", 8],
  ["Ingeniería del software", "ingeniería del software desarrollo web programación TypeScript Python", 7],
  ["Mercados e inversión", "fondos indexados ETF inversión mercados España", 7],
  ["TFG: IA, EEG y epilepsia", "inteligencia artificial EEG epilepsia electroencefalograma", 7],
  ["España y Alemania", "noticias importantes España Alemania política economía sociedad", 7],
  ["Ajedrez", "ajedrez torneos Gipuzkoa España", 3],
  ["Ruso", "idioma ruso aprendizaje ruso enseñanza", 2]
];

function setupDailyDigest() {
  ScriptApp.getProjectTriggers()
    .filter(function (trigger) {
      return trigger.getHandlerFunction() === "sendDailyDigest";
    })
    .forEach(function (trigger) {
      ScriptApp.deleteTrigger(trigger);
    });

  ScriptApp.newTrigger("sendDailyDigest")
    .timeBased()
    .atHour(8)
    .everyDays(1)
    .inTimezone(TIME_ZONE)
    .create();

  // Envía un primer correo de prueba tras autorizar los permisos.
  sendDailyDigest();
}

function sendDailyDigest() {
  const recipient = Session.getEffectiveUser().getEmail();
  if (!recipient) {
    throw new Error("Google no ha devuelto la dirección del propietario del script.");
  }

  const stories = collectStories_();
  const date = Utilities.formatDate(new Date(), TIME_ZONE, "dd/MM/yyyy");
  const subject = "Tu resumen diario — " + date;

  const plainText = stories.map(function (story, index) {
    return (index + 1) + ". " + story.title + "\n" +
      story.topic + " · " + story.source + "\n" +
      (story.summary || "Consulta la noticia completa en la fuente original.") +
      "\n" + story.link;
  }).join("\n\n");

  const htmlCards = stories.map(function (story, index) {
    const published = Utilities.formatDate(story.published, TIME_ZONE, "dd/MM/yyyy HH:mm");
    return '<article style="padding:18px 0;border-bottom:1px solid #e5e7eb">' +
      '<div style="font-size:12px;color:#64748b;margin-bottom:7px">' +
      escapeHtml_(story.topic) + ' · ' + escapeHtml_(story.source) + ' · ' +
      published + ' (hora alemana)</div>' +
      '<h2 style="font-size:18px;line-height:1.35;margin:0 0 8px">' +
      '<a href="' + escapeHtml_(story.link) + '" style="color:#1d4ed8;text-decoration:none">' +
      (index + 1) + '. ' + escapeHtml_(story.title) + '</a></h2>' +
      '<p style="font-size:14px;line-height:1.6;color:#334155;margin:0">' +
      escapeHtml_(story.summary || "Consulta la noticia completa en la fuente original.") +
      '</p><p style="margin:10px 0 0;font-size:13px">' +
      '<a href="' + escapeHtml_(story.link) + '">Leer noticia completa</a></p></article>';
  }).join("");

  const htmlBody =
    '<div style="margin:0;background:#f8fafc;font-family:Arial,Helvetica,sans-serif;color:#0f172a;padding:20px">' +
    '<main style="max-width:680px;margin:0 auto;padding:28px;background:#fff;border:1px solid #e2e8f0;border-radius:12px">' +
    '<p style="font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#64748b;margin:0 0 8px">Briefing personal</p>' +
    '<h1 style="font-size:26px;margin:0 0 6px">Tu resumen diario</h1>' +
    '<p style="font-size:14px;color:#64748b;margin:0 0 18px">' + date +
    ' · Hasta ' + MAX_STORIES + ' noticias</p>' + htmlCards +
    '<p style="font-size:12px;line-height:1.5;color:#64748b;margin:22px 0 0">' +
    'Selección automática a partir de titulares y extractos RSS. No se generan análisis no respaldados por las fuentes.</p>' +
    '</main></div>';

  MailApp.sendEmail({
    to: recipient,
    subject: subject,
    body: "Tu resumen diario\n\n" + plainText +
      "\n\nSelección automática a partir de titulares y extractos RSS.",
    htmlBody: htmlBody,
    name: "Resumen diario"
  });

  console.log("Resumen enviado correctamente.");
}

function collectStories_() {
  const requests = TOPICS.map(function (topic) {
    return {
      url: "https://news.google.com/rss/search?q=" +
        encodeURIComponent(topic[1] + " when:7d") +
        "&hl=es&gl=ES&ceid=ES:es",
      muteHttpExceptions: true,
      followRedirects: true,
      headers: { "User-Agent": "Mozilla/5.0 resumen-diario" }
    };
  });

  const responses = UrlFetchApp.fetchAll(requests);
  const allStories = [];

  responses.forEach(function (response, index) {
    if (response.getResponseCode() !== 200) {
      console.warn("Feed no disponible: " + TOPICS[index][0]);
      return;
    }

    try {
      const root = XmlService.parse(response.getContentText()).getRootElement();
      const channel = root.getChild("channel");
      if (!channel) return;

      channel.getChildren("item").forEach(function (item) {
        const title = item.getChildText("title") || "";
        const link = item.getChildText("link") || "";
        const dateText = item.getChildText("pubDate") || "";
        const published = new Date(dateText);

        if (!title || !link || isNaN(published.getTime())) return;

        const age = Date.now() - published.getTime();
        if (age > MAX_AGE_DAYS * 24 * 60 * 60 * 1000 ||
            age < -2 * 60 * 60 * 1000) return;

        const sourceNode = item.getChild("source");
        const source = sourceNode ? sourceNode.getText() : "Google News";
        let summary = stripHtml_(item.getChildText("description") || "");

        if (summary.toLowerCase().indexOf(title.toLowerCase()) === 0) {
          summary = summary.substring(title.length).replace(/^[\s\-–:|]+/, "");
        }

        allStories.push({
          topic: TOPICS[index][0],
          priority: TOPICS[index][2],
          title: title.trim(),
          link: link.trim(),
          source: source.trim() || "Google News",
          summary: summary.substring(0, 420),
          published: published
        });
      });
    } catch (error) {
      console.warn("No se pudo analizar " + TOPICS[index][0] + ": " + error);
    }
  });

  const unique = {};
  allStories.forEach(function (story) {
    const key = story.title.toLowerCase().replace(/[^a-z0-9áéíóúüñ]/g, "");
    if (!unique[key] || story.priority > unique[key].priority) unique[key] = story;
  });

  const now = Date.now();
  const ranked = Object.keys(unique).map(function (key) {
    return unique[key];
  }).sort(function (a, b) {
    const scoreA = a.priority + Math.max(0, 72 - (now - a.published.getTime()) / 3600000) / 24;
    const scoreB = b.priority + Math.max(0, 72 - (now - b.published.getTime()) / 3600000) / 24;
    return scoreB - scoreA;
  });

  const counts = {};
  const selected = [];
  ranked.forEach(function (story) {
    if (selected.length >= MAX_STORIES) return;
    counts[story.topic] = counts[story.topic] || 0;
    if (counts[story.topic] >= 2) return;
    counts[story.topic]++;
    selected.push(story);
  });

  if (!selected.length) throw new Error("No se han encontrado noticias recientes en los feeds RSS.");
  return selected;
}

function stripHtml_(value) {
  return value
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/\s+/g, " ")
    .trim();
}

function escapeHtml_(value) {
  return String(value).replace(/[&<>"']/g, function (char) {
    return {
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[char];
  });
}
