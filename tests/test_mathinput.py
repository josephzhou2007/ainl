"""Tests for ainl.mathinput (offline: no network, no API key needed)."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from ainl.mathinput import __main__ as cli
from ainl.mathinput import vision
from ainl.mathinput.latex_to_mathml import (
    SHELL_CLOSE,
    SHELL_OPEN,
    latex_to_webassign_body,
    latex_to_webassign_mathml,
)


def strip_shell(mathml: str) -> str:
    assert mathml.startswith(SHELL_OPEN), mathml
    assert mathml.endswith(SHELL_CLOSE), mathml
    return mathml[len(SHELL_OPEN) : -len(SHELL_CLOSE)]


# --------------------------------------------------------------------------- #
# golden cases: byte-for-byte against docs/cengage-webassign-api.md section 9
# --------------------------------------------------------------------------- #
GOLDEN = [
    (
        r"\frac{1}{16}\sin(16x)",
        '<mfrac><mn>1</mn><mn>16</mn></mfrac><mi>sin</mi>'
        '<mfenced separators=""><mrow><mn>16</mn><mi>x</mi></mrow></mfenced>',
    ),
    ("0", "<mn>0</mn>"),
    (r"0.5\pi", "<mn>0</mn><mo>.</mo><mn>5</mn><mi>&#x3C0;</mi>"),
    (
        r"4\sec(4x)^2",
        "<mn>4</mn><mi>sec</mi><msup><mfenced><mrow><mn>4</mn><mi>x</mi></mrow></mfenced>"
        "<mn>2</mn></msup>",
    ),
]


@pytest.mark.parametrize("latex,expected", GOLDEN)
def test_golden(latex, expected):
    assert strip_shell(latex_to_webassign_mathml(latex)) == expected


def test_shell_is_fixed():
    out = latex_to_webassign_mathml("0")
    assert out == '<math xmlns="http://www.w3.org/1998/Math/MathML"><mspace/><mn>0</mn></math>'
    assert latex_to_webassign_body("") == ""


# --------------------------------------------------------------------------- #
# shape assertions for other common calculus forms
# --------------------------------------------------------------------------- #
def test_sqrt():
    assert latex_to_webassign_body(r"\sqrt{x}") == "<msqrt><mi>x</mi></msqrt>"
    assert latex_to_webassign_body(r"\sqrt[3]{x}") == "<mroot><mi>x</mi><mn>3</mn></mroot>"


def test_polynomial_plus_constant():
    assert latex_to_webassign_body("x^2+2x+C") == (
        "<msup><mi>x</mi><mn>2</mn></msup><mo>+</mo><mn>2</mn><mi>x</mi><mo>+</mo><mi>C</mi>"
    )


def test_pi_over_four():
    assert latex_to_webassign_body(r"\frac{\pi}{4}") == "<mfrac><mi>&#x3C0;</mi><mn>4</mn></mfrac>"


def test_exponential():
    assert latex_to_webassign_body("e^{2x}") == (
        "<msup><mi>e</mi><mrow><mn>2</mn><mi>x</mi></mrow></msup>"
    )
    assert latex_to_webassign_body("e^{-x}") == (
        "<msup><mi>e</mi><mrow><mo>-</mo><mi>x</mi></mrow></msup>"
    )


def test_ln_abs():
    assert latex_to_webassign_body(r"\ln|x|") == (
        '<mi>ln</mi><mfenced open="|" close="|"><mrow><mi>x</mi></mrow></mfenced>'
    )


def test_function_power_then_parens():
    assert latex_to_webassign_body(r"\sin^2(x)") == (
        '<msup><mi>sin</mi><mn>2</mn></msup><mfenced separators=""><mrow><mi>x</mi></mrow></mfenced>'
    )


def test_left_right_parens_squared():
    assert latex_to_webassign_body(r"\left(x+1\right)^2") == (
        "<msup><mfenced><mrow><mi>x</mi><mo>+</mo><mn>1</mn></mrow></mfenced><mn>2</mn></msup>"
    )


def test_plain_parens_no_separators():
    body = latex_to_webassign_body(r"\frac{1}{2}(x+1)")
    assert body.startswith("<mfrac><mn>1</mn><mn>2</mn></mfrac><mfenced><mrow>")
    assert "separators" not in body


def test_unknown_structure_passes_through():
    body = latex_to_webassign_body(r"\int_0^1 x\,dx")
    assert body.startswith("<msubsup><mo>&#x222B;</mo><mn>0</mn><mn>1</mn></msubsup>")
    assert "mspace" not in body  # thin space dropped


# --------------------------------------------------------------------------- #
# vision: stubbed anthropic client, no network
# --------------------------------------------------------------------------- #
class _FakeMessages:
    def __init__(self, reply: str, calls: list):
        self._reply = reply
        self._calls = calls

    def create(self, **kwargs):
        self._calls.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=self._reply)])


class _FakeClient:
    def __init__(self, reply: str, calls: list):
        self.messages = _FakeMessages(reply, calls)


def _stub(monkeypatch, reply: str) -> list:
    calls: list = []
    monkeypatch.setattr(vision, "_make_client", lambda: _FakeClient(reply, calls))
    return calls


@pytest.mark.parametrize(
    "reply,expected",
    [
        (r"\frac{1}{16}\sin(16x)", r"\frac{1}{16}\sin(16x)"),
        ("$\\frac{1}{2}x^2 + C$\nThis is the antiderivative.", r"\frac{1}{2}x^2 + C"),
        ("```latex\n0.5\\pi\n```", r"0.5\pi"),
        ("  \\[ e^{2x} \\]  ", "e^{2x}"),
        ("\\( \\sqrt{x} \\)", r"\sqrt{x}"),
        ("$$4\\sec(4x)^2$$\n", r"4\sec(4x)^2"),
    ],
)
def test_handwriting_to_latex_cleans_response(monkeypatch, reply, expected):
    calls = _stub(monkeypatch, reply)
    out = vision.handwriting_to_latex(b"\xff\xd8fakejpeg", hint="indefinite integral, include +C")
    assert out == expected
    assert len(calls) == 1
    req = calls[0]
    assert req["model"] == vision.MODEL
    content = req["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["media_type"] == "image/jpeg"
    assert "indefinite integral" in content[1]["text"]


def test_png_media_type(monkeypatch):
    calls = _stub(monkeypatch, "x")
    vision.handwriting_to_latex(b"\x89PNG\r\n\x1a\nfake")
    assert calls[0]["messages"][0]["content"][0]["source"]["media_type"] == "image/png"


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv(vision.API_KEY_ENV, raising=False)
    with pytest.raises(vision.MissingApiKeyError):
        vision.handwriting_to_latex(b"\xff\xd8")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_latex(capsys):
    assert cli.main(["--latex", r"\frac{1}{16}\sin(16x)"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == latex_to_webassign_mathml(r"\frac{1}{16}\sin(16x)")


def test_cli_image_without_key_exits_2(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv(vision.API_KEY_ENV, raising=False)
    img = tmp_path / "a.jpg"
    img.write_bytes(b"\xff\xd8fake")
    assert cli.main(["--image", str(img)]) == 2
    err = capsys.readouterr().err
    assert vision.API_KEY_ENV in err


def test_cli_image_stubbed(monkeypatch, tmp_path, capsys):
    _stub(monkeypatch, r"0.5\pi")
    img = tmp_path / "a.jpg"
    img.write_bytes(b"\xff\xd8fake")
    assert cli.main(["--image", str(img), "--hint", "value"]) == 0
    out = capsys.readouterr().out
    assert "LaTeX:  0.5\\pi" in out
    assert "<mn>0</mn><mo>.</mo><mn>5</mn><mi>&#x3C0;</mi>" in out


def test_no_key_leaks_into_module_source():
    src = open(vision.__file__, encoding="utf-8").read()
    assert "sk-ant-" not in src
    assert sys.modules["ainl.mathinput.vision"] is vision


# -- one numbered answer sheet -> {question: answer} --------------------------

from ainl.mathinput.pipeline import final_answer, number_problems  # noqa: E402


def test_number_problems_maps_each_answer_to_its_question():
    got = number_problems(r"1. x^2 \quad 2. \frac{1}{2}x \quad 3. \sin(x)+C")
    assert got == {1: ["x^2"], 2: [r"\frac{1}{2}x"], 3: [r"\sin(x)+C"]}


def test_numbering_styles_all_work():
    assert set(number_problems(r"(1) a \quad 2) b \quad 3. c")) == {1, 2, 3}


def test_unnumbered_chunks_are_dropped_not_guessed():
    # a chunk with no number could belong to any question; filling it into the
    # wrong box is worse than leaving it out
    assert number_problems(r"x^2 \quad 2. y") == {2: ["y"]}


def test_working_is_stripped_down_to_the_answer():
    assert final_answer(r"u = 3x \ du = 3dx \ = \frac{1}{3}e^{3x}+C") == r"\frac{1}{3}e^{3x}+C"
    assert final_answer(r"\int x dx = \frac{x^2}{2}") == r"\frac{x^2}{2}"
    assert final_answer("x^2") == "x^2"          # no working: the whole thing


def test_trailing_words_are_not_part_of_the_answer():
    assert final_answer(r"\frac{1}{2} \text{converge}") == r"\frac{1}{2}"


def test_answer_sheet_with_working_per_question():
    got = number_problems(r"1. \int 2x dx = x^2 + C \quad 2. = \frac{\pi}{4}")
    assert got == {1: ["x^2 + C"], 2: [r"\frac{\pi}{4}"]}


def test_a_label_with_no_space_after_it_still_splits():
    # the real miss: "4." ran straight into the formula, so question 4 was
    # swallowed by question 3 and never filled
    assert sorted(number_problems(r"3.\sin(x) 4.\frac{1}{2}")) == [3, 4]


def test_a_decimal_is_not_a_question_label():
    assert number_problems(r"1. 3.5x") == {1: ["3.5x"]}


def test_several_blanks_on_one_line_split_on_commas():
    assert number_problems(r"2. x^2, \sin(x), 0") == {2: ["x^2", r"\sin(x)", "0"]}


def test_commas_inside_brackets_belong_to_the_answer():
    assert number_problems(r"5. (1,2)") == {5: ["(1,2)"]}          # an interval
    assert number_problems(r"6. \frac{f(a,b)}{2}, 0") == {6: [r"\frac{f(a,b)}{2}", "0"]}


# -- Canvas item <-> WebAssign assignment ------------------------------------

from ainl.external import link  # noqa: E402


class _A:
    def __init__(self, dep, title):
        self.dep, self.title = dep, title


WA = [_A("1", "7.4 Homework-Partial Fractions"),
      _A("2", "7.5 Homework-Strategy for Integration"),
      _A("3", "Module Quiz 10 - Integration Bee")]


def test_identical_titles_link_the_two_sides():
    # measured: Canvas and WebAssign print the same string
    assert link.find("7.5 Homework-Strategy for Integration", WA).dep == "2"


def test_punctuation_and_case_do_not_break_the_link():
    assert link.find("7.4 homework - partial fractions", WA).dep == "1"


def test_section_number_is_the_fallback():
    assert link.find("7.4 Partial Fractions (WebAssign)", WA).dep == "1"


def test_an_ambiguous_section_is_no_match():
    two = WA + [_A("9", "7.4 Homework-Extra Practice")]
    assert link.find("7.4 Something Else", two) is None


def test_an_unrelated_title_is_no_match():
    assert link.find("Annotations: Fitzsimmonds et al.", WA) is None


class _Canvas:
    def __init__(self, url="https://canvas/launch?verifier=x"):
        self.url, self.calls = url, []

    def sessionless_launch(self, course_id, assignment_id):
        self.calls.append((course_id, assignment_id))
        return self.url


class _WA:
    def __init__(self, dep="40575441", fail=False):
        self.dep, self.fail, self.launches = dep, fail, []

    def open_launch(self, url):
        if self.fail:
            raise RuntimeError("launch refused")
        self.launches.append(url)
        return self.dep

    def assignments(self):
        return WA


def test_lti_launch_is_what_resolves_the_assignment(monkeypatch, tmp_path):
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    c, w = _Canvas(), _WA()
    assert link.dep_for(c, w, "100003", "3152480", "7.4 Homework-Partial Fractions") == "40575441"
    assert c.calls == [("100003", "3152480")]


def test_a_resolved_mapping_is_not_launched_again(monkeypatch, tmp_path):
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    c, w = _Canvas(), _WA()
    link.dep_for(c, w, "100003", "3152480", "x")
    link.dep_for(c, w, "100003", "3152480", "x")
    assert len(w.launches) == 1          # ids never change; resolve once


def test_title_matching_is_the_fallback_when_launch_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    c, w = _Canvas(), _WA(fail=True)
    assert link.dep_for(c, w, "100003", "999", "7.5 Homework-Strategy for Integration") == "2"


def test_unknown_stays_unknown(monkeypatch, tmp_path):
    monkeypatch.setenv("AINL_HOME", str(tmp_path))
    c, w = _Canvas(), _WA(fail=True)
    assert link.dep_for(c, w, "100003", "999", "Something Unrelated") is None


def test_a_module_quiz_opens_without_asking():
    from ainl.external.webassign import Assignment
    q = Assignment("1", "Module Quiz 10 - Integration Bee")
    assert q.is_quiz and not q.needs_confirm       # confirmed untimed


def test_an_unfamiliar_quiz_still_asks():
    from ainl.external.webassign import Assignment
    assert Assignment("2", "Midterm Quiz 2").needs_confirm
    assert not Assignment("3", "7.4 Homework-Partial Fractions").needs_confirm


def test_no_source_file_has_a_mangled_escape():
    """A patch that loses a backslash turns \b into a backspace and \t into a
    tab, and the regex still compiles — it just never matches. _QUIZ_TITLE sat
    broken that way, so quizzes were never recognised at all."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    bad = []
    for f in list((root / "src").rglob("*.py")) + list((root / "tests").rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        for ch in (chr(8), chr(12), chr(7), chr(11)):      # backspace, formfeed, bell, vtab
            if ch in text:
                bad.append(f"{f.name}: {ch!r}")
    assert not bad, f"mangled escapes: {bad}"


# -- where work is actually handed in ----------------------------------------

from ainl.external import routing  # noqa: E402


def test_an_lti_assignment_is_one_submission_not_two():
    r = routing.route({"submission_types": ["external_tool"],
                       "external_tool_url": "https://lti-gradescope.int.turnitin.com/launch/gs"})
    assert r.where == "gradescope" and r.syncs_to_canvas
    assert "comes back to Canvas" in routing.describe(r)


def test_a_canvas_upload_in_a_gradescope_course_needs_both():
    r = routing.route({"submission_types": ["online_upload"]}, course_has_gradescope=True)
    assert r.canvas_native and not r.syncs_to_canvas
    assert "Both are needed" in routing.describe(r)


def test_a_plain_canvas_upload_is_just_canvas():
    r = routing.route({"submission_types": ["online_upload"]}, course_has_gradescope=False)
    assert r.where == "canvas" and "right here" in routing.describe(r)


def test_webassign_still_routes_to_webassign():
    r = routing.route({"submission_types": ["external_tool"],
                       "external_tool_url": "https://gateway.cengage.com/rest/launchBasicLTI/1/2"})
    assert r.where == "webassign" and r.syncs_to_canvas


def test_gradescope_tab_detection():
    assert routing.has_gradescope([{"label": "Gradescope", "type": "external"}])
    assert not routing.has_gradescope([{"label": "Grades", "type": "internal"}])



def test_written_choice_matches_abbreviations_but_never_guesses():
    from ainl.mathinput.pipeline import without_word, written_choice
    labels = ["convergent", "divergent"]
    assert written_choice(r"Conver. -40e^{-1}", labels) == (0, "Conver")
    assert written_choice("diverges", labels) == (1, "diverges")
    assert written_choice(r"\frac{1}{2}", labels) is None        # \frac is not a word
    assert written_choice("yes", ["yes", "no"]) is None          # too short to trust
    assert written_choice("cont", ["continuous", "contradiction"]) is None   # ambiguous
    assert without_word(r"divergent -\frac{1}{9}", "divergent") == r"-\frac{1}{9}"
    assert without_word(r"\text{Convergent} 5", "Convergent") == "5"


def test_lettered_parts_split_in_order_and_lose_their_labels():
    from ainl.mathinput.pipeline import number_problems, split_parts
    assert split_parts(r"(a) 25e^{-t/100} (b) 22.6") == [r"25e^{-t/100}", "22.6"]
    assert split_parts(r"a) 1, 2 b) 3") == ["1", "2", "3"]           # (a) may have blanks of its own
    assert split_parts(r"(1,2), \sin(b)") == ["(1,2)", r"\sin(b)"]    # a (b) inside an answer is not a label
    assert number_problems(r"5. x, y  6. (a) 1 (b) 2  7. 3, 4") == {5: ["x", "y"], 6: ["1", "2"], 7: ["3", "4"]}


def test_two_equations_on_one_line_are_two_answers():
    """9.3 Homework q5 (2026-09-25): two lines on the sheet, no comma, no label —
    the reader joined them and only the last right-hand side survived ("M")."""
    from ainl.mathinput.pipeline import number_problems
    got = number_problems(r"5. P(t)=M-Ce^{-kt} \lim_{t\to\infty}P(t)=M")
    assert got == {5: [r"M-Ce^{-kt}", "M"]}
    assert number_problems(r"3. y = 2x = 4")[3] == ["2x = 4"] or len(number_problems(r"3. y = 2x = 4")[3]) == 1
    assert number_problems(r"1. 15u^2+6u^2=10t^3-\frac{210}{t}+C") == {1: [r"10t^3-\frac{210}{t}+C"]}
