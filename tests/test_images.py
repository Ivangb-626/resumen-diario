import io
import json
import unittest
from unittest.mock import patch

from PIL import Image

from scripts import send_digest as digest


class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "text/html; charset=utf-8",
                 url: str = "https://publisher.example/story"):
        self.body = body
        self.headers = {"Content-Type": content_type}
        self.url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]

    def geturl(self):
        return self.url


class ArticleImageTests(unittest.TestCase):
    def test_01_jsonld_newsarticle_string_image(self):
        raw = json.dumps({
            "@type": "NewsArticle",
            "image": "https://cdn.example/news/lead.jpg",
        })
        self.assertEqual(
            digest.jsonld_article_images(raw),
            ["https://cdn.example/news/lead.jpg"],
        )

    def test_02_jsonld_nested_graph(self):
        raw = json.dumps({
            "@context": "https://schema.org",
            "@graph": [{"@type": "https://schema.org/NewsArticle",
                        "image": {"@type": "ImageObject",
                                  "url": "https://cdn.example/graph.jpg"}}],
        })
        self.assertEqual(
            digest.jsonld_article_images(raw),
            ["https://cdn.example/graph.jpg"],
        )

    def test_03_jsonld_array_of_images(self):
        raw = json.dumps({
            "@type": ["Article"],
            "image": [
                {"url": "https://cdn.example/a.jpg"},
                {"contentUrl": "https://cdn.example/b.jpg"},
            ],
        })
        self.assertEqual(
            digest.jsonld_article_images(raw),
            ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"],
        )

    def test_04_open_graph_and_twitter_metadata(self):
        parser = digest.ArticleImageParser()
        parser.feed(
            '<meta property="og:image" content="/media/og.jpg">'
            '<meta name="twitter:image" content="https://cdn.example/twitter.jpg">'
        )
        self.assertEqual(len(parser.meta_images), 2)

    def test_05_image_inside_article_element(self):
        parser = digest.ArticleImageParser()
        parser.feed('<article><img src="https://cdn.example/hero.jpg" '
                    'alt="Central bank announcement"></article>')
        self.assertEqual(len(parser.html_images), 1)
        self.assertTrue(parser.html_images[0]["in_article"])

    def test_06_srcset_candidates_are_all_retained(self):
        parser = digest.ArticleImageParser()
        parser.feed('<article><picture><source srcset="small.jpg 480w, large.jpg 1200w">'
                    '<img src="fallback.jpg"></picture></article>')
        urls = [item["url"] for item in parser.html_images]
        self.assertIn("small.jpg", urls)
        self.assertIn("large.jpg", urls)
        self.assertIn("fallback.jpg", urls)

    def test_07_lazy_loaded_images_are_retained(self):
        parser = digest.ArticleImageParser()
        parser.feed('<main><img data-src="/photos/lazy.jpg" '
                    'data-srcset="/photos/lazy2.jpg 900w"></main>')
        urls = [item["url"] for item in parser.html_images]
        self.assertIn("/photos/lazy.jpg", urls)
        self.assertIn("/photos/lazy2.jpg", urls)

    def test_08_headline_matching_rewards_related_content(self):
        related = digest.image_text_score(
            "European Central Bank cuts interest rates",
            "European Central Bank announces rate cut",
        )
        unrelated = digest.image_text_score(
            "European Central Bank cuts interest rates",
            "A football match in Madrid",
        )
        self.assertGreater(related, unrelated)

    def test_09_generic_assets_are_rejected(self):
        self.assertTrue(digest.image_url_is_generic("https://cdn.example/assets/site-logo.png"))
        self.assertFalse(digest.image_url_is_generic("https://cdn.example/photos/central-bank.jpg"))

    def test_10_relative_image_urls_are_resolved(self):
        self.assertEqual(
            digest.usable_image_url("../images/lead.jpg", "https://news.example/a/story"),
            "https://news.example/images/lead.jpg",
        )
        self.assertEqual(digest.usable_image_url("javascript:alert(1)"), "")

    def test_11_publisher_page_exposes_in_body_candidate(self):
        page = b'<html><article><img src="/media/report-photo.jpg" alt="Report photo"></article></html>'
        with patch.object(digest.urllib.request, "urlopen",
                          return_value=FakeResponse(page)):
            urls, final_url = digest.fetch_article_images({
                "url": "https://publisher.example/story",
                "headline": "Report",
            })
        self.assertIn("https://publisher.example/media/report-photo.jpg", urls)
        self.assertEqual(final_url, "https://publisher.example/story")

    def test_12_webp_is_converted_to_email_safe_jpeg(self):
        buffer = io.BytesIO()
        image = Image.effect_noise((500, 350), 90).convert("RGB")
        image.save(buffer, format="WEBP", quality=90)
        response = FakeResponse(buffer.getvalue(), "image/webp",
                                "https://cdn.example/image.webp")
        with patch.object(digest.urllib.request, "urlopen", return_value=response):
            result = digest.download_inline_image(
                "https://cdn.example/image.webp",
                "https://publisher.example/story",
            )
        self.assertIsNotNone(result)
        data, subtype = result
        self.assertEqual(subtype, "jpeg")
        self.assertTrue(data.startswith(b"\xff\xd8"))

    def test_13_failed_first_source_falls_through_to_next(self):
        valid = (b"\xff\xd8test", "jpeg")
        with patch.object(digest, "download_inline_image",
                          side_effect=[None, valid]):
            result = digest.download_first_available([
                ("https://bad.example/one.webp", "publisher",
                 "https://publisher.example/", ""),
                ("https://good.example/two.jpg", "rss",
                 "https://publisher.example/", ""),
            ])
        self.assertIsNotNone(result)
        self.assertEqual(result[2], "https://good.example/two.jpg")
        self.assertEqual(result[3], "rss")


    def test_14_google_news_thumbnail_cdn_is_allowed(self):
        page = (
            b'<html><head><meta property="og:image" '
            b'content="https://lh3.googleusercontent.com/news-image=w1200-h630">'
            b'</head></html>'
        )
        with patch.object(digest.urllib.request, "urlopen",
                          return_value=FakeResponse(page)):
            urls, _ = digest.fetch_article_images({
                "url": "https://news.google.com/rss/articles/example",
                "headline": "A current news story",
            })
        self.assertIn(
            "https://lh3.googleusercontent.com/news-image=w1200-h630",
            urls,
        )


    def test_15_publisher_fetch_retries_http_429(self):
        from email.message import Message
        from urllib.error import HTTPError

        headers = Message()
        headers["Retry-After"] = "0"
        rate_limit = HTTPError(
            "https://publisher.example/story", 429, "Too Many Requests", headers, None
        )
        page = b'<html><head><meta property="og:image" '
        page += b'content="https://cdn.example/recovered.jpg"></head></html>'
        with patch.object(
            digest.urllib.request,
            "urlopen",
            side_effect=[rate_limit, FakeResponse(page)],
        ), patch.object(digest.time, "sleep") as sleep:
            urls, _ = digest.fetch_article_images({
                "url": "https://publisher.example/story",
                "headline": "Recovered article",
            })
        self.assertIn("https://cdn.example/recovered.jpg", urls)
        sleep.assert_called_once()


    def test_16_email_contains_inline_image_attachment_and_cid(self):
        article = {
            "id": "N001",
            "section": "Economía y geopolítica",
            "headline": "Prueba de imagen en el correo",
            "summary": "Resumen de prueba.",
            "why_it_matters": "Comprobar la incrustación MIME.",
            "source_name": "Fuente de prueba",
            "url": "https://publisher.example/story",
            "published": "2026-11-23T12:00:00+00:00",
            "_image_data": b"\xff\xd8\xff\xe0fake-jpeg-bytes",
            "_image_subtype": "jpeg",
            "_image_cid": "news-image-1@resumen-diario",
        }
        message = digest.build_email(
            {"articles": [article]}, [], [], [], ai_used=True
        )
        parts = list(message.walk())
        html_part = next(part for part in parts if part.get_content_type() == "text/html")
        self.assertIn("cid:news-image-1@resumen-diario", html_part.get_content())
        self.assertTrue(any(
            part.get_content_type() == "image/jpeg"
            and part["Content-ID"] == "<news-image-1@resumen-diario>"
            for part in parts
        ))


    def test_17_blank_white_image_is_rejected_as_empty(self):
        buffer = io.BytesIO()
        Image.new("RGB", (1200, 675), "white").save(buffer, format="JPEG", quality=90)
        response = FakeResponse(buffer.getvalue(), "image/jpeg",
                                "https://cdn.example/blank.jpg")
        with patch.object(digest.urllib.request, "urlopen", return_value=response):
            result = digest.download_inline_image("https://cdn.example/blank.jpg")
        self.assertIsNone(result)

    def test_18_realistic_varied_image_passes_visual_quality_check(self):
        from PIL import ImageDraw
        image = Image.new("RGB", (900, 600), "white")
        draw = ImageDraw.Draw(image)
        for y in range(0, 600, 20):
            for x in range(0, 900, 20):
                draw.rectangle(
                    (x, y, x + 19, y + 19),
                    fill=((x * 7 + y) % 256, (y * 5 + x // 2) % 256, (x + y * 3) % 256),
                )
        valid, reason = digest.inspect_photo_quality(image)
        self.assertTrue(valid, reason)

    def test_19_gemini_image_queries_replace_long_headlines(self):
        article = {
            "id": "N019",
            "headline": "La Unión Europea abre un procedimiento contra España por incumplir la normativa pesquera",
            "original_headline": "EU launches infringement action against Spain over fisheries rules",
            "summary": "The Commission cites failures in fisheries controls.",
            "section": "España y Gipuzkoa",
        }
        answer = json.dumps({
            "queries": [{
                "id": "N019",
                "queries": ["Spain fishing fleet port inspections", "European Commission fisheries Spain"],
            }]
        })
        with patch.object(digest, "gemini_request", return_value=answer):
            queries = digest.generate_commons_queries([article], "test-key")
        self.assertEqual(
            queries["N019"][0],
            "Spain fishing fleet port inspections",
        )

    def test_20_commons_candidates_require_topic_overlap_and_photo_dimensions(self):
        payload = {
            "query": {
                "pages": {
                    "1": {
                        "title": "File:Spanish fishing boats in Vigo.jpg",
                        "imageinfo": [{
                            "mime": "image/jpeg",
                            "width": 1800,
                            "height": 1200,
                            "thumburl": "https://upload.wikimedia.org/fishing-vigo.jpg",
                            "extmetadata": {
                                "ObjectName": {"value": "Fishing boats in Vigo"},
                                "ImageDescription": {"value": "Spanish fishing fleet in Vigo port"},
                                "Artist": {"value": "Photo author"},
                                "LicenseShortName": {"value": "CC BY-SA"},
                            },
                        }],
                    },
                    "2": {
                        "title": "File:Football match.jpg",
                        "imageinfo": [{
                            "mime": "image/jpeg",
                            "width": 1800,
                            "height": 1200,
                            "thumburl": "https://upload.wikimedia.org/football.jpg",
                            "extmetadata": {
                                "ObjectName": {"value": "Football match"},
                                "ImageDescription": {"value": "Football match in a stadium"},
                            },
                        }],
                    },
                }
            }
        }
        response = FakeResponse(json.dumps(payload).encode(), "application/json")
        article = {
            "id": "N020",
            "headline": "España refuerza los controles sobre su flota pesquera",
            "original_headline": "Spain strengthens controls on its fishing fleet",
        }
        with patch.object(digest.urllib.request, "urlopen", return_value=response):
            candidates = digest.fetch_commons_candidates(
                article, ["Spain fishing fleet Vigo port"]
            )
        self.assertEqual(len(candidates), 1)
        self.assertIn("fishing-vigo.jpg", candidates[0][0])
        self.assertEqual(candidates[0][1], "commons")

    def test_21_fallback_query_removes_editorial_filler(self):
        query = digest.fallback_image_query({
            "original_headline": "The EU launches action against Spain over fisheries rules and fraud prevention"
        })
        self.assertIn("Spain", query)
        self.assertIn("fisheries", query)
        self.assertNotIn("against", query.split())


    def test_22_fallback_visual_query_targets_visible_subject(self):
        self.assertIn(
            "server room",
            digest.fallback_visual_query({
                "headline": "El decreto que regula los centros de datos en Aragón"
            }),
        )
        self.assertIn(
            "procession",
            digest.fallback_visual_query({
                "headline": "El delegado de Hermandades de Málaga rechaza la utilización partidista"
            }),
        )
        self.assertIn(
            "small business",
            digest.fallback_visual_query({
                "headline": "Innovación para reforzar las pymes del País Vasco"
            }),
        )


    def test_23_image_relevance_score_prefers_matching_named_subject(self):
        article = {
            "headline": "El Banco Central Europeo inaugura una nueva sede en Fráncfort",
            "original_headline": "European Central Bank opens new Frankfurt headquarters",
            "summary": "The European Central Bank has opened its new Frankfurt headquarters.",
            "_image_search_queries": ["European Central Bank Frankfurt headquarters"],
            "_image_candidate_descriptors": {
                "https://img.example/match.jpg": (
                    "European Central Bank new headquarters Frankfurt building"
                ),
                "https://img.example/wrong.jpg": "Football stadium Madrid match",
            },
        }
        matching = (
            "https://img.example/match.jpg", "commons", "", ""
        )
        unrelated = (
            "https://img.example/wrong.jpg", "commons", "", ""
        )
        self.assertGreater(
            digest.image_candidate_relevance_score(article, matching),
            digest.image_candidate_relevance_score(article, unrelated),
        )

    def test_24_gemini_visual_rank_sends_inline_images_and_parses_result(self):
        digest._GEMINI_VISION_DISABLED = False
        answer = json.dumps({
            "candidates": [{
                "content": {"parts": [{
                    "text": json.dumps({
                        "images": [{
                            "index": 1, "relevance": 94,
                            "relation": "direct", "reason": "Sede identificable",
                        }]
                    })
                }]}
            }]
        }).encode()
        downloaded = [(
            ("https://img.example/ecb.jpg", "commons", "", ""),
            b"\xff\xd8fake-jpeg",
            0.82,
        )]
        with patch.object(
            digest.urllib.request, "urlopen",
            return_value=FakeResponse(answer, "application/json"),
        ) as mocked:
            result = digest.gemini_visual_rank({
                "id": "N024",
                "headline": "El BCE inaugura su nueva sede en Fráncfort",
                "summary": "El banco central presenta sus nuevas instalaciones.",
            }, downloaded, "test-key")

        self.assertEqual(result[1]["relation"], "direct")
        self.assertEqual(result[1]["relevance"], 94)
        request = mocked.call_args.args[0]
        payload = json.loads(request.data.decode())
        self.assertTrue(any(
            "inline_data" in part
            for part in payload["contents"][0]["parts"]
        ))

    def test_25_visual_review_selects_relevant_candidate_over_first_download(self):
        digest._GEMINI_VISION_DISABLED = False
        article = {
            "id": "N025",
            "headline": "El Banco Central Europeo inaugura su nueva sede en Fráncfort",
            "original_headline": "European Central Bank opens its new Frankfurt headquarters",
            "summary": "The ECB opened its new headquarters in Frankfurt.",
            "_image_search_queries": ["European Central Bank Frankfurt headquarters"],
            "_image_candidate_descriptors": {
                "https://img.example/metadata-match.jpg": (
                    "European Central Bank building Frankfurt"
                ),
                "https://img.example/visually-correct.jpg": (
                    "Frankfurt skyline and modern glass tower"
                ),
            },
        }
        candidates = [
            ("https://img.example/metadata-match.jpg", "commons", "", ""),
            ("https://img.example/visually-correct.jpg", "publisher", "", ""),
        ]
        with patch.object(
            digest, "download_inline_image",
            side_effect=[(b"first-photo", "jpeg"), (b"second-photo", "jpeg")],
        ), patch.object(
            digest, "gemini_visual_rank",
            return_value={
                1: {"relation": "unrelated", "relevance": 12, "reason": "No coincide"},
                2: {"relation": "direct", "relevance": 96, "reason": "Sede visible"},
            },
        ):
            result = digest.download_ranked_candidates(article, candidates, "test-key")

        self.assertIsNotNone(result)
        self.assertEqual(result[2], "https://img.example/visually-correct.jpg")
        self.assertEqual(article["image_match_type"], "direct")

    def test_26_prepare_images_searches_publisher_even_when_commons_has_results(self):
        article = {
            "id": "N026",
            "url": "https://publisher.example/story",
            "headline": "Una noticia concreta sobre una nueva instalación",
            "original_headline": "A specific story about a new facility",
            "summary": "The facility opened this week.",
            "section": "Ciencia, investigación y tecnología",
            "rss_image_url": "",
        }

        def mark_embedded(articles, _candidates, _api_key=""):
            for item in articles:
                item["_image_data"] = b"validated-photo"
                item["_image_subtype"] = "jpeg"
                item["_image_cid"] = "test-image"
                item["image_source"] = "test"

        with patch.object(
            digest, "generate_commons_queries",
            return_value={"N026": ["new facility exact subject", "facility exterior"]},
        ), patch.object(
            digest, "fetch_commons_candidates",
            return_value=[("https://commons.example/facility.jpg", "commons", "", "")],
        ) as commons_search, patch.object(
            digest, "fetch_article_images",
            return_value=(["https://publisher.example/lead.jpg"], "https://publisher.example/story"),
        ) as publisher_search, patch.object(
            digest, "embed_candidates", side_effect=mark_embedded,
        ):
            digest.prepare_article_images({"articles": [article]}, api_key="")

        commons_search.assert_called_once()
        publisher_search.assert_called_once()
        self.assertEqual(article["image_source"], "test")

    def test_27_gemini_illustrative_classification_is_not_passed_as_specific(self):
        digest._GEMINI_VISION_DISABLED = False
        article = {
            "id": "N027",
            "headline": "La Comisión Europea anuncia una sanción a una empresa concreta",
            "original_headline": "European Commission sanctions Acme Corporation",
            "summary": "The action concerns Acme Corporation.",
            "_image_search_queries": ["European Commission Acme Corporation"],
            "_image_candidate_descriptors": {
                "https://img.example/eu-building.jpg": "European Commission headquarters Brussels",
            },
        }
        candidate = ("https://img.example/eu-building.jpg", "commons", "", "")
        with patch.object(
            digest, "download_inline_image",
            return_value=(b"photo", "jpeg"),
        ), patch.object(
            digest, "gemini_visual_rank",
            return_value={
                1: {"relation": "illustrative", "relevance": 88, "reason": "Solo edificio institucional"},
            },
        ):
            result = digest.download_ranked_candidates(article, [candidate], "test-key")

        self.assertIsNone(result)




    def test_28_google_news_thumbnail_offers_upscaled_cdn_variants(self):
        url = "https://lh3.googleusercontent.com/news-image=w300-h300-p-k-no-nu"
        variants = digest.google_image_url_variants(url)
        self.assertEqual(
            variants[0],
            "https://lh3.googleusercontent.com/news-image=w1200-h800",
        )
        self.assertIn("=s1600", variants[1])
        self.assertEqual(variants[-1], url)
        self.assertEqual(
            digest.google_image_url_variants("https://cdn.example/photo.jpg"),
            ["https://cdn.example/photo.jpg"],
        )

    def test_29_thematic_fallback_searches_are_story_specific(self):
        queries = digest.fallback_visual_queries({
            "headline": "El franco suizo se aprecia ante la cautela de los inversores",
            "section": "Economía y geopolítica",
        })
        self.assertIn("Swiss franc banknotes currency exchange", queries)
        self.assertIn("Swiss National Bank headquarters Zurich", queries)
        self.assertFalse(any("city skyline" in query for query in queries))

    def test_30_commons_rejects_candidates_with_only_one_shared_generic_word(self):
        payload = {
            "query": {
                "pages": {
                    "1": {
                        "title": "File:Swiss city skyline.jpg",
                        "imageinfo": [{
                            "mime": "image/jpeg", "width": 1800, "height": 1200,
                            "thumburl": "https://upload.wikimedia.org/swiss-city.jpg",
                            "extmetadata": {
                                "ObjectName": {"value": "Swiss city skyline"},
                                "ImageDescription": {"value": "A generic skyline of a Swiss city"},
                            },
                        }],
                    }
                }
            }
        }
        article = {
            "id": "N030",
            "headline": "El franco suizo gana valor frente al euro",
            "original_headline": "Swiss franc gains against the euro",
        }
        response = FakeResponse(json.dumps(payload).encode(), "application/json")
        with patch.object(digest.urllib.request, "urlopen", return_value=response):
            results = digest.fetch_commons_candidates(
                article, ["Swiss franc banknotes currency"]
            )
        self.assertEqual(results, [])

    def test_31_story_theme_fallback_never_searches_a_shared_generic_city_pool(self):
        article = {
            "id": "N031",
            "headline": "El franco suizo gana valor frente al euro",
            "original_headline": "Swiss franc gains against the euro",
            "summary": "Investors seek safe currencies.",
            "section": "Economía y geopolítica",
            "url": "https://publisher.example/story",
            "rss_image_url": "",
        }
        calls = []

        def commons(story, queries=None):
            calls.append((story.get("id"), list(queries or [])))
            return []

        with patch.object(
            digest, "generate_commons_queries",
            return_value={"N031": ["Swiss franc exchange rate"]},
        ), patch.object(
            digest, "fetch_commons_candidates", side_effect=commons,
        ), patch.object(
            digest, "fetch_article_images",
            return_value=([], "https://publisher.example/story"),
        ), patch.object(
            digest, "fetch_gdelt_images", return_value={},
        ), patch.object(
            digest, "fetch_openverse_image", return_value=("", ""),
        ), patch.object(
            digest, "embed_candidates",
        ), patch.dict("os.environ", {"BRAVE_SEARCH_API_KEY": ""}):
            digest.prepare_article_images({"articles": [article]}, api_key="")

        self.assertEqual(len(calls), 2)
        self.assertTrue(all(story_id == "N031" for story_id, _queries in calls))
        self.assertFalse(any(
            "city skyline" in query or "street photography" in query
            for _story_id, queries in calls for query in queries
        ))
        self.assertFalse(article.get("_image_data"))



    def test_32_publisher_provenance_does_not_fabricate_a_text_match(self):
        article = {
            "headline": "El Banco Central Europeo presenta nuevas instalaciones en Fráncfort",
            "original_headline": "European Central Bank presents new Frankfurt facilities",
            "summary": "The ECB presented its new facilities.",
            "_image_search_queries": ["European Central Bank Frankfurt headquarters"],
            "_image_candidate_descriptors": {},
        }
        score = digest.image_candidate_relevance_score(
            article,
            ("https://publisher.example/og-image.jpg", "publisher",
             "https://publisher.example/story", ""),
        )
        self.assertGreater(score, 0.2)
        self.assertLess(score, 0.6)



if __name__ == "__main__":
    unittest.main()
