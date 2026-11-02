"""The ad / tracker / popunder list kept in the Hub (v0.1.24)."""
import re
import unittest

import rb_blocklist as bl


class BlocklistTests(unittest.TestCase):
    def test_size_and_well_known_entries(self):
        self.assertGreaterEqual(len(bl.domains()), 140)
        for domain in ("doubleclick.net", "googlesyndication.com", "googleadservices.com",
                       "adservice.google.com", "google-analytics.com", "googletagmanager.com",
                       "popads.net", "popcash.net", "propellerads.com", "exoclick.com",
                       "juicyads.com", "adsterra.com", "trafficjunky.net", "hilltopads.net",
                       "a-ads.com", "adnxs.com", "criteo.com", "taboola.com", "outbrain.com"):
            with self.subTest(domain=domain):
                self.assertIn(domain, bl.domains())

    def test_entries_are_clean_unique_lowercase_domains(self):
        seen = set()
        for domain in bl.domains():
            self.assertRegex(domain, r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
            self.assertNotIn(domain, seen)
            seen.add(domain)

    def test_both_apex_and_subdomain_patterns_per_domain(self):
        patterns = bl.patterns()
        self.assertEqual(len(patterns), 2 * len(bl.domains()))
        self.assertIn("*://doubleclick.net/*", patterns)
        self.assertIn("*://*.doubleclick.net/*", patterns)
        for pattern in patterns:
            self.assertRegex(pattern, r"^\*://(\*\.)?[a-z0-9.-]+/\*$")

    def test_hoster_domains_are_never_blocked(self):
        for domain in bl.NEVER_BLOCK:
            self.assertIn(domain, ("rapidgator.net", "rg.to"))
        for pattern in bl.patterns():
            host = re.sub(r"^\*://(\*\.)?", "", pattern)[:-2]
            for hoster in bl.NEVER_BLOCK:
                self.assertFalse(host == hoster or host.endswith("." + hoster) or hoster.endswith("." + host),
                                 pattern)

    def test_hoster_entries_would_be_dropped_even_if_listed(self):
        original = bl.DOMAINS
        try:
            bl.DOMAINS = ("rapidgator.net", "static.rapidgator.net", "rg.to", "net", "ok.example.com")
            self.assertNotIn("rapidgator.net", bl.domains())
            self.assertNotIn("static.rapidgator.net", bl.domains())
            self.assertNotIn("rg.to", bl.domains())
            self.assertIn("ok.example.com", bl.domains())
        finally:
            bl.DOMAINS = original

    def test_redirectors_the_probe_needs_are_not_listed(self):
        for domain in ("adf.ly", "bit.ly", "t.co", "google.com", "facebook.com"):
            self.assertNotIn(domain, bl.domains())


if __name__ == "__main__":
    unittest.main()
