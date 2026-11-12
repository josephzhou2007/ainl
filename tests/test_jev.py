"""Jev as a chooser: our reader finds, Jev picks, unsure means stop."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ainl import jev

CASES = Path(__file__).parent / "jev_cases"

CANDS = [
    {"index": "1", "label": "Submit Answer", "role": "button", "visible": True, "disabled": False,
     "context": "1. Find the general solution", "frame": "", "href": "", "file": False, "truth": False},
    {"index": "2", "label": "Submit Answer", "role": "button", "visible": True, "disabled": False,
     "context": "3. Use Euler's method", "frame": "", "href": "", "file": False, "truth": True},
]


def _client(answer: dict, seen: list, status: int = 200) -> httpx.Client:
    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        return httpx.Response(status, json={"answers": {"target": answer}, "model": "jev-test",
                                            "usage": {"input_tokens": 321}})
    return httpx.Client(transport=httpx.MockTransport(handle))


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key")
    monkeypatch.delenv("AINL_JEV_MIN_CONF", raising=False)


def test_a_sure_right_choice():
    seen = []
    got = jev.choose("Submit question 3", CANDS, {"url": "https://x.test/"},
                     client=_client({"choice": "2", "probabilities": {"1": 0.04, "2": 0.96},
                                     "confidence": 0.93}, seen))
    assert got["index"] == "2" and got["sure"] and got["probability"] == 0.96
    assert got["model"] == "jev-test" and got["usage"] == {"input_tokens": 321}
    body = seen[0]
    q = body["questions"]["target"]
    assert q["type"] == "choice" and set(q["criteria"]) == {"1", "2"}
    assert q["criteria"]["2"]["context"] == "3. Use Euler's method"
    assert "truth" not in json.dumps(body)                  # the answer key is never sent


def test_unsure_means_do_not_act(monkeypatch):
    got = jev.choose("x", CANDS, client=_client({"choice": "1", "probabilities": {"1": 0.55, "2": 0.45},
                                                 "confidence": 0.41}, []))
    assert got["index"] == "1" and got["sure"] is False
    monkeypatch.setenv("AINL_JEV_MIN_CONF", "0.3")
    got = jev.choose("x", CANDS, client=_client({"choice": "1", "probabilities": {"1": 0.55, "2": 0.45},
                                                 "confidence": 0.41}, []))
    assert got["sure"] is True


@pytest.mark.parametrize("answer", [
    {"choice": "7", "probabilities": {"1": 0.5, "2": 0.5}, "confidence": 0.9},        # not offered
    {"choice": "1", "probabilities": {"1": 0.2, "2": 0.8}, "confidence": 0.9},        # not its best
    {"choice": "1", "probabilities": {"1": 0.9}, "confidence": 0.9},                  # options missing
    {"choice": "1", "probabilities": {"1": 0.9, "2": 0.9}, "confidence": 0.9},        # sums to 1.8
    {},
])
def test_an_answer_that_does_not_fit_chooses_nothing(answer):
    with pytest.raises(jev.JevError):
        jev.choose("x", CANDS, client=_client(answer, []))


def test_http_errors_and_no_key_choose_nothing(monkeypatch):
    with pytest.raises(jev.JevError, match="HTTP 401"):
        jev.choose("x", CANDS, client=_client({}, [], status=401))
    monkeypatch.delenv("TYPESAFE_API_KEY")
    assert not jev.available()
    with pytest.raises(jev.JevError, match="TYPESAFE_API_KEY"):
        jev.choose("x", CANDS)


def test_our_reader_finds_every_right_element_in_the_trial_pages():
    """The ten trial pages: hidden file, iframe button and link-as-button included."""
    pw = pytest.importorskip("playwright.sync_api")
    cases = json.loads((CASES / "cases.json").read_text())
    assert len(cases) == 10
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            def serve(r):
                f = CASES / r.request.url.rsplit("/", 1)[1]
                if f.suffix == ".html" and f.is_file():
                    r.fulfill(body=f.read_text(), content_type="text/html")
                else:
                    r.fulfill(status=404, body="")         # the pages' images: not needed
            page.route("https://fixtures.test/**", serve)
            found = {}
            for c in cases:
                page.goto("https://fixtures.test/" + c["file"])
                cands = jev.candidates(page)
                found[c["id"]] = [x for x in cands if x["truth"]]
                assert not any(x["role"] == "hidden-field" and x["truth"] for x in cands)
            browser.close()
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("Chromium not installed")
        raise
    assert all(len(v) == 1 for v in found.values()), {k: len(v) for k, v in found.items()}
    assert found["08_canvas_hidden_file"][0]["visible"] is False       # found although hidden
    assert found["09_canvas_lti_iframe"][0]["frame"] == "Tool Content"  # found inside the frame


# -- shadow mode: our rule failed on a real page; Jev's pick is recorded, not done --

def _real_page(p, name):
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.route("https://fixtures.test/**", lambda r: r.fulfill(
        body=(CASES / name).read_text(), content_type="text/html"))
    page.goto("https://fixtures.test/" + name)
    return browser, page


def test_shadow_records_jevs_pick_and_leaves_the_page_as_it_was(monkeypatch):
    pw = pytest.importorskip("playwright.sync_api")
    from ainl.util import trouble
    filed = []
    monkeypatch.setattr(trouble, "report", lambda source, **kw: filed.append((source, kw)))

    def fake_choose(goal, cands, info, **kw):
        right = next(c for c in cands if c["truth"])
        return {"index": right["index"], "confidence": 0.91, "probability": 0.9,
                "probabilities": {c["index"]: (0.9 if c is right else 0.1 / (len(cands) - 1)) for c in cands},
                "sure": True, "latency_ms": 120, "usage": {}, "model": "jev-test"}
    monkeypatch.setattr(jev, "choose", fake_choose)
    try:
        with pw.sync_playwright() as p:
            browser, page = _real_page(p, "07_gs_resubmit.html")
            clicks = page.evaluate("() => { window.__clicks = 0; document.addEventListener('click', "
                                   "() => window.__clicks++, true); return 0; }")
            row = jev.shadow(page, "Open the PDF upload", "gradescope: no PDF upload", chat="111")
            left = page.evaluate("() => [document.querySelectorAll('[data-ainl-cand]').length, "
                                 "document.querySelector('.js-resubmit').style.outline, window.__clicks]")
            browser.close()
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("Chromium not installed")
        raise
    assert row["chose_label"] == "Resubmit" and row["sure"] and clicks == 0
    assert left == [0, "", 0]                       # no marks left behind, nothing clicked
    [(source, kw)] = filed
    assert source == "jev" and kw["chat"] == "111" and kw["where"] == "gradescope: no PDF upload"
    assert kw["error"] is False                     # not blamed on the script's exception
    assert kw["files"]["page.png"][:4] == b"\x89PNG" and "Resubmit" in kw["files"]["candidates.json"]
    assert kw["extra"]["chose_label"] == "Resubmit"


def test_shadow_is_off_without_a_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    assert jev.shadow(object(), "x", "y") is None


def test_the_owner_is_told_what_jev_would_have_clicked():
    from ainl.util import trouble
    text = trouble.owner_text({"source": "jev", "chat": "111", "where": "gradescope: no PDF upload",
                               "message": "⚠️ Jev shadow", "error": "", "dir": "/x/failures/jev/a",
                               "extra": {"chose_label": "Resubmit", "chose_context": "Problem Set 2",
                                         "confidence": 0.91, "sure": True}})
    assert "Jev" in text and "“Resubmit”" in text and "0.91" in text and "未执行" in text
    assert "不够有把握" not in text


def test_a_big_page_is_trimmed_to_what_jev_accepts():
    many = [{"index": str(i), "label": f"Link {i % 200}", "role": "link", "href": f"/x/{i % 200}",
             "visible": i % 3 != 0, "frame": ""} for i in range(1, 400)]
    got = jev.trim(many)
    assert len(got) <= jev.MAX_CHOICES - 1
    assert len({(c["label"], c["href"]) for c in got}) == len(got)          # no duplicates
    assert [int(c["index"]) for c in got] == sorted(int(c["index"]) for c in got)   # page order
    same_label = jev.trim(CANDS)                    # one per question: both kept
    assert [c["index"] for c in same_label] == ["1", "2"]


def test_a_cross_origin_frame_is_read_too():
    """Gradescope inside Canvas is ANOTHER site: a page script cannot enter it,
    so the reader goes frame by frame through Playwright (found on the real
    Problem Set 3 page, 2026-09-27: 'Resubmit' lives in that frame)."""
    pw = pytest.importorskip("playwright.sync_api")
    from urllib.parse import urlsplit
    site = Path(__file__).parent / "jev_flow_site"

    def serve(route):
        u = urlsplit(route.request.url)
        f = site / u.hostname / ((u.path.strip("/") or "index") + ".html")
        route.fulfill(status=200 if f.is_file() else 404, content_type="text/html",
                      body=f.read_text() if f.is_file() else "")
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context()
            ctx.route("**/*", serve)
            page = ctx.new_page()
            page.goto("https://canvas.test/courses/247/assignments/88")
            page.wait_for_load_state("networkidle")
            cands = jev.candidates(page)
            hosts = {urlsplit(f.url).hostname for f in page.frames}
            inner = [c for c in cands if c["label"] == "Resubmit"]
            clicked = False
            if inner:
                for f in page.frames:                       # the index still finds it to act on
                    loc = f.locator(f'[data-ainl-cand="{inner[0]["index"]}"]')
                    if loc.count():
                        loc.click()
                        clicked = f.locator("#dlg").is_visible()
            browser.close()
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("Chromium not installed")
        raise
    assert hosts == {"canvas.test", "gradescope.test"}
    assert len(inner) == 1 and inner[0]["frame"] == "Tool Content"
    assert len({c["index"] for c in cands}) == len(cands)          # one numbering across frames
    assert any(c["label"] == "Gradescope" and c["frame"] == "" for c in cands)   # the Canvas page too
    assert clicked                                                  # and it opened the dialog


def test_the_explorer_keeps_every_frames_dom_and_what_kinds_of_answers_a_page_asks(tmp_path, monkeypatch):
    """Exploring a platform seen for the first time: each step leaves the DOM
    of every frame (cross-origin too), the candidates, Jev's choice, and a
    count of what the page asks for (choices, typed boxes, uploads…)."""
    pw = pytest.importorskip("playwright.sync_api")
    import importlib.util
    from urllib.parse import urlsplit
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "jev_flow_trial", Path(__file__).parents[1] / "scripts" / "jev_flow_trial.py")
    flow = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(flow)
    monkeypatch.setattr(flow, "EXPLORE", tmp_path / "explore")
    site = Path(__file__).parent / "jev_flow_site"

    def serve(route):
        u = urlsplit(route.request.url)
        f = site / u.hostname / ((u.path.strip("/") or "index") + ".html")
        route.fulfill(status=200 if f.is_file() else 404, content_type="text/html",
                      body=f.read_text() if f.is_file() else "")
    try:
        with pw.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context()
            ctx.route("**/*", serve)
            page = ctx.new_page()
            page.goto("https://canvas.test/courses/247/assignments/89")
            page.wait_for_load_state("networkidle")
            cands = jev.candidates(page)
            ex = flow.Explorer("run1")
            step = {"step": 1, "url": page.url, "label": "Submit PDF", "confidence": 0.9}
            d = ex.step(page, 1, cands, step)
            ex.result(step, "ok")
            browser.close()
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("Chromium not installed")
        raise
    frames = json.loads((d / "frames.json").read_text())
    assert d.parts[-3:] == ("canvas.test", "run1", "step-01")
    assert {f["host"] for f in frames} == {"canvas.test", "gradescope.test"}
    gs = next(f for f in frames if f["host"] == "gradescope.test")
    assert gs["kinds"]["file_uploads"] == 2                  # the PDF field and the images field
    assert "Submit PDF" in (d / f"frame-{gs['k']}.html").read_text()
    line = json.loads((tmp_path / "explore" / "index.jsonl").read_text().splitlines()[-1])
    assert line["host"] == "canvas.test" and line["status"] == "ok" and line["chose"] == "Submit PDF"
