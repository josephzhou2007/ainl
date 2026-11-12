"""Is this address a school's Canvas, and where does its login start?

People add schools themselves — by picking a result from Instructure's public
school directory, or by pasting an address — and the server then opens that
site in a browser. So nothing here trusts the address: before any request,
and again for every redirect,

    https only, a DNS hostname (no IP literals, no port, no userinfo), and every
    address it resolves to is public (no loopback, private, link-local,
    carrier-grade NAT, multicast or reserved ranges): our server must never be
    talked into fetching something on its own network.

Then the site has to prove it is Canvas. Observed 2026-09-26, unauthenticated
``GET /api/v1/users/self``:

    canvas.wisc.edu, ucsb.instructure.com, bruinlearn.ucla.edu
        401, application/json, {"status":"unauthenticated","errors":[{"message":
        "user authorization required"}]}, headers x-canvas-meta and
        x-request-context-id (and x-request-processor, x-runtime)
    ucla.instructure.com
        302 to bruinlearn.ucla.edu/api/v1/users/self (a vanity domain: the
        school's Canvas is the host that finally answers)
    www.wisc.edu, example.com
        404 text/html

Rule: 401 + a Canvas JSON error body (status "unauthenticated" or an
"errors" list) + at least one Canvas header. Both, not either: a page can
copy a body, and plenty of servers send an x-request-id-like header.

Where the login starts, from ``GET /login`` (redirects followed on the Canvas
host only):

    canvas.wisc.edu      /login -> /login/saml -> login.wisc.edu      => "/"
    bruinlearn.ucla.edu  /login -> /login/saml -> shb.ais.ucla.edu    => "/"
    ucsb.instructure.com /login -> www.canvas.ucsb.edu (a landing page whose
                         "UCSB NetID" button is /login/saml)          => "/login/saml"

i.e. if Canvas's own /login/<provider> sends people to their identity
provider, "/" gets there by itself; if /login sends them to a landing page (or
shows a page) with /login/<provider> links, start at the SSO one. The
directory's ``authentication_provider`` picks between several links.

Everything that touches the network is injectable (an httpx transport and a
resolver), so the rules are tested with fakes.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional
from urllib.parse import urljoin, urlsplit

import httpx

log = logging.getLogger("ainl.scaleup.detect")

DIRECTORY = "https://canvas.instructure.com/api/v1/accounts/search"
CANVAS_HEADERS = ("x-canvas-meta", "x-request-context-id", "x-canvas-user-id")
MAX_HOPS = 5
MAX_BODY = 512 * 1024
# Hosts every school shares; never "the school's own" domain. (Hosted identity
# providers too: sbcc.instructure.com signs in on login.microsoftonline.com.)
COMMON = ("instructure.com", "canvaslms.com", "duosecurity.com", "duo.com",
          "microsoftonline.com", "okta.com", "google.com", "auth0.com", "onelogin.com",
          "clever.com")

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_PROVIDER = re.compile(r"^/login/([a-z0-9_]+)/?$")
_HREF = re.compile(r"""href\s*=\s*["']([^"'<>\s]+)["']""", re.I)
# Canvas's own password forms: a link to these is not "the school's SSO".
_NOT_SSO = ("canvas", "ldap")


class Rejected(Exception):
    """Not an address we will fetch or open. ``str(e)`` is fit to show the person."""


# --------------------------------------------------------------------------- #
# The address rules
# --------------------------------------------------------------------------- #

def check_url(url: str) -> str:
    """The hostname of ``url`` if its form is acceptable; Rejected otherwise.
    (Where it resolves to is check_public's job.)"""
    try:
        u = urlsplit(url.strip())
        port = u.port
    except ValueError:
        raise Rejected("That doesn't look like a web address.")
    if u.scheme.lower() != "https":
        raise Rejected("Only https:// addresses.")
    if "@" in u.netloc or u.username or u.password:
        raise Rejected("That address has a user name in it.")
    if port is not None or u.netloc.rstrip("]").endswith(":"):
        raise Rejected("That address has a port number in it.")
    host = (u.hostname or "").rstrip(".")
    if "\\" in u.netloc or "%" in u.netloc:
        raise Rejected("That doesn't look like a web address.")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise Rejected("That doesn't look like a web address.")
    try:
        ipaddress.ip_address(host.strip("[]"))
        raise Rejected("Use the school's web address, not an IP address.")
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) < 2 or len(host) > 253 or not all(_LABEL.match(x) for x in labels):
        raise Rejected("That doesn't look like a web address.")
    if labels[-1].isdigit() or labels[-1].startswith("0x"):
        raise Rejected("Use the school's web address, not an IP address.")
    return host


def _public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped is not None:
        a = a.ipv4_mapped
    return a.is_global and not (a.is_multicast or a.is_reserved or a.is_private
                                or a.is_loopback or a.is_link_local or a.is_unspecified)


def default_resolve(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({i[4][0] for i in infos})


def check_public(host: str, resolve: Callable[[str], Iterable[str]] = default_resolve) -> None:
    """Every address the host resolves to must be public — one private answer
    among public ones is still a way in."""
    try:
        addrs = list(resolve(host))
    except OSError:
        raise Rejected("I couldn't find that address.")
    if not addrs:
        raise Rejected("I couldn't find that address.")
    bad = [a for a in addrs if not _public(a)]
    if bad:
        log.warning("refused %s: resolves to a non-public address", host)
        raise Rejected("That address isn't on the public internet.")


def parse_origin(text: str) -> str:
    """What someone pasted -> ``https://host`` (path, query dropped). A bare
    host gets https:// in front; any other scheme is refused."""
    t = (text or "").strip().strip("<>").strip()
    if not t or len(t) > 300 or any(c.isspace() for c in t):
        raise Rejected("Send just the address, like canvas.myschool.edu")
    if "://" not in t:
        t = "https://" + t
    host = check_url(t)
    return "https://" + host


def site_of(host: str) -> str:
    """The registrable part of a host, roughly: login.wisc.edu -> wisc.edu,
    www.ox.ac.uk -> ox.ac.uk. Only used to seed a school's login domains."""
    labels = (host or "").lower().rstrip(".").split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in (
            "ac", "co", "edu", "gov", "org", "com", "net", "sch"):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# --------------------------------------------------------------------------- #
# Fetching, one checked hop at a time
# --------------------------------------------------------------------------- #

@dataclass
class Page:
    url: str
    status: int
    headers: httpx.Headers
    text: str

    @property
    def location(self) -> Optional[str]:
        if self.status in (301, 302, 303, 307, 308):
            loc = self.headers.get("location")
            return urljoin(self.url, loc) if loc else None
        return None


class Http:
    """GETs with no cookies, short timeouts, a size cap, and the address rules
    applied before every hop (redirects are followed by hand, at most MAX_HOPS)."""

    def __init__(self, transport: Optional[httpx.BaseTransport] = None,
                 resolve: Callable[[str], Iterable[str]] = default_resolve,
                 timeout: float = 5.0, max_hops: int = MAX_HOPS) -> None:
        self.resolve, self.max_hops = resolve, max_hops
        self._client = httpx.Client(transport=transport, follow_redirects=False,
                                    timeout=httpx.Timeout(timeout, connect=3.0),
                                    headers={"User-Agent": "ainl-school-check/1"})

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def hop(self, url: str, headers: Optional[dict] = None, params: Optional[dict] = None) -> Page:
        """One request, no redirect followed."""
        host = check_url(url)
        check_public(host, self.resolve)
        self._client.cookies.clear()           # nothing carries from one site to the next
        try:
            with self._client.stream("GET", url, headers=headers, params=params) as r:
                # Where we actually connected (DNS can change between the check
                # and the connect): refuse to use an answer from a private address.
                stream = r.extensions.get("network_stream")
                peer = stream.get_extra_info("server_addr") if stream is not None else None
                if peer and not _public(str(peer[0])):
                    raise Rejected("That address isn't on the public internet.")
                body = b""
                for chunk in r.iter_bytes():
                    body += chunk
                    if len(body) > MAX_BODY:
                        break
                final = str(r.url)
                return Page(final, r.status_code, r.headers,
                            body[:MAX_BODY].decode(r.encoding or "utf-8", "replace"))
        except httpx.HTTPError as e:
            raise Rejected(f"I couldn't reach {host}.") from e

    def get(self, url: str, headers: Optional[dict] = None,
            params: Optional[dict] = None) -> tuple[Page, list[str]]:
        """Follow redirects, re-checking each target. (final page, urls visited)."""
        seen = []
        for _ in range(self.max_hops + 1):
            p = self.hop(url, headers, params)
            params = None                        # only the first request carries them
            seen.append(p.url)
            if p.location is None:
                return p, seen
            url = p.location
        raise Rejected("That address redirects too many times.")


# --------------------------------------------------------------------------- #
# Canvas or not, and where its login starts
# --------------------------------------------------------------------------- #

def is_canvas(p: Page) -> bool:
    if p.status != 401:
        return False
    if not any(h in p.headers for h in CANVAS_HEADERS):
        return False
    try:
        body = json.loads(p.text)
    except ValueError:
        return False
    return isinstance(body, dict) and (body.get("status") == "unauthenticated"
                                       or isinstance(body.get("errors"), list))


@dataclass
class Detected:
    origin: str                                  # the Canvas API host, https://host
    start_path: str = "/"
    domains: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)   # what was seen, for logs and the owner


def _providers(html: str, base: str, canvas_host: str) -> list[str]:
    out: list[str] = []
    for href in _HREF.findall(html or ""):
        u = urlsplit(urljoin(base, href.replace("&amp;", "&")))
        m = _PROVIDER.match(u.path)
        if u.scheme == "https" and (u.hostname or "").lower() == canvas_host and m:
            if m.group(1) not in out:
                out.append(m.group(1))
    return out


def _pick(providers: list[str], hint: Optional[str]) -> Optional[str]:
    h = (hint or "").strip().lower()
    if h and h in providers:
        return h
    sso = [p for p in providers if p not in _NOT_SSO]
    if "saml" in sso:
        return "saml"
    return sso[0] if sso else None


def _login_start(http: Http, origin: str, hint: Optional[str], notes: list[str]
                 ) -> tuple[str, list[str]]:
    """(start path, hosts seen on the way to the identity provider)."""
    host = urlsplit(origin).hostname
    url, seen = origin + "/login", []
    page = None
    for _ in range(http.max_hops):
        p = http.hop(url)
        nxt = p.location
        if nxt is None:
            page = p
            break
        nh = (urlsplit(nxt).hostname or "").lower()
        if nh == host:
            notes.append(f"{urlsplit(url).path} -> {urlsplit(nxt).path}")
            url = nxt
            continue
        seen.append(nh)
        left = urlsplit(url).path
        notes.append(f"{left} -> {nh}")
        if _PROVIDER.match(left):
            # Canvas's own provider endpoint already goes to the IdP: "/" does too.
            return "/", seen
        try:                                    # /login itself left Canvas: a landing page
            page, _ = http.get(nxt)
        except Rejected as e:
            notes.append(f"landing page unreadable: {e}")
            return "/", seen
        break
    if page is None:
        return "/", seen
    found = _providers(page.text, page.url, host)
    notes.append(f"login links on {urlsplit(page.url).hostname}: {found or 'none'}")
    choice = _pick(found, hint)
    if choice is None:
        return "/", seen
    try:                                        # where that button goes, to learn the IdP
        idp = http.hop(f"{origin}/login/{choice}").location
        if idp and urlsplit(idp).hostname and urlsplit(idp).hostname != host:
            seen.append(urlsplit(idp).hostname.lower())
            notes.append(f"/login/{choice} -> {urlsplit(idp).hostname}")
    except Rejected:
        pass
    return f"/login/{choice}", seen


def detect(address: str, *, hint: Optional[str] = None, http: Optional[Http] = None) -> Detected:
    """Prove ``address`` is a Canvas and work out how its login starts.
    Raises Rejected (with a sentence for the person) otherwise."""
    own = http is None
    http = http or Http()
    try:
        origin = parse_origin(address)
        page, seen = http.get(origin + "/api/v1/users/self", headers={"Accept": "application/json"})
        if not is_canvas(page):
            log.info("%s is not Canvas: /api/v1/users/self answered %s", origin, page.status)
            raise Rejected("That doesn't look like a Canvas site. Send the address you "
                           "open Canvas at (the page with your courses).")
        final = urlsplit(page.url)
        canvas = f"https://{final.hostname}"
        notes = [f"api {page.status} canvas" + (f" (via {origin})" if canvas != origin else "")]
        start, idp_hosts = _login_start(http, canvas, hint, notes)
        domains: list[str] = []
        for h in [final.hostname or ""] + idp_hosts:
            s = site_of(h)
            if s and s not in COMMON and not any(s == c or s.endswith("." + c) for c in COMMON) \
                    and s not in domains:
                domains.append(s)
        return Detected(canvas, start, tuple(domains), notes)
    finally:
        if own:
            http.close()


# --------------------------------------------------------------------------- #
# Instructure's "Find your school" directory
# --------------------------------------------------------------------------- #

def search_directory(term: str, *, http: Optional[Http] = None, limit: int = 10) -> list[dict]:
    """[{name, domain, authentication_provider}] from Instructure's public
    directory (what the Canvas app's "Find your school" uses). Entries whose
    domain isn't a plain hostname are dropped. Network trouble -> []."""
    own = http is None
    http = http or Http()
    try:
        page, _ = http.get(DIRECTORY, headers={"Accept": "application/json"},
                           params={"search_term": term[:80], "per_page": limit})
        rows = json.loads(page.text) if page.status == 200 else []
    except (Rejected, ValueError) as e:
        log.warning("school directory search failed: %s", e)
        return []
    finally:
        if own:
            http.close()
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not isinstance(r.get("name"), str):
            continue
        domain = str(r.get("domain") or "").lower().strip().rstrip("/")
        try:
            if check_url("https://" + domain) != domain:
                continue
        except Rejected:
            continue
        prov = r.get("authentication_provider")
        out.append({"name": r["name"].strip(), "domain": domain,
                    "authentication_provider": prov if isinstance(prov, str) else ""})
    return out
