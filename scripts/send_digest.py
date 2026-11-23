#!/usr/bin/env python3
"""Build and email a Spanish daily news briefing from RSS + Gemini."""

from __future__ import annotations

import email.utils
import html
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
from urllib.error import HTTPError
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
MAX_AGE = timedelta(hours=24)
USER_AGENT = "resumen-diario/2.0 (+https://github.com/Ivangb-626/resumen-diario)"
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    + GEMINI_MODEL
    + ":generateContent"
)

SECTIONS = [
    ("Economía y geopolítica", [
        "global economy markets central banks interest rates inflation recession",
        "geopolitics war sanctions tariffs trade energy oil gas OPEC",
        "companies earnings layoffs bankruptcy mergers acquisitions IPO",
        "stock market bonds currencies gold silver commodities China United States Europe",
        "economía mundial mercados bancos centrales tipos interés inflación geopolítica",
        "guerras sanciones aranceles petróleo gas materias primas empresas resultados",
    ]),
    ("España y Gipuzkoa", [
        "España política economía gobierno Congreso empresas sociedad",
        "Gipuzkoa Donostia Gipuzkoa noticias economía sociedad infraestructuras",
        "País Vasco Euskadi política economía industria sociedad",
    ]),
    ("Cultura, sociedad y descubrimientos", [
        "historia arqueología patrimonio descubrimiento cultura libros educación",
        "medioambiente sociedad descubrimientos curiosidades investigación social",
    ]),
    ("Ciencia, investigación y tecnología", [
        "neuroscience brain epilepsy EEG electroencephalography research",
        "medical science research biology genetics physics astronomy space discovery",
        "scientific research artificial intelligence medicine breakthrough",
        "artificial intelligence software cybersecurity technology companies innovation",
        "neurociencia cerebro epilepsia EEG investigación científica",
    ]),
]


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if value:
            self.parts.append(value)


class ImageMetaParser(HTMLParser):
    """Extract the first Open Graph / Twitter image from an article page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.image_url = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.image_url:
            return
        values = {key.lower(): (value or "").strip() for key, value in attrs}
        if tag.lower() == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            if key in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}:
                self.image_url = values.get("content", "")


def usable_image_url(value: str, base_url: str = "") -> str:
    value = (value or "").strip()
    if not value:
        return ""
    absolute = urllib.parse.urljoin(base_url, value) if base_url else value
    parsed = urllib.parse.urlparse(absolute)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    if parsed.username or parsed.password:
        return ""
    return absolute


def rss_image(item: ET.Element, raw_description: str) -> str:
    # Prefer explicit media/enclosure metadata supplied by the RSS feed.
    for node in item.iter():
        tag = node.tag.lower()
        if tag.endswith("content") or tag.endswith("thumbnail") or tag == "enclosure":
            candidate = usable_image_url(node.attrib.get("url") or node.attrib.get("href") or "")
            media_type = node.attrib.get("type", "").lower()
            if candidate and (tag.endswith("thumbnail") or tag.endswith("content")
                              or media_type.startswith("image/")):
                return candidate
    # Some feeds put the thumbnail directly inside the description HTML.
    match = re.search(r"<img\b[^>]*?src=[\"']([^\"']+)", raw_description or "", re.I)
    return usable_image_url(html.unescape(match.group(1))) if match else ""


def clean_html(value: str) -> str:
    parser = TextExtractor()
    try:
        parser.feed(value or "")
        text = " ".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass
class Story:
    id: str
    section: str
    query: str
    title: str
    link: str
    source: str
    excerpt: str
    published: datetime
    image_url: str = ""


def fetch_query(section: str, query: str) -> list[Story]:
    params = urllib.parse.urlencode({
        "q": f"{query} when:1d",
        "hl": "es",
        "gl": "ES",
        "ceid": "ES:es",
    })
    url = "https://news.google.com/rss/search?" + params
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=25) as response:
        root = ET.fromstring(response.read())

    channel = root.find("channel")
    if channel is None:
        return []

    found: list[Story] = []
    now = datetime.now(timezone.utc)
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        published = parse_date(item.findtext("pubDate") or "")
        if not title or not link or published is None:
            continue
        age = now - published
        if age > MAX_AGE or age < -timedelta(minutes=90):
            continue
        source_node = item.find("source")
        source = (source_node.text or "").strip() if source_node is not None else "Fuente RSS"
        raw_description = item.findtext("description") or ""
        excerpt = clean_html(raw_description)
        image_url = rss_image(item, raw_description)
        if excerpt.lower().startswith(title.lower()):
            excerpt = excerpt[len(title):].lstrip(" -–:|")
        found.append(Story(
            id="",
            section=section,
            query=query,
            title=title,
            link=link,
            source=source or "Fuente RSS",
            excerpt=excerpt[:700],
            published=published,
            image_url=image_url,
        ))
    return found


def collect_stories() -> list[Story]:
    jobs = [(section, query) for section, queries in SECTIONS for query in queries]
    candidates: list[Story] = []
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_query, section, query): (section, query)
                   for section, query in jobs}
        for future in as_completed(futures):
            section, query = futures[future]
            try:
                candidates.extend(future.result())
            except Exception as exc:
                failures.append(f"{section}/{query}: {type(exc).__name__}")

    # Deduplicate by normalized title and URL; preserve the freshest copy.
    unique: dict[str, Story] = {}
    for story in candidates:
        key = re.sub(r"[^a-z0-9áéíóúüñ]+", "", story.title.lower())
        if not key:
            continue
        current = unique.get(key)
        if current is None or story.published > current.published:
            unique[key] = story

    now = datetime.now(timezone.utc)
    stories = sorted(unique.values(), key=lambda s: s.published, reverse=True)
    # Bound the model input while keeping a broad cross-section of fresh results.
    stories = stories[:70]
    for index, story in enumerate(stories, 1):
        story.id = f"N{index:03d}"
    if not stories:
        detail = "; ".join(failures[:4]) or "los feeds no devolvieron novedades"
        raise RuntimeError(f"No hay noticias de las últimas 24 horas. {detail}")
    print(f"RSS: {len(stories)} noticias candidatas de las últimas 24 horas.")
    if failures:
        print(f"Advertencia: {len(failures)} consultas RSS no respondieron.")
    return stories


def gemini_request(prompt: str, api_key: str) -> str:
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
            "maxOutputTokens": 12000,
        },
    }
    request = urllib.request.Request(
        GEMINI_ENDPOINT + "?key=" + urllib.parse.quote(api_key),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        result = json.loads(response.read().decode("utf-8"))
    return result["candidates"][0]["content"]["parts"][0]["text"]


def editorial_selection(stories: list[Story], api_key: str) -> dict[str, Any]:
    candidates = [{
        "id": s.id, "section_hint": s.section, "title": s.title,
        "source": s.source, "published": s.published.isoformat(),
        "excerpt": s.excerpt, "url": s.link, "image_url": s.image_url,
    } for s in stories]
    prompt = f"""
Eres el editor de un briefing diario de actualidad en español. La fecha actual es
{datetime.now(BERLIN).strftime('%Y-%m-%d %H:%M %Z')}. Solo puedes seleccionar noticias
publicadas dentro de las últimas 24 horas, usando los candidatos proporcionados.

OBJETIVO Y CRITERIO: relevancia general e importancia real. No hay un número máximo
fijo. Incluye todas las noticias que merezcan realmente la pena, pero sé exigente:
no rellenes, no repitas la misma historia y no incluyas noticias triviales. La
distribución entre secciones es flexible; una sección puede dominar si la actualidad
lo justifica.

SECCIONES Y ORDEN:
1. Economía y geopolítica (prioridad principal): economía y mercados mundiales,
bancos centrales, inflación, tipos, empresas, resultados, despidos, quiebras, fusiones,
IPO, conflictos, sanciones, aranceles, energía, materias primas, bonos, divisas y
acontecimientos políticos con impacto internacional.
2. España y Gipuzkoa: España, País Vasco, Gipuzkoa y Donostia.
3. Cultura, sociedad y descubrimientos: historia, arqueología, patrimonio, libros,
educación, medioambiente, sociedad y hallazgos interesantes.
4. Ciencia, investigación y tecnología: prioriza ciencia e investigación sobre
tecnología; especial interés temático en neurociencia, cerebro, epilepsia, EEG,
medicina, biología, genética, física, espacio e IA aplicada a ciencia.

Para cada noticia elegida devuelve:
- id: ID exacto de un candidato.
- section: una de las cuatro secciones anteriores.
- headline: titular fiel al candidato, sin exagerar.
- summary: resumen factual en español de 3-5 frases breves.
- why_it_matters: por qué importa y consecuencias plausibles, distinguiendo hechos
  de interpretación.
- source_name: nombre de la fuente del candidato.
- url: URL exacta del candidato, sin modificarla.
- published: fecha ISO del candidato.

REGLAS ESTRICTAS:
- No inventes hechos, cifras, declaraciones, contexto, enlaces ni causalidades.
- Usa solo la información de cada candidato. Si el extracto no basta para redactar
  un resumen fiable, descarta la noticia.
- No afirmes que has leído el artículo completo si solo tienes el titular y extracto.
- No conviertas una noticia antigua en novedad. Descarta candidatos fuera de ventana.
- No incluyas empleo, prácticas ni ofertas laborales.
- No incluyas el seguimiento de mercados aquí; se genera por separado.
- Devuelve JSON válido con esta forma exacta:
{{"articles":[{{"id":"N001","section":"...","headline":"...","summary":"...","why_it_matters":"...","source_name":"...","url":"...","published":"..."}}]}}
Si no hay noticias adecuadas, devuelve {{"articles":[]}}.

CANDIDATOS:
{json.dumps(candidates, ensure_ascii=False)}
"""
    raw = gemini_request(prompt, api_key)
    data = json.loads(raw)
    valid = {s.id: s for s in stories}
    articles = []
    seen: set[str] = set()
    for item in data.get("articles", []):
        sid = str(item.get("id", ""))
        if sid not in valid or sid in seen:
            continue
        source = valid[sid]
        if datetime.now(timezone.utc) - source.published > MAX_AGE:
            continue
        item["url"] = source.link
        item["source_name"] = source.source
        item["published"] = source.published.isoformat()
        item["image_url"] = source.image_url
        seen.add(sid)
        articles.append(item)
    order = {name: index for index, (name, _) in enumerate(SECTIONS)}
    # Stable sorts preserve the agreed section order and newest-first order within each section.
    articles.sort(key=lambda a: a.get("published", ""), reverse=True)
    articles.sort(key=lambda a: order.get(a.get("section"), 99))
    return {"articles": articles}


def fallback_selection(stories: list[Story]) -> dict[str, Any]:
    """Conservative no-API fallback: send RSS extracts, without AI analysis."""
    selected = sorted(stories, key=lambda s: s.published, reverse=True)
    return {"articles": [{
        "id": s.id, "section": s.section, "headline": s.title,
        "summary": s.excerpt or "El feed no proporciona un extracto suficiente.",
        "why_it_matters": "No se ha generado análisis editorial porque Gemini no ha podido procesar las noticias en este envío.",
        "source_name": s.source, "url": s.link, "published": s.published.isoformat(),
        "image_url": s.image_url,
    } for s in selected[:20]]}




def fetch_article_image(article_url: str) -> str:
    """Read an article page only to discover its Open Graph preview image."""
    request = urllib.request.Request(
        article_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; resumen-diario/2.0)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" not in content_type:
                return ""
            page = response.read(1_500_000).decode("utf-8", errors="replace")
        parser = ImageMetaParser()
        parser.feed(page)
        return usable_image_url(parser.image_url, article_url)
    except Exception as exc:
        print(f"Imagen del artículo no disponible ({type(exc).__name__}).")
        return ""


def download_inline_image(image_url: str) -> tuple[bytes, str] | None:
    """Download a small raster image suitable for embedding in an email."""
    request = urllib.request.Request(
        image_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; resumen-diario/2.0)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"image/jpeg", "image/png", "image/gif"}:
                return None
            data = response.read(1_000_001)
        if not data or len(data) > 1_000_000:
            return None
        subtype = content_type.split("/", 1)[1]
        return data, subtype
    except Exception as exc:
        print(f"No se pudo adjuntar una imagen ({type(exc).__name__}).")
        return None


def prepare_article_images(editorial: dict[str, Any]) -> None:
    """Resolve and download story thumbnails before composing the MIME email."""
    articles = editorial.get("articles", [])
    # First discover missing image URLs from the selected articles only.
    missing = [a for a in articles if not usable_image_url(str(a.get("image_url", "")))]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(fetch_article_image, str(a.get("url", ""))): a for a in missing}
        for future in as_completed(futures):
            article = futures[future]
            try:
                article["image_url"] = future.result()
            except Exception:
                article["image_url"] = ""

    # Download thumbnails in parallel, keeping the total message reasonably small.
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(download_inline_image, usable_image_url(str(a.get("image_url", "")))): (index, a)
            for index, a in enumerate(articles, 1)
            if usable_image_url(str(a.get("image_url", "")))
        }
        for future in as_completed(futures):
            index, article = futures[future]
            try:
                result = future.result()
                if result:
                    data, subtype = result
                    article["_image_data"] = data
                    article["_image_subtype"] = subtype
                    article["_image_cid"] = f"news-image-{index}@resumen-diario"
            except Exception:
                pass

    attached = sum(1 for article in articles if article.get("_image_data"))
    print(f"Imágenes incrustadas en el correo: {attached}/{len(articles)} noticias.")

def fetch_yahoo_quote(symbol: str) -> dict[str, Any] | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol, safe='')}"
    params = urllib.parse.urlencode({"range": "5d", "interval": "1d"})
    request = urllib.request.Request(
        url + "?" + params,
        headers={"User-Agent": "Mozilla/5.0 (compatible; resumen-diario/2.0)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = payload["chart"]["result"][0]
        closes = [v for v in result["indicators"]["quote"][0]["close"] if v is not None]
        if len(closes) < 2:
            return None
        meta = result.get("meta", {})
        last, previous = closes[-1], closes[-2]
        return {
            "price": last, "change_pct": ((last / previous) - 1) * 100,
            "currency": meta.get("currency", ""),
            "market_state": meta.get("marketState", ""),
        }
    except Exception as exc:
        print(f"Mercado {symbol}: no disponible ({type(exc).__name__}).")
        return None


MARKETS = [
    ("S&P 500", "^GSPC"),
    ("Nasdaq Composite", "^IXIC"),
    ("Ibex 35", "^IBEX"),
    ("Euro Stoxx 50", "^STOXX50E"),
    ("Bitcoin", "BTC-USD"),
    ("Oro (futuros)", "GC=F"),
    ("Petróleo WTI", "CL=F"),
    ("EUR/USD", "EURUSD=X"),
]


def collect_markets() -> list[dict[str, Any]]:
    values = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_yahoo_quote, symbol): (name, symbol)
                   for name, symbol in MARKETS}
        for future in as_completed(futures):
            name, symbol = futures[future]
            quote = future.result()
            if quote:
                values.append({"name": name, **quote})
    order = {name: i for i, (name, _) in enumerate(MARKETS)}
    values.sort(key=lambda item: order[item["name"]])
    return values


def fmt_number(value: float, currency: str) -> str:
    if currency == "USD":
        return f"{value:,.2f} USD"
    if currency == "EUR":
        return f"{value:,.2f} EUR"
    return f"{value:,.2f}"


def spanish_date(value: datetime) -> str:
    days = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    months = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
              "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    return f"{days[value.weekday()]}, {value.day} de {months[value.month - 1]} de {value.year}"


def build_email(editorial: dict[str, Any], markets: list[dict[str, Any]], ai_used: bool,
                ai_error: str = "") -> EmailMessage:
    now = datetime.now(BERLIN)
    articles = editorial.get("articles", [])
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name, _ in SECTIONS}
    for article in articles:
        if article.get("section") in grouped:
            grouped[article["section"]].append(article)

    msg = EmailMessage()
    msg["Subject"] = f"Tu resumen diario — {now.strftime('%d/%m/%Y')}"
    plain = [f"TU RESUMEN DIARIO\n{spanish_date(now)}\n"]
    for section, _ in SECTIONS:
        for article in grouped[section]:
            plain.extend([
                f"\n{article.get('headline', 'Noticia')}\n{section}",
                article.get("summary", ""),
                f"Por qué importa: {article.get('why_it_matters', '')}",
                f"Fuente: {article.get('source_name', '')} — {article.get('url', '')}",
            ])
            if article.get("image_url"):
                plain.append(f"Imagen: {article['image_url']}")
    plain.append("\nSEGUIMIENTO DE MERCADOS")
    for market in markets:
        sign = "+" if market["change_pct"] >= 0 else ""
        plain.append(f"{market['name']}: {fmt_number(market['price'], market['currency'])} ({sign}{market['change_pct']:.2f}%)")
    plain.append("\nDatos de mercado indicativos. Las variaciones comparan los dos últimos cierres disponibles.")
    if not ai_used:
        if not os.environ.get("GEMINI_API_KEY", "").strip():
            plain.append("\nAviso: GEMINI_API_KEY no está disponible; se muestran extractos RSS sin análisis editorial.")
        else:
            plain.append(f"\nAviso: Gemini no pudo analizar las noticias ({ai_error or 'error no identificado'}); se muestran extractos RSS.")
    msg.set_content("\n".join(plain))

    cards = []
    for section, _ in SECTIONS:
        entries = []
        for article in grouped[section]:
            published = parse_date(article.get("published", "")) or now.astimezone(timezone.utc)
            image_html = ""
            if article.get("_image_cid"):
                image_html = (
                    f"<img src='cid:{html.escape(article['_image_cid'], quote=True)}' "
                    f"alt='Imagen de {html.escape(article.get('headline', 'la noticia'), quote=True)}' "
                    "style='display:block;width:100%;max-width:620px;height:auto;max-height:340px;"
                    "object-fit:cover;border-radius:8px;margin:0 0 12px'>"
                )
            elif article.get("image_url"):
                image_html = (
                    f"<img src='{html.escape(article['image_url'], quote=True)}' "
                    "alt='Imagen de la noticia' style='display:block;width:100%;max-width:620px;"
                    "height:auto;max-height:340px;object-fit:cover;border-radius:8px;margin:0 0 12px'>"
                )
            entries.append(f"""
            <article style="padding:17px 0;border-bottom:1px solid #e5e7eb">
              {image_html}
              <h3 style="font-size:17px;line-height:1.4;margin:0 0 8px">
                <a href="{html.escape(article.get('url', ''), quote=True)}" style="color:#1d4ed8;text-decoration:none">{html.escape(article.get('headline', 'Noticia'))}</a>
              </h3>
              <p style="font-size:14px;line-height:1.65;color:#334155;margin:0 0 8px">{html.escape(article.get('summary', ''))}</p>
              <p style="font-size:13px;line-height:1.55;color:#475569;margin:0 0 8px"><strong>Por qué importa:</strong> {html.escape(article.get('why_it_matters', ''))}</p>
              <p style="font-size:12px;color:#64748b;margin:0">{html.escape(article.get('source_name', ''))} · {published.astimezone(BERLIN).strftime('%d/%m/%Y %H:%M')} · <a href="{html.escape(article.get('url', ''), quote=True)}">Fuente</a></p>
            </article>""")
        if entries:
            cards.append(f"<h2 style='font-size:20px;margin:26px 0 4px'>{html.escape(section)}</h2>{''.join(entries)}")

    market_rows = []
    for market in markets:
        sign = "+" if market["change_pct"] >= 0 else ""
        market_rows.append(
            f"<tr><td style='padding:9px;border-bottom:1px solid #e5e7eb'>{html.escape(market['name'])}</td>"
            f"<td style='padding:9px;text-align:right;border-bottom:1px solid #e5e7eb'>{fmt_number(market['price'], market['currency'])}</td>"
            f"<td style='padding:9px;text-align:right;border-bottom:1px solid #e5e7eb'>{sign}{market['change_pct']:.2f}%</td></tr>"
        )
    market_html = (
        "<h2 style='font-size:20px;margin:30px 0 8px'>Seguimiento de mercados</h2>"
        "<p style='font-size:12px;color:#64748b'>Variación frente al cierre anterior disponible. Datos indicativos.</p>"
        "<table style='width:100%;border-collapse:collapse;font-size:13px'><thead><tr>"
        "<th style='text-align:left;padding:8px'>Activo</th><th style='text-align:right;padding:8px'>Último valor</th>"
        "<th style='text-align:right;padding:8px'>Variación</th></tr></thead><tbody>"
        + ("".join(market_rows) if market_rows else "<tr><td colspan='3'>Datos de mercado no disponibles hoy.</td></tr>")
        + "</tbody></table>"
    )
    if ai_used:
        note = ""
    elif not os.environ.get("GEMINI_API_KEY", "").strip():
        note = (
            "<p style='padding:10px;background:#fff7ed;color:#9a3412;font-size:12px'>"
            "GEMINI_API_KEY no está disponible en el entorno de GitHub Actions. "
            "Este envío contiene extractos RSS sin análisis editorial.</p>"
        )
    else:
        note = (
            "<p style='padding:10px;background:#fff7ed;color:#9a3412;font-size:12px'>"
            "La clave GEMINI_API_KEY sí está presente, pero la llamada a Gemini ha fallado. "
            f"Motivo comunicado por la API: {html.escape(ai_error or 'error no identificado')}."
            " Se ha utilizado el modo RSS como respaldo.</p>"
        )
    html_body = f"""<!doctype html><html lang="es"><body style="margin:0;background:#f8fafc;font-family:Arial,Helvetica,sans-serif;color:#0f172a">
    <main style="max-width:720px;margin:24px auto;padding:28px;background:#fff;border:1px solid #e2e8f0;border-radius:12px">
      <p style="font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#64748b;margin:0 0 8px">Briefing personal</p>
      <h1 style="font-size:27px;margin:0 0 6px">Tu resumen diario</h1>
      <p style="font-size:14px;color:#64748b;margin:0 0 18px">{html.escape(spanish_date(now))} · Novedades de las últimas 24 horas</p>
      {note}{''.join(cards) if cards else '<p>No se han encontrado noticias suficientemente relevantes en las últimas 24 horas.</p>'}
      {market_html}
      <p style="font-size:11px;line-height:1.5;color:#64748b;margin:24px 0 0">Las noticias se seleccionan a partir de feeds RSS. Los datos de mercado proceden de Yahoo Finance y pueden tener retraso. Verifica los datos antes de tomar decisiones de inversión.</p>
    </main></body></html>"""
    msg.add_alternative(html_body, subtype="html")
    html_part = msg.get_body(preferencelist=("html",))
    if html_part is not None:
        for article in articles:
            image_data = article.get("_image_data")
            if not image_data:
                continue
            html_part.add_related(
                image_data,
                maintype="image",
                subtype=article["_image_subtype"],
                cid=f"<{article['_image_cid']}>",
                disposition="inline",
            )
    return msg


def send_email(msg: EmailMessage, address: str, recipient: str, app_password: str) -> None:
    msg["From"] = address
    msg["To"] = recipient
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as server:
        server.login(address, app_password.replace(" ", ""))
        server.send_message(msg)


def main() -> int:
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    recipient = os.environ.get("GMAIL_RECIPIENT", "").strip() or address
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()

    if not address or not app_password:
        print("Faltan GMAIL_ADDRESS y/o GMAIL_APP_PASSWORD.", file=sys.stderr)
        return 2
    if "@" not in address or "@" not in recipient:
        print("GMAIL_ADDRESS y GMAIL_RECIPIENT deben ser correos válidos.", file=sys.stderr)
        return 2

    print("Recopilando noticias de las últimas 24 horas…")
    stories = collect_stories()
    ai_used = False
    ai_error = ""
    if api_key:
        print("GEMINI_API_KEY detectada en el entorno de GitHub Actions.")
        try:
            print(f"Analizando y seleccionando noticias con {GEMINI_MODEL}…")
            editorial = editorial_selection(stories, api_key)
            ai_used = True
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8", errors="replace"))
                detail = payload.get("error", {}).get("message", "respuesta de error sin detalle")
                status = payload.get("error", {}).get("status", "")
            except Exception:
                detail, status = "respuesta de error no legible", ""
            detail = str(detail).replace(api_key, "[CLAVE OCULTA]")[:500]
            ai_error = f"HTTP {exc.code} {status}: {detail}".strip()
            print(f"Gemini API rechazó la petición: {ai_error}", file=sys.stderr)
            editorial = fallback_selection(stories)
        except Exception as exc:
            ai_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            ai_error = ai_error.replace(api_key, "[CLAVE OCULTA]")
            print(f"Gemini no disponible: {ai_error}", file=sys.stderr)
            editorial = fallback_selection(stories)
    else:
        ai_error = "El secreto GEMINI_API_KEY no está definido o está vacío."
        print(ai_error, file=sys.stderr)
        editorial = fallback_selection(stories)

    print("Consultando seguimiento de mercados…")
    markets = collect_markets()
    print(f"Noticias seleccionadas: {len(editorial.get('articles', []))}; activos con datos: {len(markets)}")
    prepare_article_images(editorial)
    message = build_email(editorial, markets, ai_used, ai_error)
    send_email(message, address, recipient, app_password)
    print("Correo enviado correctamente.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
