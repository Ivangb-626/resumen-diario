#!/usr/bin/env python3
"""Build and email a Spanish daily news briefing from RSS + Gemini."""

from __future__ import annotations

import email.utils
import difflib
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
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


class ArticleImageParser(HTMLParser):
    """Extract only article lead images from structured data and page metadata."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_images: list[str] = []
        self.jsonld_blocks: list[str] = []
        self._in_jsonld = False
        self._jsonld_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "").strip() for key, value in attrs}
        if tag.lower() == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            if key in {"og:image", "og:image:url", "twitter:image", "twitter:image:src"}:
                if values.get("content"):
                    self.meta_images.append(values["content"])
        if tag.lower() == "script" and values.get("type", "").lower() == "application/ld+json":
            self._in_jsonld = True
            self._jsonld_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._jsonld_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_jsonld:
            self.jsonld_blocks.append("".join(self._jsonld_parts))
            self._in_jsonld = False
            self._jsonld_parts = []


def jsonld_article_images(raw: str) -> list[str]:
    """Walk JSON-LD structures and return images only from Article-like objects."""
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                walk(child)
            return
        if not isinstance(value, dict):
            return
        kind = value.get("@type", [])
        kinds = [kind] if isinstance(kind, str) else kind if isinstance(kind, list) else []
        is_article = any(str(item).lower() in {
            "article", "newsarticle", "reportagearticle", "analysisnewsarticle",
            "opinionnewsarticle", "blogposting",
        } for item in kinds)
        if is_article and value.get("image"):
            images = value["image"] if isinstance(value["image"], list) else [value["image"]]
            for image in images:
                if isinstance(image, str):
                    found.append(image)
                elif isinstance(image, dict):
                    candidate = image.get("url") or image.get("contentUrl")
                    if isinstance(candidate, str):
                        found.append(candidate)
        for key in ("@graph", "mainEntity", "mainEntityOfPage", "hasPart"):
            if key in value:
                walk(value[key])

    try:
        walk(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        pass
    return found

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
    # Temporary capacity errors (especially HTTP 503) are common; retry with backoff.
    retry_delays = (5, 15, 30)
    for attempt in range(len(retry_delays) + 1):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                result = json.loads(response.read().decode("utf-8"))
            return result["candidates"][0]["content"]["parts"][0]["text"]
        except HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= len(retry_delays):
                raise
            delay = retry_delays[attempt]
            print(
                f"Gemini devuelve HTTP {exc.code}; reintento {attempt + 1}/"
                f"{len(retry_delays)} en {delay} segundos…",
                file=sys.stderr,
            )
            time.sleep(delay)


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

IDIOMA OBLIGATORIO:
- Todo el contenido editorial debe estar redactado en castellano natural, claro y
  correcto, aunque el candidato original esté en inglés, alemán u otro idioma.
- Traduce al castellano todos los titulares. No dejes titulares en el idioma original.
- Escribe también en castellano los resúmenes y el apartado "why_it_matters".
- Conserva nombres propios, nombres oficiales de organizaciones, marcas y medios.
- No traduzcas el nombre propio de la fuente.

Para cada noticia elegida devuelve:
- id: ID exacto de un candidato.
- section: una de las cuatro secciones anteriores.
- headline: titular traducido al castellano, fiel al candidato y sin exagerar.
- summary: resumen factual en castellano de 3-5 frases breves.
- why_it_matters: por qué importa y consecuencias plausibles, en castellano, distinguiendo hechos
  de interpretación.
- source_name: nombre de la fuente del candidato (nombre oficial).
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
        item["rss_image_url"] = source.image_url
        item["original_headline"] = source.title
        seen.add(sid)
        articles.append(item)
    order = {name: index for index, (name, _) in enumerate(SECTIONS)}
    # Stable sorts preserve the agreed section order and newest-first order within each section.
    articles.sort(key=lambda a: a.get("published", ""), reverse=True)
    articles.sort(key=lambda a: order.get(a.get("section"), 99))
    return {"articles": articles}


def is_probably_spanish(text: str) -> bool:
    """Conservative offline language check for RSS-only fallback content."""
    normalized = f" {text.lower()} "
    markers = {
        " el ", " la ", " los ", " las ", " un ", " una ", " unos ", " unas ",
        " de ", " del ", " que ", " y ", " en ", " por ", " para ", " con ",
        " como ", " sobre ", " entre ", " desde ", " según ", " tras ",
        " este ", " esta ", " estos ", " estas ", " será ", " han ", " hay ",
        " más ", " también ", " nuevo ", " nueva ", " gobierno ", " país ",
    }
    return sum(1 for marker in markers if marker in normalized) >= 2


def fallback_selection(stories: list[Story]) -> dict[str, Any]:
    """Publish only clearly Spanish RSS items; never expose untranslated headlines."""
    articles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for story in sorted(stories, key=lambda item: item.published, reverse=True):
        combined = f"{story.title}. {story.excerpt}"
        if not story.excerpt or not is_probably_spanish(combined):
            continue
        key = re.sub(r"[^a-z0-9áéíóúüñ]+", "", story.title.lower())
        if key in seen:
            continue
        seen.add(key)
        articles.append({
            "id": story.id,
            "section": story.section,
            "headline": story.title,
            "summary": story.excerpt[:500],
            "why_it_matters": (
                "Se incluye como información de actualidad procedente del feed RSS. "
                "Al no estar disponible el análisis editorial, conviene consultar la fuente."
            ),
            "source_name": story.source,
            "url": story.link,
            "published": story.published.isoformat(),
            "image_url": story.image_url,
            "rss_image_url": story.image_url,
            "original_headline": story.title,
        })
    order = {name: index for index, (name, _) in enumerate(SECTIONS)}
    articles.sort(key=lambda item: item.get("published", ""), reverse=True)
    articles.sort(key=lambda item: order.get(item.get("section"), 99))
    print(
        f"Respaldo RSS: {len(articles)} noticias con indicios suficientes de estar en castellano.",
        file=sys.stderr,
    )
    return {"articles": articles}



def image_text_score(left: str, right: str) -> float:
    """Conservative title similarity score for matching external image results."""
    def tokens(value: str) -> set[str]:
        words = re.findall(r"[a-záéíóúüñ0-9]{3,}", (value or "").lower())
        ignored = {"the", "and", "for", "with", "from", "that", "this", "una", "uno",
                   "los", "las", "del", "por", "para", "con", "sobre", "entre"}
        return {word for word in words if word not in ignored}
    a, b = tokens(left), tokens(right)
    if not a or not b:
        return 0.0
    overlap = len(a & b) / max(1, min(len(a), len(b)))
    sequence = difflib.SequenceMatcher(None, " ".join(sorted(a)), " ".join(sorted(b))).ratio()
    return max(overlap, sequence)


def image_url_is_generic(image_url: str) -> bool:
    path = urllib.parse.urlparse(image_url).path.lower()
    return any(term in path for term in (
        "logo", "avatar", "favicon", "placeholder", "default-image",
        "default_image", "brandmark", "masthead", "sprite", "icon"
    ))


def fetch_article_image(article: dict[str, Any]) -> tuple[str, str]:
    """First choice: lead image explicitly attached to the exact publisher article."""
    article_url = str(article.get("url", "")).strip()
    if not article_url:
        return "", ""
    request = urllib.request.Request(
        article_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; resumen-diario/2.0)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=18) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            final_url = response.geturl()
            if "text/html" not in content_type:
                return "", ""
            page = response.read(2_500_000).decode("utf-8", errors="replace")
        parser = ArticleImageParser()
        parser.feed(page)
        candidates: list[str] = []
        for block in parser.jsonld_blocks:
            candidates.extend(jsonld_article_images(block))
        candidates.extend(parser.meta_images)
        for candidate in candidates:
            image_url = usable_image_url(candidate, final_url)
            if not image_url or image_url_is_generic(image_url):
                continue
            host = urllib.parse.urlparse(image_url).netloc.lower()
            if "google.com" in host or "googleusercontent.com" in host:
                continue
            return image_url, final_url
    except Exception as exc:
        print(f"No se pudo extraer imagen del artículo ({type(exc).__name__}: {str(exc)[:120]}).")
    return "", ""


def fetch_gdelt_images(articles: list[dict[str, Any]]) -> dict[str, str]:
    """Find social images for the same stories in GDELT's global news index."""
    missing = list(articles)
    if not missing:
        return {}
    # One batched query avoids hammering GDELT's rate-limited public API.
    titles = [str(a.get("original_headline") or a.get("headline") or "").strip()
              for a in missing]
    titles = [t for t in titles if len(t) >= 12]
    if not titles:
        return {}
    # Keep the URL manageable; GDELT accepts exact-phrase OR groups.
    query = "(" + " OR ".join('"' + t.replace('"', " ")[:180] + '"' for t in titles[:18]) + ")"
    params = urllib.parse.urlencode({
        "query": query, "mode": "artlist", "format": "json",
        "timespan": "3d", "maxrecords": "250", "sort": "HybridRel",
    })
    request = urllib.request.Request(
        "https://api.gdeltproject.org/api/v2/doc/doc?" + params,
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        matches = payload.get("articles", [])
        mapped: dict[str, str] = {}
        for article in missing:
            original = str(article.get("original_headline") or article.get("headline") or "")
            article_domain = (urllib.parse.urlparse(str(article.get("url", ""))).hostname or "").lower()
            best_url, best_score = "", 0.0
            for candidate in matches:
                candidate_image = usable_image_url(str(candidate.get("socialimage") or ""))
                candidate_title = str(candidate.get("title") or "")
                candidate_domain = str(candidate.get("domain") or "").lower()
                score = image_text_score(original, candidate_title)
                same_domain = bool(article_domain and (
                    candidate_domain == article_domain or candidate_domain.endswith("." + article_domain)
                    or article_domain.endswith("." + candidate_domain)
                ))
                if candidate_image and not image_url_is_generic(candidate_image):
                    # Exact title match is strong; syndicated copies require a higher
                    # threshold when the publisher domain differs.
                    threshold = 0.48 if same_domain else 0.68
                    if score >= threshold and score > best_score:
                        best_url, best_score = candidate_image, score
            if best_url:
                mapped[str(article.get("id") or article.get("url"))] = best_url
        print(f"GDELT: imágenes de artículos coincidentes para {len(mapped)}/{len(missing)} noticias.")
        return mapped
    except Exception as exc:
        print(f"GDELT no disponible para imágenes ({type(exc).__name__}: {str(exc)[:120]}).")
        return {}


def fetch_brave_image(article: dict[str, Any], api_key: str) -> str:
    """Optional final discovery source; accept only results whose title matches the story."""
    original = str(article.get("original_headline") or article.get("headline") or "").strip()
    if not original:
        return ""
    source = str(article.get("source_name") or "")
    query = " ".join(part for part in (original[:220], source) if part)
    params = urllib.parse.urlencode({
        "q": query, "count": "12", "country": "ALL",
        "search_lang": "en", "safesearch": "strict",
    })
    request = urllib.request.Request(
        "https://api.search.brave.com/res/v1/images/search?" + params,
        headers={
            "X-Subscription-Token": api_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        best_url, best_score = "", 0.0
        for result in payload.get("results", []):
            title = str(result.get("title") or "")
            score = image_text_score(original, title)
            props = result.get("properties") or {}
            # Prefer Brave's normalized 500px proxy thumbnail: it is more likely
            # to be directly embeddable than the publisher's original WebP asset.
            thumb = result.get("thumbnail") or {}
            candidate = usable_image_url(str(thumb.get("src") or ""))
            if not candidate:
                candidate = usable_image_url(str(props.get("url") or ""))
            width = props.get("width") or 0
            height = props.get("height") or 0
            if candidate and not image_url_is_generic(candidate) and score >= 0.52:
                if width and height and (width < 400 or height < 220):
                    continue
                if score > best_score:
                    best_url, best_score = candidate, score
        return best_url
    except Exception as exc:
        print(f"Brave Image Search no disponible ({type(exc).__name__}: {str(exc)[:100]}).")
        return ""


def download_inline_image(image_url: str) -> tuple[bytes, str] | None:
    """Download a raster image for reliable inline email display."""
    request = urllib.request.Request(
        image_url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; resumen-diario/2.0)"},
    )
    try:
        with urllib.request.urlopen(request, timeout=18) as response:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
                return None
            data = response.read(1_500_001)
        if not data or len(data) > 1_500_000:
            return None
        subtype = content_type.split("/", 1)[1]
        # Email clients do not consistently support WebP inline; convert only by
        # avoiding it here and allowing the next candidate to be tried instead.
        if subtype == "webp":
            return None
        return data, subtype
    except Exception as exc:
        print(f"No se pudo descargar imagen ({urllib.parse.urlparse(image_url).netloc}: {type(exc).__name__}).")
        return None


def download_first_available(candidates: list[tuple[str, str]]) -> tuple[bytes, str, str, str] | None:
    """Try each candidate in priority order and return the first embeddable image."""
    seen: set[str] = set()
    for image_url, source in candidates:
        if not image_url or image_url in seen:
            continue
        seen.add(image_url)
        result = download_inline_image(image_url)
        if result:
            data, subtype = result
            return data, subtype, image_url, source
    return None


def prepare_article_images(editorial: dict[str, Any]) -> None:
    """Collect article-linked images and exhaust reliable sources before leaving a story without one."""
    articles = editorial.get("articles", [])
    candidates: dict[int, list[tuple[str, str]]] = {
        id(article): [] for article in articles
    }

    # Tier 1: publisher's own JSON-LD / Open Graph metadata.
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(fetch_article_image, article): article for article in articles}
        for future in as_completed(futures):
            article = futures[future]
            try:
                image_url, publisher_url = future.result()
                if image_url:
                    candidates[id(article)].append((image_url, "publisher"))
                    article["image_source_page"] = publisher_url
            except Exception as exc:
                print(f"Error leyendo imagen del artículo: {type(exc).__name__}.")

    # Tier 2: original RSS media:content / media:thumbnail / enclosure.
    for article in articles:
        rss_candidate = usable_image_url(str(article.get("rss_image_url") or ""))
        if rss_candidate and not image_url_is_generic(rss_candidate):
            candidates[id(article)].append((rss_candidate, "rss"))

    # Tier 3: GDELT socialimage, matched against each original headline.
    gdelt_map = fetch_gdelt_images(articles)
    for article in articles:
        candidate = gdelt_map.get(str(article.get("id") or article.get("url")), "")
        if candidate:
            candidates[id(article)].append((candidate, "gdelt"))

    # Download all article-linked candidates before trying a general image index.
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(download_first_available, candidates[id(article)]): (index, article)
            for index, article in enumerate(articles, 1)
        }
        for future in as_completed(futures):
            index, article = futures[future]
            try:
                result = future.result()
                if result:
                    data, subtype, image_url, image_source = result
                    article["image_url"] = image_url
                    article["image_source"] = image_source
                    article["_image_data"] = data
                    article["_image_subtype"] = subtype
                    article["_image_cid"] = f"news-image-{index}@resumen-diario"
                else:
                    article["image_url"] = ""
            except Exception as exc:
                article["image_url"] = ""
                print(f"No se pudo incrustar imagen para una noticia ({type(exc).__name__}).")

    # Tier 4: optional Brave Image Search only for stories still lacking a working
    # image. Results are accepted only with a strong headline/title match.
    brave_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if brave_key:
        missing = [a for a in articles if not a.get("_image_data")]
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(fetch_brave_image, article, brave_key): article for article in missing}
            for future in as_completed(futures):
                article = futures[future]
                try:
                    candidate = future.result()
                    if not candidate:
                        continue
                    result = download_inline_image(candidate)
                    if result:
                        data, subtype = result
                        index = articles.index(article) + 1
                        article["image_url"] = candidate
                        article["image_source"] = "brave"
                        article["_image_data"] = data
                        article["_image_subtype"] = subtype
                        article["_image_cid"] = f"news-image-{index}@resumen-diario"
                except Exception as exc:
                    print(f"Brave no pudo aportar imagen para una noticia ({type(exc).__name__}).")
    else:
        print("Brave Image Search omitido: falta el secreto opcional BRAVE_SEARCH_API_KEY.")

    attached = sum(1 for article in articles if article.get("_image_data"))
    source_counts: dict[str, int] = {}
    for article in articles:
        if article.get("_image_data"):
            source = str(article.get("image_source") or "publisher")
            source_counts[source] = source_counts.get(source, 0) + 1
    print(
        f"Imágenes incrustadas: {attached}/{len(articles)}. "
        f"Por fuente: {source_counts}. Sin imagen descargable: {len(articles) - attached}."
    )


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
    ("Euro Stoxx 50", "^STOXX50E"),
    ("Bitcoin", "BTC-USD"),
    ("Oro (futuros)", "GC=F"),
    ("Petróleo WTI", "CL=F"),
    ("EUR/USD", "EURUSD=X"),
]

# Large, widely followed global companies only. This fixed universe avoids obscure
# micro-caps dominating the daily movers list because of illiquid price jumps.
MOVER_WATCHLIST = [
    ("NVIDIA", "NVDA"), ("Apple", "AAPL"), ("Microsoft", "MSFT"),
    ("Amazon", "AMZN"), ("Alphabet", "GOOGL"), ("Meta", "META"),
    ("Tesla", "TSLA"), ("Broadcom", "AVGO"), ("Berkshire Hathaway", "BRK-B"),
    ("JPMorgan Chase", "JPM"), ("Eli Lilly", "LLY"), ("Walmart", "WMT"),
    ("Oracle", "ORCL"), ("AMD", "AMD"), ("Netflix", "NFLX"),
    ("Visa", "V"), ("Mastercard", "MA"), ("Exxon Mobil", "XOM"),
    ("Costco", "COST"), ("Salesforce", "CRM"), ("ASML", "ASML"),
    ("SAP", "SAP"), ("LVMH", "MC.PA"), ("Novo Nordisk", "NOVO-B.CO"),
    ("Siemens", "SIE.DE"), ("TotalEnergies", "TTE.PA"),
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
                values.append({"name": name, "symbol": symbol, **quote})
    order = {name: i for i, (name, _) in enumerate(MARKETS)}
    values.sort(key=lambda item: order[item["name"]])
    return values


def collect_market_movers(limit: int = 5) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    values = []
    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {pool.submit(fetch_yahoo_quote, symbol): (name, symbol)
                   for name, symbol in MOVER_WATCHLIST}
        for future in as_completed(futures):
            name, symbol = futures[future]
            quote = future.result()
            if quote:
                values.append({"name": name, "symbol": symbol, **quote})
    values.sort(key=lambda item: item["change_pct"], reverse=True)
    gainers = [item for item in values if item["change_pct"] > 0][:limit]
    losers = [item for item in reversed(values) if item["change_pct"] < 0][:limit]
    return gainers, losers


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


def build_email(editorial: dict[str, Any], markets: list[dict[str, Any]],
                movers_up: list[dict[str, Any]], movers_down: list[dict[str, Any]],
                ai_used: bool, ai_error: str = "") -> EmailMessage:
    now = datetime.now(BERLIN)
    articles = editorial.get("articles", [])
    grouped: dict[str, list[dict[str, Any]]] = {name: [] for name, _ in SECTIONS}
    for article in articles:
        if article.get("section") in grouped:
            grouped[article["section"]].append(article)

    msg = EmailMessage()
    msg["Subject"] = f"Tu resumen diario — {now.strftime('%d/%m/%Y')}"
    plain = [f"TU RESUMEN DIARIO\\n{spanish_date(now)}\\n"]
    plain.append("SEGUIMIENTO DE MERCADOS")
    for market in markets:
        sign = "+" if market["change_pct"] >= 0 else ""
        plain.append(f"{market['name']}: {fmt_number(market['price'], market['currency'])} ({sign}{market['change_pct']:.2f}%)")
    plain.append("Datos de mercado indicativos. Las variaciones comparan los dos últimos cierres disponibles.")
    plain.append("\\nMAYORES SUBIDAS (GRANDES EMPRESAS)")
    for item in movers_up:
        sign = "+" if item["change_pct"] >= 0 else ""
        plain.append(f"{item['name']} ({item['symbol']}): {sign}{item['change_pct']:.2f}% — {fmt_number(item['price'], item['currency'])}")
    if not movers_up:
        plain.append("Datos no disponibles.")
    plain.append("\\nMAYORES BAJADAS (GRANDES EMPRESAS)")
    for item in movers_down:
        sign = "+" if item["change_pct"] >= 0 else ""
        plain.append(f"{item['name']} ({item['symbol']}): {sign}{item['change_pct']:.2f}% — {fmt_number(item['price'], item['currency'])}")
    if not movers_down:
        plain.append("Datos no disponibles.")
    plain.append("Universo limitado a grandes compañías internacionales conocidas; ordenado por variación porcentual diaria.")
    for section, _ in SECTIONS:
        section_articles = grouped[section]
        if not section_articles:
            continue
        plain.append(f"\\n{section.upper()} — {len(section_articles)} noticias")
        for article in section_articles:
            plain.extend([
                f"\\n{article.get('headline', 'Noticia')}",
                article.get("summary", ""),
                f"Por qué importa: {article.get('why_it_matters', '')}",
                f"Fuente: {article.get('source_name', '')} — {article.get('url', '')}",
            ])
            if article.get("image_url"):
                plain.append(f"Imagen: {article['image_url']}")
    if not ai_used:
        if not os.environ.get("GEMINI_API_KEY", "").strip():
            plain.append("\\nAviso: GEMINI_API_KEY no está disponible; se muestran solo noticias RSS identificadas como probablemente escritas en castellano, sin análisis editorial.")
        else:
            plain.append(f"\\nAviso: Gemini no pudo analizar las noticias ({ai_error or 'error no identificado'}); se muestran solo noticias RSS identificadas como probablemente escritas en castellano, sin análisis editorial.")
    msg.set_content("\n".join(plain))

    cards = []
    for section, _ in SECTIONS:
        section_articles = grouped[section]
        if not section_articles:
            continue
        entries = []
        for article in section_articles:
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
            image_credit_html = ""
            if article.get("image_credit"):
                credit = html.escape(str(article["image_credit"]))
                image_credit_html = (
                    f"<p style='font-size:10px;color:#64748b;margin:-7px 0 10px'>"
                    f"Imagen: {credit}</p>"
                )
            entries.append(f"""
            <article style="padding:17px 0;border-bottom:1px solid #e5e7eb">
              {image_html}
              {image_credit_html}
              <h3 style="font-size:17px;line-height:1.4;margin:0 0 8px">
                <a href="{html.escape(article.get('url', ''), quote=True)}" style="color:#1d4ed8;text-decoration:none">{html.escape(article.get('headline', 'Noticia'))}</a>
              </h3>
              <p style="font-size:14px;line-height:1.65;color:#334155;margin:0 0 8px">{html.escape(article.get('summary', ''))}</p>
              <p style="font-size:13px;line-height:1.55;color:#475569;margin:0 0 8px"><strong>Por qué importa:</strong> {html.escape(article.get('why_it_matters', ''))}</p>
              <p style="font-size:12px;color:#64748b;margin:0">{html.escape(article.get('source_name', ''))} · {published.astimezone(BERLIN).strftime('%d/%m/%Y %H:%M')} · <a href="{html.escape(article.get('url', ''), quote=True)}">Fuente</a></p>
            </article>""")
        if entries:
            count = len(section_articles)
            cards.append(
                f"<h2 style='font-size:20px;margin:26px 0 4px'>{html.escape(section)} "
                f"<span style='font-size:14px;font-weight:normal;color:#64748b'>— {count} noticias</span>"
                f"</h2>{''.join(entries)}"
            )

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

    def mover_rows(items: list[dict[str, Any]]) -> str:
        if not items:
            return "<tr><td colspan='3' style='padding:8px'>Datos no disponibles.</td></tr>"
        rows = []
        for item in items:
            sign = "+" if item["change_pct"] >= 0 else ""
            rows.append(
                f"<tr><td style='padding:8px;border-bottom:1px solid #e5e7eb'>{html.escape(item['name'])} "
                f"<span style='color:#64748b'>({html.escape(item['symbol'])})</span></td>"
                f"<td style='padding:8px;text-align:right;border-bottom:1px solid #e5e7eb'>{fmt_number(item['price'], item['currency'])}</td>"
                f"<td style='padding:8px;text-align:right;border-bottom:1px solid #e5e7eb'>{sign}{item['change_pct']:.2f}%</td></tr>"
            )
        return "".join(rows)

    mover_html = (
        "<h3 style='font-size:16px;margin:22px 0 6px'>Mayores subidas — grandes empresas</h3>"
        "<table style='width:100%;border-collapse:collapse;font-size:13px'><tbody>"
        + mover_rows(movers_up) + "</tbody></table>"
        "<h3 style='font-size:16px;margin:18px 0 6px'>Mayores bajadas — grandes empresas</h3>"
        "<table style='width:100%;border-collapse:collapse;font-size:13px'><tbody>"
        + mover_rows(movers_down) + "</tbody></table>"
        "<p style='font-size:11px;color:#64748b'>Selección de grandes compañías internacionales conocidas; "
        "ordenada por variación porcentual frente al cierre anterior disponible.</p>"
    )
    market_html += mover_html
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
            " Se ha utilizado el modo RSS como respaldo, mostrando únicamente noticias que parecen estar redactadas en castellano.</p>"
        )
    html_body = f"""<!doctype html><html lang="es"><body style="margin:0;background:#f8fafc;font-family:Arial,Helvetica,sans-serif;color:#0f172a">
    <main style="max-width:720px;margin:24px auto;padding:28px;background:#fff;border:1px solid #e2e8f0;border-radius:12px">
      <p style="font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#64748b;margin:0 0 8px">Briefing personal</p>
      <h1 style="font-size:27px;margin:0 0 6px">Tu resumen diario</h1>
      <p style="font-size:14px;color:#64748b;margin:0 0 18px">{html.escape(spanish_date(now))} · Novedades de las últimas 24 horas</p>
      {note}{market_html}
      {''.join(cards) if cards else ('<p>No se han encontrado noticias suficientemente relevantes en las últimas 24 horas.</p>' if ai_used else '<p>Gemini no ha podido completar el análisis y no se han encontrado noticias RSS que podamos identificar con suficiente confianza como contenido en castellano. El seguimiento de mercados sí está disponible.</p>')}
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
    movers_up, movers_down = collect_market_movers()
    print(
        f"Noticias seleccionadas: {len(editorial.get('articles', []))}; "
        f"activos principales: {len(markets)}; mayores subidas: {len(movers_up)}; "
        f"mayores bajadas: {len(movers_down)}"
    )
    prepare_article_images(editorial)
    message = build_email(editorial, markets, movers_up, movers_down, ai_used, ai_error)
    send_email(message, address, recipient, app_password)
    print("Correo enviado correctamente.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())