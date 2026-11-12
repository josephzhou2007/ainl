"""Gradescope, through the student's own browser session.

Read on the live page 2026-09-21 (PHYSICS 247, Problem Set 3):

  - Getting there. Canvas's sessionless_launch — which is how WebAssign is
    reached — answers 403 for this tool (LTI 1.3). What works is what the
    student's own click does: the assignment's Canvas page carries a hidden
    form that POSTs to the tool's OIDC login, targeted at a new window. We
    submit that same form in the current tab. It lands on
    gradescope.com/courses/…/assignments/…/submissions/new with no password
    asked — but it needs the Canvas LOGIN in the browser, not just a token.
  - The page offers "Submit Images" (one per question) and "Submit PDF" (one
    file, pages assigned to questions afterwards). PDF is a single file input,
    `#submission_pdf_attachment`, and an "Upload PDF" button.
  - NOT yet seen: what follows the upload (the page-to-question assignment).
    upload_pdf reports where it landed and sends a picture, and the bot tells
    the student that step is still theirs.

One tab only: opening a second tab in this headless browser hangs often enough
(three times on the day this was written) that the existing tab is borrowed and
handed back to WebAssign afterwards.

Same line as everywhere else: this moves the student's own pages, and only
after they confirmed in chat.
"""
from __future__ import annotations

import base64
import logging
from io import BytesIO
from urllib.parse import urlsplit

from ..util import browserthread
from ..util.timing import span, step
from .webassign import CDP_URL, WA_LIST

log = logging.getLogger("ainl.gradescope")

_FORM_READY = "() => [...document.forms].some(f => f.action.includes('/oidc/login'))"
_LAUNCH = ("() => { const f = [...document.forms].find(f => f.action.includes('/oidc/login'));"
           " f.target = '_self'; f.submit(); }")
_FILE_INPUT = "#submission_pdf_attachment"
# Once "Submit PDF" is open, the upload form posts to …/submissions (before, to
# the page itself). Checked on the path only.
_FORM_POSTS_TO_SUBMISSIONS = ("(sel) => { const i = document.querySelector(sel);"
                              " return !!(i && i.form && new URL(i.form.action, location.href)"
                              ".pathname.endsWith('/submissions')); }")
_SUBMITTED = __import__("re").compile(r"/submissions/\d+")


def _is_404(tab) -> bool:
    try:
        return bool(tab.evaluate("() => /couldn.t find anything at this address/i"
                                 ".test(document.body ? document.body.innerText.slice(0, 2000) : '')"))
    except Exception:
        return False


class GradescopeUnavailable(RuntimeError):
    """Couldn't get to the upload page. The message says what to do."""


def as_one_pdf(files: list[tuple[str, str, bytes]]) -> tuple[str, bytes]:
    """What the student sent -> (filename, one PDF).

    ``files`` are (filename, content_type, bytes) in the order they were sent.
    A single PDF goes up as it is. Photos are bound into one PDF, one page
    each, in that order — the order is the student's, and is never changed.
    A mix cannot be merged without a PDF library, so it is refused out loud.
    """
    if not files:
        raise ValueError("No files to upload.")
    pdfs = [f for f in files if f[1] == "application/pdf" or f[0].lower().endswith(".pdf")]
    if len(files) == 1 and pdfs:
        return files[0][0], files[0][2]
    if pdfs:
        raise ValueError("I can upload ONE PDF, or photos — not a mix, and not several PDFs.")
    from PIL import Image, ImageOps

    pages = []
    for name, _, data in files:
        try:
            img = ImageOps.exif_transpose(Image.open(BytesIO(data)))   # phone photos carry rotation
            pages.append(img.convert("RGB"))
        except Exception as e:
            raise ValueError(f"{name} isn't an image I can read.") from e
    buf = BytesIO()
    pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:], resolution=150.0)
    return f"pages-{len(pages)}.pdf", buf.getvalue()


def _host_path(url: str) -> str:
    p = urlsplit(url)
    return f"{p.netloc}{p.path}"


class Gradescope:
    def __init__(self, cdp_url: str = CDP_URL):
        self.cdp_url = cdp_url

    def upload_pdf(self, canvas_assignment_url: str, filename: str, pdf: bytes) -> dict:
        """Launch from the Canvas page, upload, report where it landed.

        Returns {"title", "landed" (host+path only), "shot" (PNG or None)}.
        """
        def go():
            from ..util import pw
            with pw.connect(self.cdp_url) as browser:
                pages = [p for p in browser.contexts[0].pages if not p.is_closed()]
                if not pages:
                    raise GradescopeUnavailable("The browser has no tab to work in.")
                tab = pages[0]
                try:
                    return self._upload(tab, canvas_assignment_url, filename, pdf)
                finally:
                    try:                       # hand the tab back to WebAssign
                        tab.goto(WA_LIST, wait_until="domcontentloaded", timeout=20_000)
                    except Exception:
                        log.info("could not return the tab to WebAssign")

        with step("gs.upload", budget=120, cat="network"):
            try:
                return browserthread.run(go, budget=120)
            except browserthread.Timeout as e:
                raise GradescopeUnavailable(
                    "Gradescope didn't answer in two minutes. I don't know whether the upload "
                    "went through — check the page before trying again.") from e

    @staticmethod
    def _upload(tab, canvas_url: str, filename: str, pdf: bytes) -> dict:
        with span("gs.canvas_page", "network"):
            tab.goto(canvas_url, wait_until="domcontentloaded", timeout=45_000)
            try:
                tab.wait_for_function(_FORM_READY, timeout=20_000, polling=300)
            except Exception as e:
                raise GradescopeUnavailable(
                    "Canvas didn't show the Gradescope launch — the Canvas login in the browser "
                    "may have expired (python scripts/browser_start.py --show, sign in).") from e
        with span("gs.launch", "network"):
            tab.evaluate(_LAUNCH)
            for _ in range(40):
                tab.wait_for_timeout(1000)
                if urlsplit(tab.url).netloc.endswith("gradescope.com"):
                    break
            else:
                raise GradescopeUnavailable("The launch from Canvas never reached Gradescope.")
        with span("gs.find_input", "ours"):
            try:
                tab.wait_for_selector(_FILE_INPUT, state="attached", timeout=15_000)
            except Exception as e:
                from .. import jev
                jev.shadow(tab, "Open the PDF upload for this assignment so a new PDF can be handed "
                                "in (on an assignment already submitted, that is Resubmit).",
                           "gradescope: no PDF upload")
                raise GradescopeUnavailable(
                    f"Gradescope opened ({_host_path(tab.url)}) but showed no PDF upload — it may "
                    "already have a submission, or be closed. Nothing was uploaded.") from e
        before = tab.url
        with span("gs.open_pdf", "ours"):
            # Open the "Submit PDF" step first, the way a student does. Until
            # it is open the form posts back to /submissions/new itself, which
            # answers 404 — how an upload failed on 2026-09-26 (the page had
            # changed to offer "Submit Images" / "Submit PDF" first).
            opener = tab.get_by_role("button", name="Submit PDF")
            if opener.count() and opener.first.is_visible():
                opener.first.click()
            try:
                tab.wait_for_function(_FORM_POSTS_TO_SUBMISSIONS, arg=_FILE_INPUT, timeout=10_000)
            except Exception as e:
                from .. import jev
                jev.shadow(tab, "Open the PDF upload for this assignment so a new PDF can be handed "
                                "in (on an assignment already submitted, that is Resubmit).",
                           "gradescope: Submit PDF did not open")
                raise GradescopeUnavailable(
                    "Gradescope's PDF upload didn't open the way I expected, so I stopped before "
                    "uploading. Nothing was submitted.") from e
        with span("gs.upload", "network", kb=len(pdf) // 1024):
            tab.set_input_files(_FILE_INPUT, files=[{"name": filename, "mimeType": "application/pdf",
                                                     "buffer": pdf}])
            # A real click on the visible "Upload PDF", as a student would.
            tab.evaluate("(sel) => document.querySelector(sel).form.querySelector('[type=submit]')"
                         ".scrollIntoView({block: 'center'})", _FILE_INPUT)
            tab.locator(_FILE_INPUT).locator("xpath=ancestor::form//*[@type='submit']").first.click()
            for _ in range(90):
                tab.wait_for_timeout(1000)
                if tab.url != before or _SUBMITTED.search(urlsplit(tab.url).path):
                    break
            tab.wait_for_timeout(2500)
        shot = None
        try:
            with span("gs.shot", "render"):
                cdp = tab.context.new_cdp_session(tab)
                shot = base64.b64decode(cdp.send("Page.captureScreenshot", {"format": "png"})["data"])
                cdp.detach()
        except Exception as e:
            log.info("no picture of the result: %s", str(e)[:80])
        title = (tab.title() or "")[:120]
        path = urlsplit(tab.url).path
        if _SUBMITTED.search(path):
            status = "uploaded"            # a submission exists: /submissions/<id>
        elif tab.url == before or "404" in title or _is_404(tab):
            status = "failed"              # still the form, or Gradescope's 404 page
        else:
            status = "unknown"
        log.info("gradescope: %s (%d KB), landed on %s", status, len(pdf) // 1024, _host_path(tab.url))
        return {"title": title, "landed": _host_path(tab.url), "status": status,
                "moved": status == "uploaded", "shot": shot}
