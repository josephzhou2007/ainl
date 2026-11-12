"""Question pictures: whole even when taller than the screen; picture options
lettered on the picture itself (real headless Chromium)."""
from __future__ import annotations

from io import BytesIO

import pytest

from ainl.external import webassign

PIC = ("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='220' height='160'>"
       "<rect width='220' height='160' fill='%23ddd'/></svg>")


def _option(name: str, value: int) -> str:
    return (f"<label style='display:inline-block;margin:10px'><input type='radio' name='{name}' "
            f"value='{value}'><img src=\"{PIC}\"></label>")


def _page(tall: int, at_bottom: bool) -> str:
    top = "" if at_bottom else "<div style='height:300px'>header</div>"
    after = "" if at_bottom else "<div style='height:1200px'>more questions</div>"
    return (f"<html><body style='margin:0'>{top}<div id='question111_1' style='height:{tall}px;"
            "position:relative;border-top:4px solid #000'>"
            "<p>1. A direction field is shown.</p>"
            f"{_option('RC_1', 1)}{_option('RC_1', 2)}"
            f"<div style='position:absolute;top:{tall - 260}px'>{_option('RC_1', 3)}{_option('RC_1', 4)}</div>"
            f"</div>{after}</body></html>")


def _shoot(html: str, marks: bool = True):
    pw = pytest.importorskip("playwright.sync_api")
    from PIL import Image
    try:
        with pw.sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            page = b.new_page(viewport={"width": 1000, "height": 800})
            page.set_content(html)
            want = page.evaluate(webassign._OPTION_MARKS_JS, "question111_1")
            shots = webassign.shoot_all(page, ["question111_1"], [], mark_options=marks)
            b.close()
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("Chromium not installed")
        raise
    return Image.open(BytesIO(shots["question111_1"])).convert("RGB"), want


def _red(img, x, y, around: int = 12) -> bool:
    """The badge's red is around the radio (its centre may be the white letter)."""
    for dx in range(-around, around + 1, 3):
        for dy in range(-around, around + 1, 3):
            r, g, b = img.getpixel((int(x) + dx, int(y) + dy))
            if r > 200 and g < 110 and b < 110:
                return True
    return False


@pytest.mark.parametrize("at_bottom", [False, True])
def test_a_question_taller_than_the_screen_is_taken_whole(at_bottom):
    img, _ = _shoot(_page(2400, at_bottom), marks=False)
    assert abs(img.height - 2404) <= 2, img.height        # 2400 + its 4px border: three screens, nothing twice
    assert img.width >= 900


def test_picture_options_are_lettered_on_the_question_picture():
    img, marks = _shoot(_page(2400, False))
    assert [(m["group"], m["k"]) for m in marks] == [(1, 0), (1, 1), (1, 2), (1, 3)]
    for m in marks:                                       # A and B on screen one, C and D on screen three
        assert _red(img, m["x"], m["y"]), m


def test_text_only_choices_get_no_badges():
    html = ("<html><body><div id='question111_1'><label><input type='radio' name='RC_1'>convergent</label>"
            "<label><input type='radio' name='RC_1'>divergent</label></div></body></html>")
    img, marks = _shoot(html)
    assert marks == []


def test_picture_options_are_not_sent_one_photo_each():
    from types import SimpleNamespace
    from ainl.bot.conversation import Conversation
    from ainl.bot.state import BotState, MathSession
    photos, texts = [], []
    tg = SimpleNamespace(send=lambda chat, text, **kw: texts.append(text),
                         send_photo=lambda *a, **kw: photos.append(a))
    conv = Conversation(SimpleNamespace(), tg, "1", BotState(), tz="UTC")
    ms = MathSession(stage="shoot", dep="9")
    ms.taps = [{"n": 1, "name": "RC_1", "kind": "choice",
                "options": [{"value": str(i), "label": "", "image": f"https://x.test/{i}.gif"} for i in range(4)]},
               {"n": 1, "name": "RC_2", "kind": "choice",
                "options": [{"value": "a", "label": "The slopes at each point are independent of x", "image": ""},
                            {"value": "b", "label": "y' = 0 on the lines x = 0 and y = 3", "image": ""}]}]
    conv.cs.math = ms
    conv._send_taps()
    assert photos == []
    assert "marked <b>A–D</b> in red on question 1's picture" in texts[0]
    assert "<b>A.</b> The slopes at each point" in texts[1]


def test_a_plain_number_box_is_typed_into_not_just_set():
    """A page that, like WebAssign, keeps only what was TYPED (its own model is
    updated on keyup): the filled value must reach that model."""
    pw = pytest.importorskip("playwright.sync_api")
    html = """<html><body><div id="question4597916_3">y(2.5) =
      <input type="text" name="RN_4597916_3_0_4605218" id="RN_4597916_3_0_4605218" value="">
      <input type="hidden" id="RA_4597916_3_1_x" value=""><div id="editable-math-RA_4597916_3_1_x"></div>
    </div><script>
      window.model = ''; window.keys = 0; window.changed = '';
      const f = document.getElementById('RN_4597916_3_0_4605218');
      f.addEventListener('keyup', () => { window.keys++; window.model = f.value; });
      f.addEventListener('change', () => { window.changed = f.value; });
      // MooTools replaces the global Event: `new Event('change')` throws
      window.Event = function (e) { return e.indexOf('x'); };
    </script></body></html>"""
    try:
        with pw.sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            page = b.new_page()
            page.set_content(html)
            plain = page.evaluate(webassign._PLAIN_JS, ["RN_4597916_3_0_4605218", "RA_4597916_3_1_x"])
            page.evaluate(webassign._FILL_JS, {"RN_4597916_3_0_4605218": "15.9394"})
            before = page.evaluate("() => window.model")
            for box in plain:
                webassign._type_into(page, box, "15.9394")
            after = page.evaluate("() => [window.model, window.keys, document.getElementById('RN_4597916_3_0_4605218').value, window.changed]")
            b.close()
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("Chromium not installed")
        raise
    assert plain == ["RN_4597916_3_0_4605218"]            # the maths box is not typed into
    assert before == ""                                    # setting .value alone: the page never knew
    assert after[0] == "15.9394" and after[1] >= 7 and after[2] == "15.9394"
    assert after[3] == "15.9394"                           # change fired despite MooTools' Event


def test_a_question_with_two_parts_to_tap_labels_them_a_and_b():
    """9.2 Homework q3 (2026-09-27): a picture part and a text part, both headed
    "3." — read as two question 3s."""
    from types import SimpleNamespace
    from ainl.bot.conversation import Conversation
    from ainl.bot.state import BotState, MathSession
    texts = []
    tg = SimpleNamespace(send=lambda chat, text, **kw: texts.append(text) or [len(texts)],
                         send_photo=lambda *a, **kw: None)
    conv = Conversation(SimpleNamespace(), tg, "1", BotState(), tz="UTC")
    ms = MathSession(stage="shoot", dep="9")
    long = "The slopes at each point are independent of x, so the slopes are the same along the lines y = c"
    ms.taps = [{"n": 3, "name": "RC_1", "kind": "choice",
                "options": [{"value": str(i), "label": "", "image": "https://x.test/p.gif"} for i in range(4)]},
               {"n": 3, "name": "RC_2", "kind": "choice",
                "options": [{"value": "7", "label": long, "image": ""}, {"value": "8", "label": "y' = 0", "image": ""}]},
               {"n": 4, "name": "RC_3", "kind": "choice",
                "options": [{"value": "a", "label": "convergent", "image": ""}, {"value": "b", "label": "divergent", "image": ""}]}]
    ms.picked = {"RC_2": ["7"], "RC_3": ["b"]}
    conv.cs.math = ms
    conv._send_taps()
    assert texts[0].startswith("<b>3(a).</b>") and texts[1].startswith("<b>3(b).</b>")
    assert texts[2].startswith("<b>4.</b>")                             # one part: just its number
    assert long in texts[1]                                           # not cut at 80 characters
    assert conv._tap_name(ms.taps[1], ms.taps[1]["options"][0]) == "A"
