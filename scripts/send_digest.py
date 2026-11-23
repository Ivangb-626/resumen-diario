#!/usr/bin/env python3
"""Build and email a daily Spanish news digest from Google News RSS feeds.

This version does not call a paid AI API. It selects recent stories and uses
the summaries/snippets supplied by the RSS feed; it does not invent analysis.
"""

from __future__ import annotations

import email.utils
import html
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Optional
from zoneinfo import ZoneInfo


BERLIN = ZoneInfo("Europe/Berlin")
MAX_STORIES = 5
MAX_AGE = timedelta(days=7)
USER_AGENT = "resumen-diario/1.0 (GitHub Actions RSS reader)"

TOPICS = [
    ("IA y tecnología", "inteligencia artificial tecnología modelos IA", 8),
    ("Prácticas y empleo tecnológico", "prácticas empleo software engineering España Europa 2027", 8),
    ("Ingeniería del software", "ingeniería del software desarrollo web programación TypeScript Python", 7),
    ("Mercados e inversión", "fondos indexados ETF inversión mercados España", 7),
    ("TFG: IA, EEG y epilepsia", "inteligencia artificial EEG epilepsia electroencefalograma", 7),
    ("España y Alemania", "noticias importantes España Alemania política economía sociedad", 7),
    ("Ajedrez", "ajedrez torneos Gipuzkoa España", 3),
    ("Ruso", "idioma ruso aprendizaje ruso enseñanza", 2),
]


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if value:
            self.parts.append(value)


def clean_html(value: str) -> str:
    parser = TextExtractor()
    try:
        parser.feed(value or "")
        text = " ".join(parser.parts)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


@dataclass
class Story:
    topic: str
    priority: int
    title: str
    link: str
    source: str
    summary: str
    published: datetime


def parse_date(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def fetch_topic(topic: tuple[str, str, int]) -> list[Story]:
    label, query, priority = topic
    params = urllib.parse.urlencode({
        "q": f"{query} when:7d",
        "hl": "es",
        "gl": "ES",
        "ceid": "ES:es",
    })
    url = "https://news.google.com/rss/search?" + params
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=25) as response:
        payload = response.read()

    root = ET.fromstring(payload)
    channel = root.find("channel")
    if channel is None:
        return []

    stories: list[Story] = []
    now = datetime.now(timezone.utc)
    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue

        published = parse_date(item.findtext("pubDate") or "")
        if published is None:
            continue
        if now - published > MAX_AGE or published > now + timedelta(hours=2):
            continue

        source_node = item.find("source")
        source = (source_node.text or "").strip() if source_node is not None else "Google News"
        summary = clean_html(item.findtext("description") or "")
        # Google News' RSS description often repeats the headline and publisher.
        if summary.lower().startswith(title.lower()):
            summary = summary[len(title):].lstrip(" -–:|")
        if source and summary.lower().endswith(source.lower()):
            summary = summary[: -len(source)].strip(" -–:|")

        stories.append(Story(
            topic=label,
            priority=priority,
            title=title,
            link=link,
            source=source or "Google News",
            summary=summary[:420],
            published=published,
        ))
    return stories


def collect_stories() -> list[Story]:
    candidates: list[Story] = []
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_topic, topic): topic[0] for topic in TOPICS}
        for future in as_completed(futures):
            label = futures[future]
            try:
                candidates.extend(future.result())
            except Exception as exc:
                errors.append(f"{label}: {type(exc).__name__}: {exc}")

    # Deduplicate headlines while keeping the strongest topic classification.
    unique: dict[str, Story] = {}
    for story in candidates:
        key = re.sub(r"[^a-z0-9áéíóúüñ]+", "", story.title.lower())
        existing = unique.get(key)
        if existing is None or story.priority > existing.priority:
            unique[key] = story

    now = datetime.now(timezone.utc)
    ranked = list(unique.values())
    ranked.sort(
        key=lambda s: (
            s.priority + max(0, 72 - (now - s.published).total_seconds() / 3600) / 24,
            s.published,
        ),
        reverse=True,
    )

    # Prefer breadth: at most two stories from any one topic.
    selected: list[Story] = []
    per_topic: dict[str, int] = {}
    for story in ranked:
        if per_topic.get(story.topic, 0) >= 2:
            continue
        selected.append(story)
        per_topic[story.topic] = per_topic.get(story.topic, 0) + 1
        if len(selected) == MAX_STORIES:
            break

    if not selected:
        detail = "; ".join(errors[:3]) if errors else "los feeds no devolvieron noticias recientes"
        raise RuntimeError(f"No se han podido recopilar noticias. Detalle: {detail}")
    return selected


def build_email(stories: list[Story]) -> EmailMessage:
    now = datetime.now(BERLIN)
    date_label = now.strftime("%A, %d de %B de %Y")
    date_label = date_label.capitalize()

    msg = EmailMessage()
    msg["Subject"] = f"Tu resumen diario — {now.strftime('%d/%m/%Y')}"
    msg.set_content(
        "Tu resumen diario\\n"
        f"{date_label}\\n\\n"
        + "\\n\\n".join(
            f"{i}. {s.title}\\n{s.topic} · {s.source}\\n"
            f"{s.summary or 'Lee la noticia completa en la fuente original.'}\\n{s.link}"
            for i, s in enumerate(stories, 1)
        )
        + "\\n\\nSelección automática de titulares y extractos de fuentes RSS. "
          "No se ha generado análisis editorial mediante IA."
    )

    cards = []
    for index, story in enumerate(stories, 1):
        summary = story.summary or "Lee la noticia completa en la fuente original."
        local_time = story.published.astimezone(BERLIN).strftime("%d/%m/%Y %H:%M")
        cards.append(f"""
        <article style="padding:18px 0;border-bottom:1px solid #e5e7eb">
          <div style="font-size:12px;color:#64748b;margin-bottom:7px">
            {html.escape(story.topic)} · {html.escape(story.source)} · {local_time} (hora alemana)
          </div>
          <h2 style="font-size:18px;line-height:1.35;margin:0 0 8px">
            <a href="{html.escape(story.link, quote=True)}" style="color:#1d4ed8;text-decoration:none">
              {index}. {html.escape(story.title)}
            </a>
          </h2>
          <p style="font-size:14px;line-height:1.6;color:#334155;margin:0">
            {html.escape(summary)}
          </p>
          <p style="margin:10px 0 0;font-size:13px">
            <a href="{html.escape(story.link, quote=True)}">Leer noticia completa</a>
          </p>
        </article>
        """)

    body = f"""<!doctype html>
    <html lang="es"><body style="margin:0;background:#f8fafc;font-family:Arial,Helvetica,sans-serif;color:#0f172a">
      <main style="max-width:680px;margin:24px auto;padding:28px;background:#ffffff;border:1px solid #e2e8f0;border-radius:12px">
        <p style="font-size:12px;letter-spacing:1px;text-transform:uppercase;color:#64748b;margin:0 0 8px">Briefing personal</p>
        <h1 style="font-size:26px;margin:0 0 6px">Tu resumen diario</h1>
        <p style="font-size:14px;color:#64748b;margin:0 0 18px">{html.escape(date_label)} · Hasta {MAX_STORIES} noticias</p>
        {''.join(cards)}
        <p style="font-size:12px;line-height:1.5;color:#64748b;margin:22px 0 0">
          Selección automática a partir de titulares y extractos de Google News RSS.
          Este primer montaje no utiliza una API de IA ni genera análisis que no aparezcan en las fuentes.
        </p>
      </main></body></html>"""
    msg.add_alternative(body, subtype="html")
    return msg


def send_email(msg: EmailMessage, address: str, app_password: str) -> None:
    msg["From"] = address
    msg["To"] = address
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as server:
        server.login(address, app_password.replace(" ", ""))
        server.send_message(msg)


def main() -> int:
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    if not address or not app_password:
        print(
            "Faltan los secretos GMAIL_ADDRESS y GMAIL_APP_PASSWORD. "
            "Añádelos en Settings > Secrets and variables > Actions del repositorio.",
            file=sys.stderr,
        )
        return 2

    print("Recopilando fuentes RSS…")
    stories = collect_stories()
    print(f"Seleccionadas {len(stories)} noticias:")
    for story in stories:
        print(f"- [{story.topic}] {story.title} ({story.source})")

    message = build_email(stories)
    send_email(message, address, app_password)
    print(f"Correo enviado correctamente a {address}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
