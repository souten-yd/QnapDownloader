import unittest
from unittest.mock import patch
import rapidgator_extract as rg


class RapidgatorExtractionTests(unittest.TestCase):
    def test_text_links_normalize_and_deduplicate(self):
        found=rg.extract("https://rapidgator.net/file/1234/filename.zip "
          "https://rg.to/file/1234?utm_x=1 "
          "https://www.rapidgator.net/file/9876#anchor")
        self.assertEqual(found, ["https://rapidgator.net/file/1234",
                                  "https://rapidgator.net/file/9876"])

    def test_html_anchor_visible_links_and_script_ignored(self):
        page=('''<a href="https://rg.to/file/9876?utm=1">one</a>'''
              '''<script>https://rapidgator.net/file/1111</script>'''
              '''<style>https://rapidgator.net/file/2222</style>'''
              '''<p>https://rapidgator.net/file/3333</p>'''
              '''<meta http-equiv="refresh" content="https://rapidgator.net/file/4444">''')
        self.assertEqual(rg.extract(page), [
            "https://rapidgator.net/file/9876",
            "https://rapidgator.net/file/3333"])

    def test_reject_unsafe_urls_and_impersonating_domains(self):
        bad=[
            "http://rapidgator.net/file/1234",
            "https://evilrapidgator.net/file/1234",
            "https://rapidgator.net.evil.example/file/1234",
            "https://user@rapidgator.net/file/1234",
            "https://rapidgator.net:9999/file/1234",
            "https://rapidgator.net/notfile/1234",
            "https://rapidgator.net/file/no_spaces!",
            "https://rapidgator.net/file/../../etc/passwd",
        ]
        for text in bad:
            with self.subTest(text=text):
                self.assertEqual(rg.extract(text), [])

    def test_valid_long_alphanumeric_id(self):
        value="a1b2c3d4"*4
        self.assertEqual(rg.extract("https://www.rg.to/file/"+value),[
            "https://rapidgator.net/file/"+value])

    def test_limit_and_dedup_bound(self):
        source="\n".join("https://rg.to/file/"+str(n+1000) for n in range(150))
        self.assertEqual(len(rg.extract(source)),20)
        self.assertEqual(len(rg.extract(source,100)),100)
        with self.assertRaises(ValueError):rg.extract(source,101)
        with self.assertRaises(ValueError):rg.extract(source,True)
        with self.assertRaises(ValueError):rg.extract(source,0)

    def test_input_byte_bound_and_hash(self):
        with self.assertRaises(ValueError):rg.extract("あ"*22000)
        with self.assertRaises(ValueError):rg.digest("あ"*22000)
        self.assertEqual(len(rg.digest("test")),64)
        self.assertNotEqual(rg.digest("test"),rg.digest("Test"))

    def test_no_network_or_javascript_execution(self):
        import inspect
        source=inspect.getsource(rg)
        self.assertNotIn("urlopen(", source)
        self.assertNotIn("requests.get(", source)
        self.assertNotIn("subprocess.run(", source)
        self.assertNotIn("eval(", source)


if __name__=="__main__":
    unittest.main()
