#!/usr/bin/env python3
"""Live image-source smoke test and visual gallery for recent Google News RSS stories."""

from __future__ import annotations

import html
import sys
from collections import Counter
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

    output_dir = Path("image-samples")
    output_dir.mkdir(parents=True, exist_ok=True)
    attached = [article for article in articles if article.get("_image_data")]
    source_counts = Counter(
        str(article.get("image_source") or "desconocido") for article in attached
    )
    relation_counts = Counter(
        str(article.get("image_match_type") or "ilustrativa/respaldo")
        for article in attached
    )
    rows = []
    for index, article in enumerate(articles, 1):
        image_data = article.get("_image_data")
        if not image_data:
            print(f"- SIN IMAGEN | {article['headline'][:100]}")
            continue
        filename = f"{index:02d}-{article['id']}.jpg"
        (output_dir / filename).write_bytes(image_data)
        relation = str(article.get("image_match_type") or "ilustrativa/respaldo")
        score = article.get("image_relevance_score")
        source = str(article.get("image_source") or "desconocido")
        credit = str(article.get("image_credit") or "")
        rows.append(
            "<article><h2>" + html.escape(str(article["headline"])) + "</h2>"
            + f"<img src='{html.escape(filename, quote=True)}' alt='Fotografía seleccionada' "
              "style='max-width:100%;max-height:360px;object-fit:contain'>"
            + "<p><b>Relación:</b> " + html.escape(relation)
            + " · <b>Fuente:</b> " + html.escape(source)
            + (f" · <b>Puntuación:</b> {float(score):.2f}" if score is not None else "")
            + "</p><p>" + html.escape(credit) + "</p>"
            + "<p><a href='" + html.escape(str(article.get("url") or ""), quote=True)
            + "'>Abrir noticia original</a></p></article>"
        )
        print(
            f"- {source} | {relation} | score={score if score is not None else 'n/d'} | "
            f"{article['headline'][:100]}"
        )

    gallery = (
        "<!doctype html><html lang='es'><meta charset='utf-8'>"
        "<title>Galería de comprobación de imágenes</title>"
        "<style>body{font:16px Arial,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem}"
        "article{border-bottom:1px solid #ddd;padding:1rem 0}img{display:block}"
        "</style><h1>Revisión visual de imágenes del resumen diario</h1>"
        + "".join(rows) + "</html>"
    )
    (output_dir / "index.html").write_text(gallery, encoding="utf-8")

    print(f"Prueba real completada: {len(attached)}/{len(articles)} fotografías incrustables.")
    print(f"Fuentes: {dict(source_counts)}")
    print(f"Relación estimada: {dict(relation_counts)}")
    print(f"Galería guardada en: {output_dir.resolve()}")

    required = len(articles)
    if len(attached) < required:
        raise RuntimeError(
            f"Cobertura insuficiente: {len(attached)}/{required}; "
            f"se exigen al menos {required} fotografías descargables."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
