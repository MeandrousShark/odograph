from __future__ import annotations

from html.parser import HTMLParser


class _AuthorizationLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.href = None

    def handle_starttag(self, tag, attrs):
        if tag == "a" and dict(attrs).get("id") == "oidc-authorization-link":
            self.href = dict(attrs).get("href")


def oidc_authorization_url(response) -> str:
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store, private"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "location" not in response.headers
    parser = _AuthorizationLinkParser()
    body = response.body if hasattr(response, "body") else response.content
    parser.feed(body.decode("utf-8"))
    assert parser.href
    return parser.href
