"""Validate a user-chosen HTTPS browsing destination without fetching it.

Only the user's own browser loads third-party pages in an isolated iframe.
No server HTTP proxy, remote source-serving, credential forwarding, DNS
resolution, redirect fallback or headless automation is provided.
"""
import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

DOMAIN_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
DENIED_TLDS = {"local", "localhost", "lan", "internal", "home", "arpa",
               "onion", "test", "invalid"}
MAX_URL = 2048


def validate(url):
    if not isinstance(url, str) or not 1 <= len(url) <= MAX_URL:
        raise ValueError("URLは2048文字以内のHTTPSアドレスを指定してください")
    if any(ord(c) < 32 or ord(c) == 127 or c.isspace() for c in url):
        raise ValueError("URLには空白や制御文字を含められません")
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        raise ValueError("URLのホスト名が不正です") from None
    if (parts.scheme.lower() != "https" or not parts.netloc or
            parts.username is not None or parts.password is not None or
            port not in (None, 443) or not hostname):
        raise ValueError("認証情報や特殊なポートを含まない公開HTTPS URLだけを許可します")
    hostname = hostname.lower()
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("IPアドレスへの埋め込みアクセスは許可しません")
    labels = hostname.split(".")
    if (len(labels) < 2 or labels[-1] in DENIED_TLDS or
            not re.fullmatch("[a-z]{2,63}", labels[-1]) and
            not labels[-1].startswith("xn--") or
            any(DOMAIN_LABEL.fullmatch(label) is None for label in labels)):
        raise ValueError("公開DNSドメインのHTTPS URLを指定してください")
    if parts.path.startswith("//") or "\\" in url:
        raise ValueError("紛らわしいURL表記は許可されません")
    # Keep the user-selected URL, including path/query, without following it.
    netloc = hostname + (":443" if port == 443 else "")
    return urlunsplit(("https", netloc, parts.path, parts.query, parts.fragment))
