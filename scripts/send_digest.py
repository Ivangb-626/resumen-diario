#!/usr/bin/env python3
"""Build and email a Spanish daily news briefing from RSS + Gemini."""

from __future__ import annotations

import base64
import email.utils
from io import BytesIO
import difflib
import html
import json
import math
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
_GEMINI_VISION_DISABLED = False

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
    """Collect structured, social and in-body images from a publisher page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_images: list[str] = []
        self.jsonld_blocks: list[str] = []
        self.html_images: list[dict[str, Any]] = []
        self._in_jsonld = False
        self._jsonld_parts: list[str] = []
        self._article_depth = 0

    @staticmethod
    def _srcset_urls(value: str) -> list[str]:
        found = []
        for part in (value or "").split(","):
            candidate = part.strip().split(" ", 1)[0]
            if candidate:
                found.append(candidate)
        return found

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {key.lower(): (value or "").strip() for key, value in attrs}
        if tag in {"article", "main"}:
            self._article_depth += 1

        if tag == "meta":
            key = (values.get("property") or values.get("name") or values.get("itemprop") or "").lower()
            if key in {
                "og:image", "og:image:url", "og:image:secure_url", "twitter:image",
                "twitter:image:src", "image", "thumbnail"
            } and values.get("content"):
                self.meta_images.append(values["content"])

        if tag == "link":
            rel = values.get("rel", "").lower()
            if ("image_src" in rel or values.get("itemprop") == "image") and values.get("href"):
                self.meta_images.append(values["href"])

        if tag == "script" and values.get("type", "").lower() == "application/ld+json":
            self._in_jsonld = True
            self._jsonld_parts = []

        if tag in {"img", "source"}:
            urls: list[str] = []
            for key in ("src", "data-src", "data-lazy-src", "data-original",
                        "data-original-src", "data-url", "data-image"):
                if values.get(key):
                    urls.append(values[key])
            for key in ("srcset", "data-srcset"):
                urls.extend(self._srcset_urls(values.get(key, "")))
            for url in urls:
                self.html_images.append({
                    "url": url,
                    "alt": values.get("alt") or values.get("title") or "",
                    "class": values.get("class") or "",
                    "id": values.get("id") or "",
                    "width": values.get("width") or "",
                    "height": values.get("height") or "",
                    "in_article": self._article_depth > 0,
                })

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._jsonld_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script" and self._in_jsonld:
            self.jsonld_blocks.append("".join(self._jsonld_parts))
            self._in_jsonld = False
            self._jsonld_parts = []
        if tag in {"article", "main"}:
            self._article_depth = max(0, self._article_depth - 1)

def jsonld_article_images(raw: str) -> list[str]:
    """Walk nested JSON-LD and return images only from article-like objects."""
    found: list[str] = []
    article_types = {
        "article", "newsarticle", "reportagearticle", "analysisnewsarticle",
        "opinionnewsarticle", "blogposting",
    }

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for child in value:
                walk(child)
            return
        if not isinstance(value, dict):
            return
        kind = value.get("@type", [])
        kinds = [kind] if isinstance(kind, str) else kind if isinstance(kind, list) else []
        normalized = {str(item).lower().rsplit("/", 1)[-1] for item in kinds}
        if normalized & article_types and value.get("image"):
            images = value["image"] if isinstance(value["image"], list) else [value["image"]]
            for image in images:
                if isinstance(image, str):
                    found.append(image)
                elif isinstance(image, dict):
                    candidate = image.get("url") or image.get("contentUrl") or image.get("@id")
                    if isinstance(candidate, str):
                        found.append(candidate)
        # Publishers often nest NewsArticle several layers deep inside @graph.
        for child in value.values():
            if isinstance(child, (dict, list)):
                walk(child)

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


def google_image_url_variants(value: str) -> list[str]:
    """Try larger variants of Googleusercontent thumbnails before the original URL."""
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").lower()
    if not (host == "googleusercontent.com" or host.endswith(".googleusercontent.com")):
        return [value]
    variants: list[str] = []
    # Google image CDN sizing suffixes commonly look like =w300-h300 or =s300.
    sized = re.search(r"=(?:w\d+(?:-h\d+)?|s\d+)(?:-[a-z0-9-]+)?$", parsed.path, re.I)
    if sized:
        path_prefix = parsed.path[:sized.start()]
        for suffix in ("=w1200-h800", "=s1600"):
            variants.append(urllib.parse.urlunparse(parsed._replace(path=path_prefix + suffix)))
    variants.append(value)
    return list(dict.fromkeys(variants))


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


def gemini_request(prompt: str, api_key: str, retry_delays: tuple[int, ...] = (5, 15, 30)) -> str:
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


def image_token_overlap(left: str, right: str) -> float:
    """Measure shared meaningful words without fuzzy character-level false positives."""
    ignored = {
        "the", "and", "for", "with", "from", "that", "this", "una", "uno",
        "los", "las", "del", "por", "para", "con", "sobre", "entre", "como",
        "its", "are", "was", "were", "has", "have", "into", "over", "after",
    }
    a = {
        word for word in re.findall(r"[a-záéíóúüñ0-9]{3,}", (left or "").lower())
        if word not in ignored
    }
    b = {
        word for word in re.findall(r"[a-záéíóúüñ0-9]{3,}", (right or "").lower())
        if word not in ignored
    }
    return len(a & b) / max(1, len(a))


def image_url_is_generic(image_url: str) -> bool:
    path = urllib.parse.urlparse(image_url).path.lower()
    return any(term in path for term in (
        "logo", "avatar", "favicon", "placeholder", "default-image",
        "default_image", "brandmark", "masthead", "sprite", "icon"
    ))


def fetch_article_images(article: dict[str, Any]) -> tuple[list[str], str]:
    """Return several usable image candidates from the exact story page."""
    article_url = str(article.get("url", "")).strip()
    if not article_url:
        return [], ""
    request = urllib.request.Request(
        article_url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        },
    )
    try:
        page = ""
        final_url = article_url
        content_type = ""
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=22) as response:
                    content_type = response.headers.get("Content-Type", "").lower()
                    final_url = response.geturl()
                    if "html" not in content_type:
                        print(f"Imagen publisher: la URL acabó en contenido {content_type or 'desconocido'} ({final_url[:100]}).")
                        return [], final_url
                    page = response.read(4_000_000).decode("utf-8", errors="replace")
                break
            except HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise
                retry_header = exc.headers.get("Retry-After", "") if exc.headers else ""
                try:
                    delay = min(8.0, max(2.0, float(retry_header)))
                except (TypeError, ValueError):
                    delay = 2.5 * (attempt + 1)
                print(f"Publisher HTTP {exc.code}; reintento {attempt + 1}/2 en {delay:.1f}s.")
                time.sleep(delay)
        parser = ArticleImageParser()
        parser.feed(page)

        headline = str(article.get("original_headline") or article.get("headline") or "")
        ranked_html: list[tuple[float, str]] = []
        for item in parser.html_images:
            candidate = usable_image_url(str(item.get("url") or ""), final_url)
            if not candidate or image_url_is_generic(candidate):
                continue
            width = int(item.get("width") or 0) if str(item.get("width") or "").isdigit() else 0
            height = int(item.get("height") or 0) if str(item.get("height") or "").isdigit() else 0
            if (width and width < 260) or (height and height < 150):
                continue
            alt = str(item.get("alt") or "")
            context = " ".join((str(item.get("class") or ""), str(item.get("id") or "")))
            similarity = image_text_score(headline, f"{alt} {context}")
            priority = (2.0 if item.get("in_article") else 0.0)
            priority += min(width, 1600) / 1600 if width else 0.25
            priority += min(height, 1000) / 1000 if height else 0.15
            priority += min(similarity, 1.0)
            lowered_context = context.lower()
            if any(word in lowered_context for word in ("hero", "lead", "featured", "main-image", "article-image", "cover")):
                priority += 0.65
            ranked_html.append((priority, candidate))
        ranked_html.sort(reverse=True)

        candidates: list[str] = []
        for block in parser.jsonld_blocks:
            candidates.extend(jsonld_article_images(block))
        candidates.extend(parser.meta_images)
        candidates.extend(url for _, url in ranked_html)

        unique: list[str] = []
        for candidate in candidates:
            image_url = usable_image_url(candidate, final_url)
            if not image_url or image_url_is_generic(image_url):
                continue
            host = urllib.parse.urlparse(image_url).netloc.lower()
            # Google News story thumbnails may expose a small default variant. Try
            # larger CDN sizes first; still preserve the original as a last option.
            if host.endswith("google.com") and not host.endswith("googleusercontent.com"):
                continue
            for expanded in google_image_url_variants(image_url):
                if expanded not in unique:
                    unique.append(expanded)
                if len(unique) >= 12:
                    break
            if len(unique) >= 12:
                break
        descriptors = article.setdefault("_image_candidate_descriptors", {})
        for item in parser.html_images:
            image_url = usable_image_url(str(item.get("url") or ""), final_url)
            if image_url:
                descriptors[image_url] = " ".join((
                    str(item.get("alt") or ""), str(item.get("class") or ""),
                    str(item.get("id") or ""),
                )).strip()
        # Preserve only actual alt/class metadata; don't fabricate a headline match.
        if not unique:
            print(
                f"Imagen publisher: 0 candidatas; HTML={len(page)} bytes, "
                f"JSON-LD={len(parser.jsonld_blocks)}, OG/Twitter={len(parser.meta_images)}, "
                f"IMG/SOURCE={len(parser.html_images)}, URL={final_url[:100]}."
            )
        return unique, final_url
    except Exception as exc:
        print(f"No se pudo leer imagen del artículo ({type(exc).__name__}: {str(exc)[:140]}).")
        return [], ""


def fetch_gdelt_images(articles: list[dict[str, Any]]) -> dict[str, str]:
    """Search GDELT in small title batches; it is a fallback, not a single point of failure."""
    if not articles:
        return {}
    batches = [articles[index:index + 5] for index in range(0, len(articles), 5)]
    mapped: dict[str, str] = {}

    def fetch_batch(batch: list[dict[str, Any]]) -> dict[str, str]:
        titles = [str(a.get("original_headline") or a.get("headline") or "").strip()
                  for a in batch]
        titles = [t for t in titles if len(t) >= 12]
        if not titles:
            return {}
        query = "(" + " OR ".join('"' + t.replace('"', " ")[:150] + '"' for t in titles) + ")"
        params = urllib.parse.urlencode({
            "query": query, "mode": "artlist", "format": "json",
            "timespan": "3d", "maxrecords": "75", "sort": "HybridRel",
        })
        request = urllib.request.Request(
            "https://api.gdeltproject.org/api/v2/doc/doc?" + params,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=16) as response:
                results = json.loads(response.read().decode("utf-8", errors="replace")).get("articles", [])
        except Exception as exc:
            print(f"GDELT lote sin respuesta ({type(exc).__name__}: {str(exc)[:90]}).")
            return {}
        batch_result: dict[str, str] = {}
        for article in batch:
            original = str(article.get("original_headline") or article.get("headline") or "")
            article_domain = (urllib.parse.urlparse(str(article.get("url", ""))).hostname or "").lower()
            best_url, best_score = "", 0.0
            for candidate in results:
                candidate_image = usable_image_url(str(candidate.get("socialimage") or ""))
                candidate_title = str(candidate.get("title") or "")
                candidate_domain = str(candidate.get("domain") or "").lower()
                score = image_text_score(original, candidate_title)
                same_domain = bool(article_domain and (
                    candidate_domain == article_domain or candidate_domain.endswith("." + article_domain)
                    or article_domain.endswith("." + candidate_domain)
                ))
                threshold = 0.38 if same_domain else 0.56
                if candidate_image and not image_url_is_generic(candidate_image) and score >= threshold and score > best_score:
                    best_url, best_score = candidate_image, score
            if best_url:
                batch_result[str(article.get("id") or article.get("url"))] = best_url
        return batch_result

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(fetch_batch, batch) for batch in batches]
        for future in as_completed(futures):
            try:
                mapped.update(future.result())
            except Exception:
                pass
    print(f"GDELT: imágenes coincidentes para {len(mapped)}/{len(articles)} noticias.")
    return mapped


def fetch_openverse_image(article: dict[str, Any]) -> tuple[str, str]:
    """Search openly licensed images, retaining only candidates tied to the exact headline."""
    original = str(article.get("original_headline") or article.get("headline") or "").strip()
    query = str(article.get("image_search_query") or fallback_image_query(article) or original).strip()
    if len(query) < 6:
        return "", ""
    params = urllib.parse.urlencode({
        "q": query[:180], "page_size": "20", "mature": "false",
    })
    request = urllib.request.Request(
        "https://api.openverse.org/v1/images/?" + params,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=13) as response:
            results = json.loads(response.read().decode("utf-8", errors="replace")).get("results", [])
        best_url, best_score, best_credit, best_descriptor = "", 0.0, "", ""
        for result in results:
            title = str(result.get("title") or "")
            tags = result.get("tags") or []
            tag_text = " ".join(
                str(tag.get("name") or "") if isinstance(tag, dict) else str(tag)
                for tag in tags
            )
            descriptor = f"{title} {tag_text}"
            score = max(
                image_token_overlap(query, descriptor),
                image_token_overlap(original, descriptor) * 0.65,
            )
            candidate = usable_image_url(str(result.get("thumbnail") or result.get("url") or ""))
            if candidate and not image_url_is_generic(candidate) and score >= 0.18 and score > best_score:
                best_url, best_score = candidate, score
                creator = str(result.get("creator") or "Autor desconocido")
                license_name = str(result.get("license") or "licencia abierta")
                landing = str(result.get("foreign_landing_url") or "")
                best_credit = f"{creator} · {license_name}" + (f" · {landing}" if landing else "")
                best_descriptor = descriptor
        if best_url:
            article.setdefault("_image_candidate_descriptors", {})[best_url] = best_descriptor
        return best_url, best_credit
    except Exception as exc:
        print(f"Openverse no disponible ({type(exc).__name__}: {str(exc)[:100]}).")
        return "", ""


def fallback_image_query(article: dict[str, Any]) -> str:
    """Build a compact Commons query without inventing context."""
    title = str(article.get("original_headline") or article.get("headline") or "")
    words = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9]{3,}", title)
    ignored = {
        "the", "and", "for", "with", "from", "that", "this", "una", "uno",
        "los", "las", "del", "por", "para", "con", "sobre", "entre", "como",
        "este", "esta", "estos", "estas", "según", "tras", "desde", "hacia",
        "entre", "sobre", "against", "over", "after", "before", "into",
    }
    selected = []
    for word in words:
        if word.lower() not in ignored and word.lower() not in {v.lower() for v in selected}:
            selected.append(word)
        if len(selected) >= 9:
            break
    return " ".join(selected)


def fallback_visual_queries(article: dict[str, Any]) -> list[str]:
    """Build several concrete, per-story photo searches without inventing an event."""
    text = " ".join((
        str(article.get("original_headline") or article.get("headline") or ""),
        str(article.get("summary") or ""),
    )).lower()
    queries: list[str] = []

    if any(term in text for term in ("data center", "data centre", "centro de datos", "centros de datos")):
        queries = [
            "data center server room server racks",
            "data centre cooling infrastructure servers",
            "data center construction facility exterior",
        ]
    elif any(term in text for term in ("hermandad", "cofrad", "procesión", "procession", "holy week", "semana santa")):
        queries = [
            "Malaga Holy Week procession nazarenos",
            "Semana Santa religious brotherhood procession Spain",
            "cofradia Malaga pasos procession street",
        ]
    elif any(term in text for term in ("pyme", "pymes", "small business", "small and medium", "emprend")):
        queries = [
            "small business owners workshop Spain",
            "small entrepreneurs local business office",
            "Basque Country small manufacturing company workshop",
        ]
    elif any(term in text for term in ("adamuz", "accidente ferrovi", "accident train", "railway accident", "ferrovi", "tren", "railway safety")):
        queries = [
            "Adamuz Spain railway accident train tracks",
            "railway safety train accident investigation Spain",
            "Spanish railway tracks high speed train",
        ]
    elif any(term in text for term in ("pesquer", "fish", "flota", "fishing")):
        queries = [
            "Spanish fishing fleet boats harbor",
            "fishing boats port fish market Spain",
            "fishermen fishing vessel deck Spain",
        ]
    elif any(term in text for term in ("pension", "jubil", "retiree", "retirement", "pensionista", "pensionistas")):
        queries = [
            "pensioners elderly retirement Spain",
            "elderly pensioners demonstration Spain",
            "Spanish social security pension office",
        ]
    elif any(term in text for term in ("franc suizo", "swiss franc", "franco suizo", "currency exchange", "divisa", "divisas", "exchange rate")):
        queries = [
            "Swiss franc banknotes currency exchange",
            "Swiss National Bank headquarters Zurich",
            "Swiss franc currency notes coins",
        ]
    elif any(term in text for term in ("sindicato", "sindicatos", "union", "trade union", "workers", "votar", "vote", "elección", "elecciones", "ballot")):
        queries = [
            "Spanish trade union workers demonstration",
            "Spain election ballot voting polling station",
            "Spanish trade unions CCOO UGT protest",
        ]
    elif any(term in text for term in ("pradales", "lehendakari", "euskadi", "gipuzkoa", "país vasco", "pais vasco", "basque")):
        queries = [
            "Basque Government Lehendakaritza Vitoria Gasteiz",
            "Basque Country industrial factories manufacturing Euskadi",
            "Basque Parliament Vitoria Gasteiz exterior",
        ]
    elif any(term in text for term in ("banco central", "central bank", "mercados", "markets", "bolsa", "stocks", "equity rout")):
        queries = [
            "European stock exchange trading floor financial markets",
            "central bank headquarters monetary policy",
            "financial district stock market traders",
        ]
    elif any(term in text for term in ("parlamento", "congreso", "government", "gobierno", "comisión europea", "european commission")):
        queries = [
            "Spanish Congress of Deputies hemicycle Madrid",
            "European Commission Berlaymont building Brussels",
            "Spanish government Moncloa palace Madrid",
        ]
    elif any(term in text for term in ("laboratorio", "neurociencia", "cerebro", "research", "investigación", "scientific", "ciencia")):
        queries = [
            "scientific research laboratory researchers",
            "neuroscience brain research laboratory EEG",
            "scientists laboratory microscopy experiment",
        ]
    else:
        section = str(article.get("section") or "")
        if section == "España y Gipuzkoa":
            queries = [
                "Basque Parliament Vitoria Gasteiz",
                "Donostia San Sebastian city hall Spain",
                "Basque Country industrial manufacturing facility",
            ]
        elif section == "Economía y geopolítica":
            queries = [
                "European financial district stock exchange traders",
                "container port logistics industrial shipping",
                "European central bank headquarters",
            ]
        elif section == "Cultura, sociedad y descubrimientos":
            queries = [
                "Spanish museum cultural heritage exhibition",
                "archaeological excavation Spain historical site",
                "library museum historical archive Spain",
            ]
        elif section == "Ciencia, investigación y tecnología":
            queries = [
                "scientific laboratory researchers Spain",
                "medical research laboratory microscope",
                "university science research facility",
            ]
        else:
            queries = [
                "public institution building Spain",
                "industrial or public infrastructure Spain",
            ]

    unique = []
    for query in queries:
        query = re.sub(r"\s+", " ", query).strip()
        if query and query not in unique:
            unique.append(query)
    return unique[:3]


def fallback_visual_query(article: dict[str, Any]) -> str:
    """Backward-compatible first thematic query for the article."""
    queries = fallback_visual_queries(article)
    return queries[0] if queries else "public institution building Spain"



def generate_commons_queries(
    articles: list[dict[str, Any]], api_key: str = ""
) -> dict[str, list[str]]:
    """Use Gemini to turn long headlines into precise, visual Commons search phrases."""
    queries = {
        str(article.get("id") or article.get("url")): list(dict.fromkeys(
            [fallback_image_query(article)] + fallback_visual_queries(article)
        ))[:3]
        for article in articles
    }
    if not api_key or not articles:
        return queries

    records = [{
        "id": str(article.get("id") or article.get("url")),
        "headline": str(article.get("original_headline") or article.get("headline") or ""),
        "summary": str(article.get("summary") or "")[:240],
        "section": str(article.get("section") or ""),
    } for article in articles]
    prompt = f"""
You prepare search queries for real photographs on Wikimedia Commons.
For every news story below, create exactly 2 short, concrete image-search phrases.
The first must target the distinctive person/place/institution/event. The second
must target a plausible visible scene or physical subject (for example server racks,
fishing boats in port, a parliamentary chamber, a railway track or a laboratory).
Use named people, places, institutions, objects, events and visible scenes from
the story. Prefer phrases likely to appear in a Commons file title or description.
Translate abstract policy/economic language into the real-world subject that a
photograph would depict, without inventing a scene or adding unrelated context.
Keep important proper names. Queries should be 4-9 words, concise, and can be in
English because Commons metadata is often in English. Do not return generic words
such as "news", "business", "politics", "technology" or "people" on their own.
Return JSON only with exactly two distinct queries per story:
{{"queries":[{{"id":"N001","queries":["specific phrase one","specific phrase two"]}}]}}

STORIES:
{json.dumps(records, ensure_ascii=False)}
"""
    try:
        raw = gemini_request(prompt, api_key, retry_delays=(2,))
        data = json.loads(raw)
        for item in data.get("queries", []):
            story_id = str(item.get("id") or "")
            values = item.get("queries") or []
            if story_id not in queries or not isinstance(values, list):
                continue
            cleaned = []
            for value in values:
                phrase = re.sub(r"\s+", " ", str(value)).strip()[:120]
                if len(phrase) >= 8 and phrase not in cleaned:
                    cleaned.append(phrase)
                if len(cleaned) >= 2:
                    break
            if cleaned:
                story = next(
                    article for article in articles
                    if str(article.get("id") or article.get("url")) == story_id
                )
                for visual_fallback in fallback_visual_queries(story):
                    if visual_fallback not in cleaned:
                        cleaned.append(visual_fallback)
                    if len(cleaned) >= 3:
                        break
                queries[story_id] = cleaned[:3]
        print(f"Gemini: consultas visuales preparadas para {sum(bool(v) for v in queries.values())}/{len(articles)} noticias.")
        for article in articles:
            story_id = str(article.get("id") or article.get("url"))
            print(f"Consulta visual {story_id}: {queries.get(story_id, [])}")
    except Exception as exc:
        print(f"Gemini no pudo afinar las consultas de imágenes ({type(exc).__name__}); se usan titulares.")
    return queries


def fetch_commons_candidates(
    article: dict[str, Any], queries: list[str] | None = None
) -> list[tuple[str, str, str, str]]:
    """Search Wikimedia Commons and retain useful metadata for relevance ranking."""
    original = str(article.get("original_headline") or article.get("headline") or "").strip()
    search_queries = [q for q in (queries or []) if len(q.strip()) >= 6]
    fallback = fallback_image_query(article)
    if fallback and fallback not in search_queries:
        search_queries.append(fallback)
    if not search_queries:
        return []

    article_id = str(article.get("id") or article.get("url") or "")
    best: dict[str, tuple[float, str, str, str, str]] = {}
    descriptors = article.setdefault("_image_candidate_descriptors", {})

    for query in search_queries[:3]:
        params = urllib.parse.urlencode({
            "action": "query",
            "generator": "search",
            "gsrsearch": query[:160],
            "gsrnamespace": "6",
            "gsrlimit": "25",
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata",
            "iiurlwidth": "1200",
            "format": "json",
        })
        request = urllib.request.Request(
            "https://commons.wikimedia.org/w/api.php?" + params,
            headers={
                "User-Agent": USER_AGENT + " (news-photo-retrieval; contact: project repository)",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=18) as response:
                pages = json.loads(response.read().decode("utf-8", errors="replace")).get("query", {}).get("pages", {})
        except HTTPError as exc:
            if exc.code == 429:
                print(f"Commons HTTP 429 para {article_id}; se omite esa consulta.")
            else:
                print(f"Commons no disponible ({exc.code}) para {article_id}.")
            continue
        except Exception as exc:
            print(f"Commons no disponible ({type(exc).__name__}) para {article_id}.")
            continue

        for page in pages.values():
            info_list = page.get("imageinfo") or []
            if not info_list:
                continue
            info = info_list[0]
            mime = str(info.get("mime") or "")
            width = int(info.get("width") or 0)
            height = int(info.get("height") or 0)
            if not mime.startswith("image/") or width < 600 or height < 350:
                continue
            ratio = width / max(1, height)
            if ratio < 0.48 or ratio > 2.8:
                continue

            meta = info.get("extmetadata") or {}
            object_name = clean_html(str((meta.get("ObjectName") or {}).get("value") or ""))
            description = clean_html(str((meta.get("ImageDescription") or {}).get("value") or ""))
            title = str(page.get("title") or "").replace("File:", "").replace("_", " ")
            descriptor = " ".join((title, object_name, description[:1800])).strip()
            score = max(
                image_token_overlap(query, descriptor) for query in search_queries[:3]
            )
            headline_score = image_token_overlap(original, descriptor)
            score = max(score, headline_score * 0.78)

            query_words = {
                word for word in re.findall(r"[a-záéíóúüñ0-9]{3,}", query.lower())
                if word not in {"the", "and", "for", "with", "from", "that", "this",
                                "una", "uno", "los", "las", "del", "por", "para",
                                "con", "sobre", "entre", "news", "photo", "image"}
            }
            descriptor_words = set(re.findall(
                r"[a-záéíóúüñ0-9]{3,}", descriptor.lower()
            ))
            matched_words = query_words & descriptor_words
            required_matches = 2 if len(query_words) >= 3 else 1
            if score < 0.24 or len(matched_words) < required_matches:
                continue

            candidate = usable_image_url(str(info.get("thumburl") or info.get("url") or ""))
            if not candidate or image_url_is_generic(candidate):
                continue
            descriptors[candidate] = descriptor

            artist = clean_html(str((meta.get("Artist") or {}).get("value") or ""))
            license_name = clean_html(str((meta.get("LicenseShortName") or {}).get("value") or ""))
            credit = " · ".join(
                part for part in (artist, license_name, "Wikimedia Commons") if part
            )
            page_title = str(page.get("title") or "")
            landing = "https://commons.wikimedia.org/wiki/" + urllib.parse.quote(page_title.replace(" ", "_"), safe=":_")
            current = best.get(candidate)
            record = (score, candidate, landing, credit, descriptor[:220])
            if current is None or score > current[0]:
                best[candidate] = record

    ranked = sorted(best.values(), key=lambda item: item[0], reverse=True)[:6]
    return [
        (image_url, "commons", landing_url, credit)
        for _score, image_url, landing_url, credit, _description in ranked
    ]



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
        best_url, best_score, best_title = "", 0.0, ""
        for result in payload.get("results", []):
            title = str(result.get("title") or "")
            score = image_text_score(original, title)
            props = result.get("properties") or {}
            thumb = result.get("thumbnail") or {}
            candidate = usable_image_url(str(thumb.get("src") or props.get("url") or ""))
            width = props.get("width") or 0
            height = props.get("height") or 0
            if candidate and not image_url_is_generic(candidate) and score >= 0.44:
                if width and height and (width < 350 or height < 180):
                    continue
                if score > best_score:
                    best_url, best_score, best_title = candidate, score, title
        if best_url:
            article.setdefault("_image_candidate_descriptors", {})[best_url] = best_title
        return best_url
    except Exception as exc:
        print(f"Brave Image Search no disponible ({type(exc).__name__}: {str(exc)[:100]}).")
        return ""


def inspect_photo_quality(image: Any) -> tuple[bool, str]:
    """Reject blank pixels, tiny thumbnails and low-information placeholders."""
    from PIL import ImageStat

    width, height = image.size
    if width < 320 or height < 200 or width * height < 100_000:
        return False, f"resolución insuficiente ({width}x{height})"
    ratio = width / max(1, height)
    if ratio < 0.48 or ratio > 2.8:
        return False, f"proporción no fotográfica ({width}x{height})"

    sample = image.copy()
    sample.thumbnail((72, 72))
    gray = sample.convert("L")
    stats = ImageStat.Stat(gray)
    deviation = stats.stddev[0]
    histogram = gray.histogram()
    total = max(1, sum(histogram))
    entropy = -sum(
        (count / total) * math.log2(count / total)
        for count in histogram if count
    )
    colors = len(sample.getcolors(maxcolors=6000) or [])
    if deviation < 6.0 or entropy < 2.2 or colors < 18:
        return False, (
            f"imagen vacía/placeholder (desviación={deviation:.1f}, "
            f"entropía={entropy:.2f}, colores={colors})"
        )
    return True, f"{width}x{height}; entropía={entropy:.1f}; colores={colors}"


def download_inline_image(image_url: str, referer: str = "") -> tuple[bytes, str] | None:
    """Download, validate visual content and normalize images to email-safe JPEG."""
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
    if referer:
        headers["Referer"] = referer
    request = urllib.request.Request(image_url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            data = response.read(8_000_001)
        if not data or len(data) > 8_000_000 or len(data) < 1_500:
            print(f"Imagen rechazada por tamaño ({len(data)} bytes): {urllib.parse.urlparse(image_url).netloc}.")
            return None

        try:
            from PIL import Image, ImageOps
            try:
                import pillow_avif  # noqa: F401 - registers AVIF support with Pillow
            except ImportError:
                pass
            with Image.open(BytesIO(data)) as opened:
                opened.seek(0)
                oriented = ImageOps.exif_transpose(opened)
                if "A" in oriented.getbands():
                    rgba = oriented.convert("RGBA")
                    background = Image.new("RGBA", rgba.size, "white")
                    background.alpha_composite(rgba)
                    image = background.convert("RGB")
                else:
                    image = oriented.convert("RGB")

                valid, quality_note = inspect_photo_quality(image)
                if not valid:
                    print(
                        f"Fotografía descartada ({quality_note}): "
                        f"{urllib.parse.urlparse(image_url).netloc}."
                    )
                    return None

                image.thumbnail((1400, 1000), Image.Resampling.LANCZOS)
                output = BytesIO()
                image.save(output, format="JPEG", quality=84, optimize=True, progressive=True)
                converted = output.getvalue()
            if len(converted) > 950_000:
                output = BytesIO()
                image.save(output, format="JPEG", quality=76, optimize=True)
                converted = output.getvalue()
            if len(converted) > 1_200_000:
                return None
            return converted, "jpeg"
        except ImportError:
            if content_type in {"image/jpeg", "image/png"}:
                return data, content_type.split("/", 1)[1]
            return None
        except Exception as exc:
            print(f"Formato de imagen no legible ({content_type or 'sin MIME'}; "
                  f"{type(exc).__name__}): {urllib.parse.urlparse(image_url).netloc}.")
            return None
    except Exception as exc:
        print(f"No se pudo descargar imagen ({urllib.parse.urlparse(image_url).netloc}: {type(exc).__name__}).")
        return None

def image_candidate_relevance_score(
    article: dict[str, Any], candidate: tuple[str, str, str, str]
) -> float:
    """Estimate story relevance from image metadata, query match and source provenance."""
    image_url, source, _referer, credit = candidate[:4]
    descriptor = str(article.get("_image_candidate_descriptors", {}).get(image_url, ""))
    if not descriptor:
        descriptor = str(credit or "")

    headline = str(article.get("original_headline") or article.get("headline") or "")
    summary = str(article.get("summary") or "")
    queries = article.get("_image_search_queries") or []
    if isinstance(queries, str):
        queries = [queries]
    if not queries:
        fallback = str(article.get("image_search_query") or fallback_image_query(article))
        queries = [fallback] if fallback else []

    query_score = max(
        (image_token_overlap(str(query), descriptor) for query in queries if descriptor),
        default=0.0,
    )
    headline_score = image_token_overlap(headline, descriptor) if descriptor else 0.0
    summary_score = image_token_overlap(summary, descriptor) if descriptor and summary else 0.0
    provenance = {
        "publisher": 0.30, "rss": 0.28, "gdelt": 0.15,
        "brave": 0.08, "openverse": 0.04, "commons": 0.0,
    }.get(source, 0.0)
    if source in {"publisher", "rss"} and not descriptor:
        headline_score = 0.45

    score = 0.36 * query_score + 0.39 * headline_score + 0.15 * summary_score + provenance
    return round(min(1.0, score), 4)


def gemini_visual_rank(
    article: dict[str, Any],
    downloaded: list[tuple[tuple[str, str, str, str], bytes, float]],
    api_key: str,
) -> dict[int, dict[str, Any]]:
    """Ask the configured multimodal Gemini model whether each photo depicts this story."""
    global _GEMINI_VISION_DISABLED
    if not api_key or not downloaded or _GEMINI_VISION_DISABLED:
        return {}

    headline = str(article.get("headline") or article.get("original_headline") or "")
    summary = str(article.get("summary") or "")
    parts: list[dict[str, Any]] = [{
        "text": (
            "Evalúa la relación VISUAL de cada fotografía con esta noticia. "
            "No juzgues solo por la similitud de palabras: mira lo que aparece realmente. "
            "Titular: " + headline[:500] + "\nResumen: " + summary[:900] + "\n\n"
            "Para cada imagen devuelve: index (empezando en 1), relevance (0-100), "
            "relation (direct, related, illustrative o unrelated) y reason (máximo 18 palabras). "
            "direct = muestra al protagonista, lugar, institución, empresa o acontecimiento concreto. "
            "related = representa claramente el objeto o actividad específica de la noticia. "
            "illustrative = solo representa el tema general, sin conexión concreta con esta noticia. "
            "unrelated = no guarda una relación visual útil. "
            "No supongas identidades ni lugares que no puedas reconocer en la imagen. "
            "Sé estricto: una foto genérica de políticos, edificios, ordenadores o mercados "
            "no es direct ni related solo por pertenecer a esa categoría. "
            "Devuelve JSON con esta estructura: "
            '{"images":[{"index":1,"relevance":90,"relation":"direct","reason":"..."}]}'
        )
    }]
    for index, (_candidate, data, _heuristic_score) in enumerate(downloaded, 1):
        parts.append({"text": f"Fotografía candidata {index}:"})
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(data).decode("ascii"),
            }
        })

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": 0.1,
            "responseMimeType": "application/json",
            "maxOutputTokens": 1800,
        },
    }
    request = urllib.request.Request(
        GEMINI_ENDPOINT + "?key=" + urllib.parse.quote(api_key),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=65) as response:
            result = json.loads(response.read().decode("utf-8"))
        raw = result["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(raw)
        ranked: dict[int, dict[str, Any]] = {}
        for item in parsed.get("images", []):
            try:
                index = int(item.get("index"))
                relevance = max(0, min(100, int(float(item.get("relevance", 0)))))
            except (TypeError, ValueError):
                continue
            relation = str(item.get("relation") or "unrelated").strip().lower()
            aliases = {
                "directa": "direct", "directo": "direct",
                "relacionada": "related", "relacionado": "related",
                "ilustrativa": "illustrative", "ilustrativo": "illustrative",
                "no relacionada": "unrelated", "no relacionado": "unrelated",
            }
            relation = aliases.get(relation, relation)
            if relation not in {"direct", "related", "illustrative", "unrelated"}:
                relation = "unrelated"
            ranked[index] = {
                "relevance": relevance, "relation": relation,
                "reason": str(item.get("reason") or "")[:180],
            }
        for index, value in ranked.items():
            print(
                f"Gemini visión {article.get('id', '')} candidata {index}: "
                f"{value['relation']} ({value['relevance']}/100)."
            )
        return ranked
    except HTTPError as exc:
        _GEMINI_VISION_DISABLED = True
        print(f"Gemini visión no disponible (HTTP {exc.code}); se usará la puntuación local.")
        return {}
    except Exception as exc:
        _GEMINI_VISION_DISABLED = True
        print(f"Gemini visión no disponible ({type(exc).__name__}); se usará la puntuación local.")
        return {}


def download_ranked_candidates(
    article: dict[str, Any],
    candidates: list[tuple[str, str, str, str]],
    api_key: str = "",
) -> tuple[bytes, str, str, str, str] | None:
    """Download several top candidates, compare relevance, and embed the best match."""
    unique: dict[str, tuple[float, tuple[str, str, str, str]]] = {}
    for candidate in candidates:
        if not candidate or len(candidate) < 4:
            continue
        image_url = candidate[0]
        if not image_url:
            continue
        score = image_candidate_relevance_score(article, candidate)
        current = unique.get(image_url)
        if current is None or score > current[0]:
            unique[image_url] = (score, candidate)

    ranked = sorted(unique.values(), key=lambda item: item[0], reverse=True)
    if not ranked:
        return None

    if all("illustrative" in item[1][1] for item in ranked):
        for heuristic_score, candidate in ranked:
            image_url, source, referer, credit = candidate[:4]
            result = download_inline_image(image_url, referer)
            if result:
                data, _subtype = result
                article["image_relevance_score"] = round(heuristic_score, 3)
                article["image_match_type"] = "illustrative"
                print(
                    f"Imagen ilustrativa elegida {article.get('id', '')}: "
                    f"relevancia temática={heuristic_score:.2f}."
                )
                return data, "jpeg", image_url, source, credit
        return None

    downloaded: list[tuple[tuple[str, str, str, str], bytes, float]] = []
    for heuristic_score, candidate in ranked[:4]:
        if heuristic_score < 0.12:
            continue
        image_url, _source, referer, _credit = candidate[:4]
        result = download_inline_image(image_url, referer)
        if result:
            data, _subtype = result
            downloaded.append((candidate, data, heuristic_score))
    if not downloaded:
        return None

    visual_scores = gemini_visual_rank(article, downloaded, api_key)
    choices: list[tuple[float, tuple[bytes, str, str, str, str], str]] = []
    if visual_scores:
        for index, (candidate, data, heuristic_score) in enumerate(downloaded, 1):
            visual = visual_scores.get(index)
            if not visual:
                continue
            relation = visual["relation"]
            relevance = visual["relevance"]
            if relation not in {"direct", "related"} or relevance < 55:
                continue
            combined = 0.32 * heuristic_score + 0.68 * (relevance / 100.0)
            url, source, _referer, credit = candidate[:4]
            choices.append((combined, (data, "jpeg", url, source, credit), relation))
    else:
        # During a Gemini outage, choose the strongest metadata match, not the first
        # downloadable URL. Exact publisher/RSS provenance remains a useful signal.
        for candidate, data, heuristic_score in downloaded:
            if heuristic_score >= 0.22:
                url, source, _referer, credit = candidate[:4]
                choices.append((
                    heuristic_score, (data, "jpeg", url, source, credit), "metadata"
                ))

    if not choices:
        return None
    choices.sort(key=lambda item: item[0], reverse=True)
    final_score, result, relation = choices[0]
    article["image_relevance_score"] = round(final_score, 3)
    article["image_match_type"] = relation
    print(
        f"Imagen elegida {article.get('id', '')}: fuente={result[3]}, "
        f"relevancia={final_score:.2f}, criterio={relation}."
    )
    return result


def download_first_available(candidates: list[tuple[str, str, str, str]]) -> tuple[bytes, str, str, str, str] | None:
    """Try each fallback candidate in priority order and return the first embeddable image."""
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or len(candidate) < 4:
            continue
        image_url, source, referer, credit = candidate[:4]
        if not image_url or image_url in seen:
            continue
        seen.add(image_url)
        result = download_inline_image(image_url, referer)
        if result:
            data, subtype = result
            return data, subtype, image_url, source, credit
    return None


def embed_candidates(
    articles: list[dict[str, Any]],
    candidates: dict[int, list[tuple[str, str, str, str]]],
    api_key: str = "",
) -> None:
    """Download, rank and embed the most relevant valid candidate per story."""
    pending = [article for article in articles if not article.get("_image_data")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(
                download_ranked_candidates, article, candidates.get(id(article), []), api_key
            ): (index, article)
            for index, article in enumerate(articles, 1) if article in pending
        }
        for future in as_completed(futures):
            index, article = futures[future]
            try:
                result = future.result()
                if result:
                    data, subtype, image_url, image_source, credit = result
                    article["image_url"] = image_url
                    article["image_source"] = image_source
                    if credit:
                        article["image_credit"] = credit
                    article["_image_data"] = data
                    article["_image_subtype"] = subtype
                    article["_image_cid"] = f"news-image-{index}@resumen-diario"
            except Exception as exc:
                print(f"Error descargando fotografía ({type(exc).__name__}).")



def prepare_article_images(editorial: dict[str, Any], api_key: str = "") -> None:
    """Discover multiple sources in parallel, rank them, then use labelled fallbacks."""
    global _GEMINI_VISION_DISABLED
    articles = editorial.get("articles", [])
    if not articles:
        return
    _GEMINI_VISION_DISABLED = False
    api_key = api_key or os.environ.get("GEMINI_API_KEY", "").strip()
    candidates: dict[int, list[tuple[str, str, str, str]]] = {id(a): [] for a in articles}
    ids = {id(article): str(article.get("id") or article.get("url")) for article in articles}
    for article in articles:
        article["_image_candidate_descriptors"] = {}

    queries_by_id = generate_commons_queries(articles, api_key)
    for article in articles:
        queries = queries_by_id.get(ids[id(article)], [fallback_image_query(article)])
        article["_image_search_queries"] = queries
        article["image_search_query"] = queries[0] if queries else fallback_image_query(article)

    # Search Commons and the publisher's page for every story in parallel before selecting.
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures: dict[Any, tuple[dict[str, Any], str]] = {}
        for article in articles:
            futures[pool.submit(
                fetch_commons_candidates, article, queries_by_id.get(ids[id(article)], [])
            )] = (article, "commons")
            futures[pool.submit(fetch_article_images, article)] = (article, "publisher")
        for future in as_completed(futures):
            article, source_type = futures[future]
            try:
                result = future.result()
                if source_type == "commons":
                    candidates[id(article)].extend(result)
                else:
                    image_urls, publisher_url = result
                    for image_url in image_urls:
                        candidates[id(article)].append((
                            image_url, "publisher",
                            publisher_url or str(article.get("url") or ""), "",
                        ))
            except Exception as exc:
                print(f"Búsqueda inicial {source_type} falló ({type(exc).__name__}).")

    # RSS imagery participates in the same ranking instead of short-circuiting other sources.
    for article in articles:
        rss_candidate = usable_image_url(str(article.get("rss_image_url") or ""))
        if rss_candidate and not image_url_is_generic(rss_candidate):
            candidates[id(article)].append((
                rss_candidate, "rss", str(article.get("url") or ""), ""
            ))
            # RSS provenance is evidence of article association, not proof of image content.

    commons_count = sum(
        any(candidate[1] == "commons" for candidate in candidates[id(article)])
        for article in articles
    )
    publisher_count = sum(
        any(candidate[1] == "publisher" for candidate in candidates[id(article)])
        for article in articles
    )
    print(
        f"Búsqueda paralela: Commons={commons_count}/{len(articles)}, "
        f"página del medio={publisher_count}/{len(articles)}."
    )
    embed_candidates(articles, candidates, api_key)
    print(
        f"Tras comparar Commons/medios/RSS: "
        f"{sum(bool(a.get('_image_data')) for a in articles)}/{len(articles)}."
    )

    # Query external news indexes only for stories without a direct or related match.
    missing = [article for article in articles if not article.get("_image_data")]
    gdelt_map = fetch_gdelt_images(missing)
    for article in missing:
        candidate = gdelt_map.get(str(article.get("id") or article.get("url")), "")
        if candidate:
            article["_image_candidate_descriptors"][candidate] = (
                str(article.get("original_headline") or article.get("headline") or "")
                + " image returned by a news article index"
            )
            candidates[id(article)] = [(
                candidate, "gdelt", str(article.get("url") or ""), ""
            )]
    embed_candidates(missing, candidates, api_key)
    print(f"Tras GDELT: {sum(bool(a.get('_image_data')) for a in articles)}/{len(articles)}.")

    missing = [article for article in articles if not article.get("_image_data")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_openverse_image, article): article for article in missing}
        for future in as_completed(futures):
            article = futures[future]
            try:
                url, credit = future.result()
                if url:
                    candidates[id(article)] = [(
                        url, "openverse", str(article.get("url") or ""), credit
                    )]
            except Exception:
                pass
    embed_candidates(missing, candidates, api_key)
    print(f"Tras Openverse: {sum(bool(a.get('_image_data')) for a in articles)}/{len(articles)}.")

    brave_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if brave_key:
        missing = [article for article in articles if not article.get("_image_data")]
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(fetch_brave_image, article, brave_key): article for article in missing}
            for future in as_completed(futures):
                article = futures[future]
                try:
                    url = future.result()
                    if url:
                        candidates[id(article)] = [(
                            url, "brave", str(article.get("url") or ""), ""
                        )]
                except Exception:
                    pass
        embed_candidates(missing, candidates, api_key)

    # Theme-level images are allowed only after all story-specific sources fail.
    # Every fallback search remains tied to this individual story; no generic photo is
    # reused across unrelated articles.
    missing = [article for article in articles if not article.get("_image_data")]
    if missing:
        for article in missing:
            candidates[id(article)] = []
            theme_queries = fallback_visual_queries(article)
            article["_image_search_queries"] = theme_queries
            article["image_search_query"] = theme_queries[0] if theme_queries else ""
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                pool.submit(
                    fetch_commons_candidates,
                    article,
                    fallback_visual_queries(article),
                ): article
                for article in missing
            }
            for future in as_completed(futures):
                article = futures[future]
                try:
                    results = future.result()
                    for image_url, _source, landing, credit in results:
                        candidates[id(article)].append((
                            image_url,
                            "commons-illustrative",
                            landing,
                            (credit + " · imagen ilustrativa").strip(" ·"),
                        ))
                except Exception as exc:
                    print(f"Commons ilustrativo falló ({type(exc).__name__}).")
        embed_candidates(missing, candidates, api_key)
        print(
            f"Tras Wikimedia Commons con consultas temáticas específicas: "
            f"{sum(bool(a.get('_image_data')) for a in articles)}/{len(articles)}."
        )

    attached = sum(1 for article in articles if article.get("_image_data"))
    source_counts: dict[str, int] = {}
    for article in articles:
        if article.get("_image_data"):
            origin = str(article.get("image_source") or "desconocido")
            source_counts[origin] = source_counts.get(origin, 0) + 1
    print(
        f"IMÁGENES FINAL: {attached}/{len(articles)}. "
        f"Fuentes={source_counts}. Sin fotografía válida={len(articles) - attached}."
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
    prepare_article_images(editorial, api_key)
    message = build_email(editorial, markets, movers_up, movers_down, ai_used, ai_error)
    send_email(message, address, recipient, app_password)
    print("Correo enviado correctamente.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())