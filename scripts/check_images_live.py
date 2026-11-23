#!/usr/bin/env python3
"""Live image-source smoke test using fresh Google News RSS stories."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from send_digest import collect_stories, prepare_article_images


def main() -> int:
    stories = collect_stories()
    selected = stories[:8]
    articles = [{
        "id": story.id,
        "url": story.link,
        "headline": story.title,
        "original_headline": story.title,
        "source_name": story.source,
        "rss_image_url": story.image_url,
        "image_url": story.image_url,
    } for story in selected]
    print(f"Prueba real: {len(articles)} noticias RSS recientes.")
    prepare_article_images({"articles": articles})

    attached = [article for article in articles if article.get("_image_data")]
    print(f"Prueba real completada: {len(attached)}/{len(articles)} fotografías incrustables.")
    for article in articles:
        print(
            f"- {article['source_name']}: "
            f"{article['headline'][:100]} | "
            f"{article.get('image_source', 'SIN IMAGEN')}"
        )

    # Every selected story must have a non-empty, validated image, using an
    # explicitly labelled illustrative Commons photo as the final fallback.
    required = len(articles)
    if len(attached) < required:
        raise RuntimeError(
            f"Cobertura insuficiente: {len(attached)}/{len(articles)}; "
            f"se exigen al menos {required} fotografías descargables."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
