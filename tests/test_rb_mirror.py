"""Mirror display (v0.1.39): CSS / HTML sanitizing, token map, resource checks, gzip, endpoints."""
import gzip
import unittest
from unittest.mock import patch

import rb_mirror as m
import remote_browser
import webapp
from test_webapp import HttpTests

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 30


def reg_into(store):
    def register(url, kind):
        store.append((url, kind))
        return "T%d" % len(store)
    return register


class CssTests(unittest.TestCase):
    def rewrite(self, css, base="https://a.example.com/dir/"):
        seen = []
        out = m.rewrite_css(css, base, reg_into(seen), None)
        return out, seen

    def test_urls_become_hub_references_and_nothing_external_survives(self):
        out, seen = self.rewrite("a{background:url(bg.png)} b{background-image:url('//ev.example.net/x.png')}")
        self.assertNotIn("example", out)
        self.assertIn('url("T1")', out)
        self.assertIn(("https://a.example.com/dir/bg.png", "img"), seen)

    def test_dangerous_constructs_are_dropped(self):
        for css in ("a{behavior:url(x.htc)}", "a{-moz-binding:url(x)}", "a{width:expression(alert(1))}",
                    "@import url(http://evil.example.net/x.css);", "a{background:url(javascript:alert(1))}"):
            with self.subTest(css=css):
                out, _ = self.rewrite(css)
                for bad in ("behavior", "binding", "expression", "evil", "javascript"):
                    self.assertNotIn(bad, out.lower())

    def test_text_is_not_an_attack_surface(self):
        out, _ = self.rewrite("a::after{content:'</style><script>alert(1)</script>'}")
        doc = m.build_document(out, "")
        self.assertEqual(doc.count("</style>"), 2)
        from html.parser import HTMLParser

        class Tags(HTMLParser):
            tags = []

            def handle_starttag(self, tag, attrs):
                self.tags.append(tag)
        parser = Tags()
        parser.feed(doc)
        self.assertNotIn("script", parser.tags)   # inside <style> the text stays text


class HtmlTests(unittest.TestCase):
    def clean(self, raw):
        seen = []
        out, stats = m.sanitize_html(raw, "https://a.example.com/", reg_into(seen))
        return out, stats, seen

    def test_scripts_handlers_and_navigating_attributes_are_removed(self):
        raw = ('<div onclick="x()" style="color:red"><script>alert(1)</script><a href="javascript:1" data-m="3">l</a>'
               '<iframe src="http://e.example.net/"></iframe><form action="http://e.example.net/"><input name=q></form>'
               '<img src="/p.png" srcset="http://e.example.net/a.png 2x"></div>')
        out, stats, seen = self.clean(raw)
        for bad in ("<script", "onclick", "javascript", "<iframe", "action=", "srcset", "e.example.net", "href="):
            self.assertNotIn(bad, out)
        self.assertNotIn("<form", out)
        self.assertEqual(seen, [("https://a.example.com/p.png", "img")])
        self.assertIn("data-m=\"3\"", out)

    def test_inputs_are_read_only_so_no_keyboard_opens(self):
        out, _, _ = self.clean('<input type="text" value="v"><textarea>t</textarea>')
        self.assertEqual(out.count("readonly"), 2)
        self.assertEqual(out.count('inputmode="none"'), 2)

    def test_file_inputs_become_text(self):
        out, _, _ = self.clean('<input type="file">')
        self.assertNotIn("file", out)


class TokenAndCacheTests(unittest.TestCase):
    def test_token_map_cap_dedup_and_clear(self):
        tokens = m.TokenMap(cap=3)
        a = tokens.register("https://a.example.com/1", "img")
        self.assertEqual(tokens.register("https://a.example.com/1", "img"), a)
        tokens.register("https://a.example.com/2", "img")
        tokens.register("https://a.example.com/3", "font")
        self.assertIsNone(tokens.register("https://a.example.com/4", "img"))
        self.assertEqual(tokens.get(a)["url"], "https://a.example.com/1")
        tokens.clear()
        self.assertEqual(len(tokens), 0)
        self.assertIsNone(tokens.get(a))

    def test_resource_cache_is_an_lru_bounded_by_bytes(self):
        cache = m.ResourceCache(cap_bytes=10)
        cache.put("b1", "a", b"1234", "x")
        cache.put("b1", "b", b"1234", "x")
        cache.get("b1", "a")                     # a is now newest
        cache.put("b1", "c", b"1234", "x")       # evicts b
        self.assertIsNone(cache.get("b1", "b"))
        self.assertIsNotNone(cache.get("b1", "a"))
        cache.put("b1", "big", b"x" * 11, "x")   # larger than the cap: not stored
        self.assertIsNone(cache.get("b1", "big"))
        cache.drop_tab("b1")
        self.assertEqual((len(cache), cache.bytes), (0, 0))

    def test_resource_checks(self):
        self.assertEqual(m.check_resource("img", PNG)[1], "image/png")
        for kind, data, ctype in (("img", b"<html></html>", None), ("img", PNG, "text/html"),
                                  ("img", b"", None), ("font", PNG, None),
                                  ("img", b"<svg xmlns='x'></svg>", "text/html"),
                                  ("img", PNG + b"x" * m.IMAGE_MAX_BYTES, None)):
            with self.subTest(kind=kind, ctype=ctype):
                with self.assertRaises(m.ResourceRefused):
                    m.check_resource(kind, data, ctype)
        self.assertEqual(m.check_resource("img", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>",
                                          "image/svg+xml")[1], "image/svg+xml")


class DocumentTests(unittest.TestCase):
    def test_document_has_the_strict_policy_and_no_script(self):
        doc = m.build_document("a{color:red}", "<p>x</p>")
        self.assertIn("Content-Security-Policy", doc)
        self.assertIn(m.DOC_CSP, doc)
        self.assertNotIn("<script", doc.lower())
        self.assertIn("default-src 'none'", m.DOC_CSP)
        self.assertNotIn("script-src", m.DOC_CSP.replace("default-src 'none'", ""))

    def test_gzip_negotiation(self):
        self.assertTrue(webapp.accepts_gzip("gzip, deflate"))
        self.assertFalse(webapp.accepts_gzip("identity"))
        self.assertFalse(webapp.accepts_gzip(None))
        self.assertFalse(webapp.accepts_gzip("gzip;q=0"))


class EndpointTests(HttpTests):
    POST = (("/api/rb/mirror", {"tab": "b1"}), ("/api/rb/mirror/click", {"tab": "b1", "m": 1}),
            ("/api/rb/mirror/release", {"tab": "b1"}))
    GET = ("/api/rb/mirror/doc?tab=b1&rev=1", "/api/rb/mirror/res?tab=b1&r=abcd")

    def test_login_csrf_and_origin_are_required(self):
        for path in self.GET:
            self.assertEqual(self.req(path)[0], 401)
        for path, body in self.POST:
            self.assertIn(self.req(path, body)[0], (401, 403))
        self.login()
        for path, body in self.POST:
            self.assertEqual(self.req(path, body, csrf=False)[0], 403)
            self.assertEqual(self.req(path, body, origin=False)[0], 403)

    def test_validation_and_error_mapping(self):
        self.login()
        self.assertEqual(self.req("/api/rb/mirror", {})[0], 400)
        self.assertEqual(self.req("/api/rb/mirror", {"tab": "b9"})[0], 404)
        for bad in ("rev=x", "rev=-1", "rev=" + "1" * 13, "rev="):
            self.assertEqual(self.get_raw("/api/rb/mirror/doc?tab=b1&" + bad)[0], 400)
        self.req("/api/rb/tabs", {"url": "https://example.com/a"})
        self.assertEqual(self.get_raw("/api/rb/mirror/doc?tab=b1&rev=1")[0], 410)
        self.assertEqual(self.get_raw("/api/rb/mirror/res?tab=b1&r=zz")[0], 410)
        self.assertEqual(self.get_raw("/api/rb/mirror/res?tab=b9&r=zz")[0], 404)
        for body in ({"tab": "b1", "m": "x"}, {"tab": "b1", "m": -1}, {"tab": "b1", "m": True}):
            self.assertEqual(self.req("/api/rb/mirror/click", body)[0], 400)
        self.assertEqual(self.req("/api/rb/mirror/release", {"tab": "b1"})[0], 200)

    def test_document_resource_and_gzip_headers(self):
        self.login()
        self.req("/api/rb/tabs", {"url": "https://example.com/a"})
        rb = self.server.rb
        text = m.build_document("a{}", "<p>" + "あ" * 3000 + "</p>")
        with patch.object(rb, "mirror_document", return_value=text):
            import urllib.request
            request = urllib.request.Request(self.url + "/api/rb/mirror/doc?tab=b1&rev=1",
                                             headers={"Host": self.host, "Accept-Encoding": "gzip"})
            with self.client.open(request, timeout=4) as response:
                self.assertEqual(response.headers["Content-Encoding"], "gzip")
                self.assertEqual(response.headers["Content-Security-Policy"], m.DOC_CSP_HEADER)
                self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
                self.assertEqual(gzip.decompress(response.read()).decode(), text)
            code, headers, raw = self.get_raw("/api/rb/mirror/doc?tab=b1&rev=1")
            self.assertEqual((code, raw.decode()), (200, text))
            self.assertNotIn("Content-Encoding", headers)
        with patch.object(rb, "mirror_resource", return_value=(PNG, "image/png")):
            code, headers, raw = self.get_raw("/api/rb/mirror/res?tab=b1&r=abcd")
            self.assertEqual((code, raw), (200, PNG))
            self.assertEqual(headers["Cache-Control"], "private, max-age=300")
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        svg = b"<svg xmlns='http://www.w3.org/2000/svg'/>"
        with patch.object(rb, "mirror_resource", return_value=(svg, "image/svg+xml")):
            code, headers, raw = self.get_raw("/api/rb/mirror/res?tab=b1&r=abcd")
            self.assertEqual(headers["Content-Security-Policy"], m.RES_SVG_CSP)
        with patch.object(rb, "mirror_resource", side_effect=remote_browser.ImageRefused("x")):
            self.assertEqual(self.get_raw("/api/rb/mirror/res?tab=b1&r=abcd")[0], 415)

    def test_click_on_a_link_navigates_through_the_normal_checks(self):
        self.login()
        self.req("/api/rb/tabs", {"url": "https://example.com/a"})
        rb = self.server.rb
        with patch.object(rb, "mirror_click", return_value={"ok": True, "navigated": False,
                                                            "href": "https://example.org/x"}), \
                patch.object(rb, "navigate") as nav:
            code, body = self.req("/api/rb/mirror/click", {"tab": "b1", "m": 5})
            self.assertEqual((code, body["navigated"], body["url"]), (200, True, "https://example.org/x"))
            nav.assert_called_once()
        with patch.object(rb, "mirror_click", return_value={"ok": True, "navigated": False,
                                                            "href": "http://192.168.0.1/admin"}), \
                patch.object(rb, "navigate") as nav:
            self.assertEqual(self.req("/api/rb/mirror/click", {"tab": "b1", "m": 5})[0], 400)
            nav.assert_not_called()
        with patch.object(rb, "mirror_click", side_effect=remote_browser.MirrorGone("gone")):
            self.assertEqual(self.req("/api/rb/mirror/click", {"tab": "b1", "m": 5})[0], 410)


_OWN = [EndpointTests("test_login_csrf_and_origin_are_required"), EndpointTests("test_validation_and_error_mapping"),
        EndpointTests("test_document_resource_and_gzip_headers"),
        EndpointTests("test_click_on_a_link_navigates_through_the_normal_checks")]


def load_tests(loader, tests, pattern):
    # EndpointTests inherits every HttpTests test; only the mirror ones belong to this module.
    suite = unittest.TestSuite()
    for case in (CssTests, HtmlTests, TokenAndCacheTests, DocumentTests):
        suite.addTests(loader.loadTestsFromTestCase(case))
    suite.addTests(_OWN)
    return suite

if __name__ == "__main__":
    unittest.main()
