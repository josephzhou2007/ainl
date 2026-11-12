"""Canvas detection with a fake network: an httpx MockTransport and a fake DNS.

The responses copy what the real sites answered on 2026-09-26 (see detect.py's
docstring): UW (/login -> /login/saml -> IdP), UCSB (/login -> a landing page
with /login/saml), UCLA (ucla.instructure.com -> bruinlearn.ucla.edu).
"""
from __future__ import annotations

import json

import httpx
import pytest

import detect
from detect import Http, Rejected

PUBLIC = "128.104.1.1"
UNAUTH = json.dumps({"status": "unauthenticated", "errors": [{"message": "user authorization required"}]})
CANVAS_H = {"content-type": "application/json; charset=utf-8", "x-canvas-meta": "q=1",
            "x-request-context-id": "abc"}


def _canvas_401():
    return httpx.Response(401, headers=CANVAS_H, text=UNAUTH)


def _redirect(to):
    return httpx.Response(302, headers={"location": to})


SITES = {
    ("canvas.wisc.edu", "/api/v1/users/self"): _canvas_401,
    ("canvas.wisc.edu", "/login"): lambda: _redirect("https://canvas.wisc.edu/login/saml"),
    ("canvas.wisc.edu", "/login/saml"): lambda: _redirect(
        "https://login.wisc.edu/idp/profile/SAML2/Redirect/SSO?SAMLRequest=xyz"),
    ("ucsb.instructure.com", "/api/v1/users/self"): _canvas_401,
    ("ucsb.instructure.com", "/login"): lambda: _redirect("https://www.canvas.ucsb.edu"),
    ("www.canvas.ucsb.edu", "/"): lambda: httpx.Response(200, text=(
        '<a href="https://ucsb.instructure.com/login/canvas">Other</a>'
        '<a href="https://ucsb.instructure.com/login/saml">UCSB NetID</a>'
        '<a href="https://evil.example.com/login/saml">not Canvas</a>')),
    ("ucsb.instructure.com", "/login/saml"): lambda: _redirect(
        "https://passport.identity.ucsb.edu/idp/profile/SAML2/Redirect/SSO?x=1"),
    ("ucla.instructure.com", "/api/v1/users/self"): lambda: _redirect(
        "https://bruinlearn.ucla.edu/api/v1/users/self"),
    ("bruinlearn.ucla.edu", "/api/v1/users/self"): _canvas_401,
    ("bruinlearn.ucla.edu", "/login"): lambda: _redirect("https://bruinlearn.ucla.edu/login/saml"),
    ("bruinlearn.ucla.edu", "/login/saml"): lambda: _redirect("https://shb.ais.ucla.edu/idp?x"),
    ("www.wisc.edu", "/api/v1/users/self"): lambda: httpx.Response(404, text="<html>nope</html>"),
}


class World:
    """Fake DNS + fake web. ``requests`` lists every URL actually fetched."""

    def __init__(self, sites=None, dns=None):
        self.sites = {**SITES, **(sites or {})}
        self.dns = dns or {}
        self.requests: list[str] = []

    def resolve(self, host):
        return self.dns.get(host, [PUBLIC])

    def handler(self, request: httpx.Request):
        self.requests.append(str(request.url))
        assert "cookie" not in request.headers
        make = self.sites.get((request.url.host, request.url.path))
        return make() if make else httpx.Response(404, text="not here")

    def http(self):
        return Http(transport=httpx.MockTransport(self.handler), resolve=self.resolve)


# -- the address rules ---------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://canvas.wisc.edu",                    # not https
    "https://127.0.0.1", "https://[::1]", "https://10.0.0.5/login",
    "https://2130706433", "https://0x7f000001", "https://1.2.3.4.5",
    "https://localhost", "https://intranet",     # no dot: not a public name
    "https://user:pw@canvas.wisc.edu", "https://canvas.wisc.edu@evil.com",
    "https://canvas.wisc.edu:8443", "https://canvas.wisc.edu:443", "https://canvas.wisc.edu:",
    "https://canvas.wisc.edu\\@evil.com", "https://canvas%2ewisc.edu",
    "file:///etc/passwd", "javascript:alert(1)", "https://-bad-.edu", "https://a..b.edu",
])
def test_bad_addresses_are_rejected_before_any_request(url):
    with pytest.raises(Rejected):
        detect.check_url(url)


def test_good_addresses_pass():
    assert detect.check_url("https://Canvas.Wisc.EDU/courses?x=1") == "canvas.wisc.edu"
    assert detect.parse_origin("canvas.wisc.edu/courses/123") == "https://canvas.wisc.edu"
    assert detect.parse_origin(" https://ucsb.instructure.com/ ") == "https://ucsb.instructure.com"
    with pytest.raises(Rejected):
        detect.parse_origin("ftp://canvas.wisc.edu")
    with pytest.raises(Rejected):
        detect.parse_origin("canvas wisc edu")


@pytest.mark.parametrize("addrs", [
    ["127.0.0.1"], ["10.1.2.3"], ["192.168.0.10"], ["172.16.5.5"], ["169.254.169.254"],
    ["100.64.0.1"], ["0.0.0.0"], ["::1"], ["fe80::1"], ["fc00::1"], ["::ffff:127.0.0.1"],
    ["224.0.0.1"], ["240.0.0.1"], [], ["not-an-ip"],
    [PUBLIC, "10.0.0.1"],                        # one private answer is enough to refuse
])
def test_private_or_odd_resolutions_are_refused(addrs):
    with pytest.raises(Rejected):
        detect.check_public("canvas.example.edu", lambda h: addrs)


def test_public_resolutions_pass():
    detect.check_public("canvas.example.edu", lambda h: [PUBLIC, "2607:f388::1"])


def test_resolver_errors_are_a_rejection():
    def boom(host):
        raise OSError("nxdomain")
    with pytest.raises(Rejected):
        detect.check_public("nowhere.example.edu", boom)


# -- is it Canvas? --------------------------------------------------------------

def _page(status, headers=None, text=""):
    return detect.Page("https://x.edu/api/v1/users/self", status, httpx.Headers(headers or {}), text)


def test_canvas_rule_needs_401_a_canvas_header_and_a_canvas_body():
    assert detect.is_canvas(_page(401, CANVAS_H, UNAUTH))
    assert detect.is_canvas(_page(401, {"x-request-context-id": "1"}, '{"errors":[]}'))
    assert not detect.is_canvas(_page(401, {}, UNAUTH))                 # body alone
    assert not detect.is_canvas(_page(401, CANVAS_H, "<html>login</html>"))   # header alone
    assert not detect.is_canvas(_page(200, CANVAS_H, UNAUTH))
    assert not detect.is_canvas(_page(404, {}, "<html></html>"))
    assert not detect.is_canvas(_page(401, CANVAS_H, '["unauthenticated"]'))


# -- detection end to end, on the fake web ---------------------------------------

def test_uw_its_saml_redirect_means_start_at_root():
    w = World()
    d = detect.detect("canvas.wisc.edu", http=w.http())
    assert (d.origin, d.start_path, d.domains) == ("https://canvas.wisc.edu", "/", ("wisc.edu",))
    # The IdP is only read from the Location header, never fetched.
    assert not any("login.wisc.edu" in u for u in w.requests)


def test_ucsb_its_landing_page_means_start_at_the_saml_button():
    w = World()
    d = detect.detect("https://ucsb.instructure.com/courses/5", http=w.http())
    assert d.origin == "https://ucsb.instructure.com"
    assert d.start_path == "/login/saml"           # not /login/canvas, not the other host's link
    assert d.domains == ("ucsb.edu",)              # instructure.com is everyone's


def test_a_directory_hint_picks_between_providers():
    w = World({("www.canvas.ucsb.edu", "/"): lambda: httpx.Response(200, text=(
        '<a href="https://ucsb.instructure.com/login/saml">A</a>'
        '<a href="https://ucsb.instructure.com/login/openid_connect">B</a>'))})
    assert detect.detect("ucsb.instructure.com", hint="openid_connect",
                         http=w.http()).start_path == "/login/openid_connect"
    assert detect.detect("ucsb.instructure.com", hint="nonsense",
                         http=w.http()).start_path == "/login/saml"


def test_a_vanity_domain_becomes_the_canvas_origin():
    w = World()
    d = detect.detect("ucla.instructure.com", http=w.http())
    assert d.origin == "https://bruinlearn.ucla.edu" and d.start_path == "/"
    assert d.domains == ("ucla.edu",)


def test_a_non_canvas_site_is_refused():
    with pytest.raises(Rejected, match="Canvas"):
        detect.detect("www.wisc.edu", http=World().http())


def test_a_site_resolving_privately_is_never_fetched():
    w = World(dns={"canvas.evil.edu": ["127.0.0.1"]})
    with pytest.raises(Rejected, match="public"):
        detect.detect("canvas.evil.edu", http=w.http())
    assert w.requests == []


def test_every_redirect_target_is_checked_again():
    # A public site that redirects to one resolving to the metadata address.
    w = World({("canvas.trick.edu", "/api/v1/users/self"): lambda: _redirect(
        "https://metadata.trick.edu/latest/meta-data/")},
        dns={"metadata.trick.edu": ["169.254.169.254"]})
    with pytest.raises(Rejected):
        detect.detect("canvas.trick.edu", http=w.http())
    assert not any("metadata" in u for u in w.requests)
    for target in ("http://canvas.trick.edu/x", "https://10.0.0.1/", "https://canvas.trick.edu:8080/"):
        w = World({("canvas.trick.edu", "/api/v1/users/self"): lambda t=target: _redirect(t)})
        with pytest.raises(Rejected):
            detect.detect("canvas.trick.edu", http=w.http())
        assert len(w.requests) == 1


def test_redirect_loops_stop():
    w = World({("loop.example.edu", "/api/v1/users/self"): lambda: _redirect(
        "https://loop.example.edu/api/v1/users/self")})
    with pytest.raises(Rejected, match="redirects"):
        detect.detect("loop.example.edu", http=w.http())
    assert len(w.requests) == detect.MAX_HOPS + 1


def test_a_landing_page_on_a_private_address_just_means_start_at_root():
    w = World(dns={"www.canvas.ucsb.edu": ["10.0.0.9"]})
    d = detect.detect("ucsb.instructure.com", http=w.http())
    assert d.start_path == "/"
    assert not any("www.canvas.ucsb.edu" in u for u in w.requests)


def test_unreachable_is_a_rejection():
    def down(request):
        raise httpx.ConnectError("down")
    h = Http(transport=httpx.MockTransport(down), resolve=lambda h: [PUBLIC])
    with pytest.raises(Rejected, match="reach"):
        detect.detect("canvas.wisc.edu", http=h)


# -- the directory ---------------------------------------------------------------

def test_directory_results_are_parsed_and_bad_domains_dropped():
    rows = [{"name": "UCLA", "domain": "bruinlearn.ucla.edu", "authentication_provider": None},
            {"name": "Evil", "domain": "127.0.0.1"},
            {"name": "Evil2", "domain": "x.edu/../y"},
            {"name": "Port", "domain": "canvas.x.edu:8443"},
            {"name": "Stanford", "domain": "Canvas.Stanford.edu", "authentication_provider": "saml"},
            "junk"]
    seen = {}

    def handler(request):
        seen.update(request.url.params)
        return httpx.Response(200, json=rows)
    h = Http(transport=httpx.MockTransport(handler), resolve=lambda x: [PUBLIC])
    out = detect.search_directory("ucla", http=h)
    assert seen == {"search_term": "ucla", "per_page": "10"}
    assert out == [{"name": "UCLA", "domain": "bruinlearn.ucla.edu", "authentication_provider": ""},
                   {"name": "Stanford", "domain": "canvas.stanford.edu", "authentication_provider": "saml"}]


def test_directory_trouble_is_an_empty_list():
    h = Http(transport=httpx.MockTransport(lambda r: httpx.Response(503)), resolve=lambda x: [PUBLIC])
    assert detect.search_directory("ucla", http=h) == []


def test_site_of():
    assert detect.site_of("login.wisc.edu") == "wisc.edu"
    assert detect.site_of("passport.identity.ucsb.edu") == "ucsb.edu"
    assert detect.site_of("www.ox.ac.uk") == "ox.ac.uk"
