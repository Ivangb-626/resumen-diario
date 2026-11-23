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
        Image.new("RGB", (32, 24), "navy").save(buffer, format="WEBP")
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


if __name__ == "__main__":
    unittest.main()
