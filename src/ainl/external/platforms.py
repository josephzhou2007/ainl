"""Where Canvas hands the student off to — the registry behind the ↗ marker.

Every entry was observed in real traffic (the author's private traffic-capture notes (not included),
docs/cengage-webassign-api.md). `tier` is the L7 submission tier from ARCHITECTURE.md:

    A  we submit for the student through an official API
    B  we work inside the student's own logged-in browser session: enter the
       answers THEY produced, show them the rendered result, and submit only
       after they confirm in chat
    C  we do not act; we deep-link the student to the exact page

The line is not "graded vs. ungraded" — it was, and WebAssign sat in C because
of it, until the student pointed out that sending them back to the browser for
the last click restored the very middle layer this removes. The line is: the
agent moves work the student did, never writes it, never submits unconfirmed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse


@dataclass(frozen=True)
class Platform:
    key: str
    label: str
    tier: str                  # A | B | C
    lti: Optional[str]         # "1.1" | "1.3" | None (plain link)
    hosts: tuple[str, ...]     # host suffixes
    needs_popup: bool = False  # launch breaks under a popup blocker unless retargeted to _self
    note: str = ""


PLATFORMS: tuple[Platform, ...] = (
    Platform("webassign", "WebAssign", "B", "1.1",
             ("webassign.net", "cengage.com", "cengageorg.my.site.com"),
             note="reached via Canvas sessionless_launch (no vendor password); answers "
                  "go into RA_/RC_/RS_ fields — see external/webassign.py"),
    Platform("connect", "McGraw Hill", "C", "1.3",
             ("mheducation.com",), needs_popup=True,
             note="10-hop launch ending at newconnect…/student/todo; window.open('', 'ltipaircourse')"),
    # Turnitin owns Gradescope, and the LTI tool lives on their host — Canvas
    # points PHYSICS 247's Problem Set 3 at lti-gradescope.int.turnitin.com,
    # not at gradescope.com (observed 2026-09-19). Missing that host made the
    # assignment classify as "unknown external site".
    Platform("gradescope", "Gradescope", "B", "1.3",
             ("gradescope.com", "lti-gradescope.int.turnitin.com", "turnitin.com"),
             note="no public API; uploads the student's own pages. Two modes: one "
                  "image per problem, or a single PDF whose pages are then assigned "
                  "to questions."),
    # ECON101 (2026-09-26): LearningCurve, EOC problems, podcasts — all behind
    # Macmillan's LMS link host. Unknown until then, so each one was first
    # tried as WebAssign: 30 s to "no WebAssign link".
    Platform("macmillan", "Macmillan Achieve", "C", "1.1",
             ("bfwpub.com", "macmillanlearning.com")),
    # Canvas New Quizzes run as an LTI tool on a per-school Instructure host,
    # e.g. ucsb.quiz-lti-iad-prod.instructure.com (seen 2026-09-27).
    Platform("newquizzes", "Canvas New Quizzes", "C", "1.3",
             ("quiz-lti-iad-prod.instructure.com", "quiz-lti-pdx-prod.instructure.com",
              "quiz-lti-dub-prod.instructure.com", "quiz-lti-syd-prod.instructure.com",
              "quiz-lti-fra-prod.instructure.com", "quiz-lti-yul-prod.instructure.com",
              "quiz-lti-sin-prod.instructure.com")),
    Platform("gdocs", "Google Docs", "C", None, ("docs.google.com", "drive.google.com"),
             note="rejects embedded webviews (signin/rejected); real Chrome only"),
    Platform("box", "Box", "C", None, ("box.com",), note="public folders readable without login"),
    Platform("pressbooks", "Pressbooks", "C", None, ("unizin.org", "pressbooks.pub")),
    Platform("piazza", "Piazza", "C", "1.1", ("piazza.com",)),
    Platform("registerblast", "Testing Center", "C", "1.1", ("registerblast.com",)),
)


def classify(url_or_host: Optional[str]) -> Optional[Platform]:
    if not url_or_host:
        return None
    host = urlparse(url_or_host).netloc if "//" in url_or_host else url_or_host
    host = host.lower().split(":")[0]
    for p in PLATFORMS:
        if any(host == h or host.endswith("." + h) for h in p.hosts):
            return p
    return None
