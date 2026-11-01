"""Browser targets are validated offline without any NAS-side HTTP fetch."""
import unittest
import browser_safety


class BrowserTargetTests(unittest.TestCase):
    def test_public_https_domain_only(self):
        self.assertEqual(browser_safety.validate("https://rapidgator.net/file/1234"),
                         "https://rapidgator.net/file/1234")
        self.assertEqual(browser_safety.validate("https://Example.COM:443/a?q=1#frag"),
                         "https://example.com:443/a?q=1#frag")
        self.assertEqual(browser_safety.validate("https://www.gnu.org/"),
                         "https://www.gnu.org/")

    def test_private_network_rejected_even_in_https(self):
        invalid=("http://example.com/", "javascript:alert(1)", "data:text/html,hi",
                 "file:///etc/passwd", "https://localhost/", "https://nas.local/",
                 "https://router.lan/", "https://host.home.arpa/",
                 "https://127.0.0.1/", "https://192.168.68.57:17864/",
                 "https://[::1]/", "https://8.8.8.8/", "https://127.1/",
                 "https://user:pass@example.com/", "https://example.com:444/",
                 "https://example.com@127.0.0.1/", "https://example.com\\@evil.test/",
                 "https://example.com/%0a", "https://example.com/../\\etc")
        for value in invalid:
            with self.subTest(url=value), self.assertRaises(ValueError):
                browser_safety.validate(value)

    def test_bounded_normalization_and_no_dns_or_network(self):
        for value in ("", None, "https://example.com/"+"a"*2500, "https://example.com/\n"):
            with self.subTest(value=str(value)[:50]), self.assertRaises(ValueError):
                browser_safety.validate(value)
        import inspect
        code=inspect.getsource(browser_safety)
        for forbidden in ("urlopen(", "socket.getaddrinfo(", "requests.get(",
                          "subprocess.Popen(", "http.client."):
            self.assertNotIn(forbidden,code)


if __name__=="__main__":
    unittest.main()
