"""Tests for ainl.mathinput.providers / pipeline / evaluate (offline: every
network call is stubbed, no API key is needed and none is ever set to a real value)."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from ainl.mathinput import __main__ as cli
from ainl.mathinput import evaluate, pipeline
from ainl.mathinput.providers import (
    ProviderNotConfigured,
    VisionProvider,
    anthropic,
    base,
    gemini,
    mathpix,
    qwen,
    registry,
)

ALL_ENV = (
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "DASHSCOPE_API_KEY",
    "MATHPIX_APP_ID",
    "MATHPIX_APP_KEY",
)
JPEG = b"\xff\xd8fakejpeg"


@pytest.fixture(autouse=True)
def _no_keys(monkeypatch):
    for name in ALL_ENV:
        monkeypatch.delenv(name, raising=False)


def _stub_http(monkeypatch, payload: dict) -> list:
    """Replace the single HTTP entry point; returns the list of captured calls."""
    calls: list = []

    def fake_post(url, *, headers, json):
        calls.append({"url": url, "headers": headers, "json": json})
        return payload

    monkeypatch.setattr(base, "http_post", fake_post)
    return calls


# --------------------------------------------------------------------------- #
# missing key behaviour
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "factory,env",
    [
        (gemini.GeminiProvider, "GEMINI_API_KEY"),
        (qwen.QwenProvider, "DASHSCOPE_API_KEY"),
        (anthropic.sonnet, "ANTHROPIC_API_KEY"),
        (anthropic.haiku, "ANTHROPIC_API_KEY"),
        (mathpix.MathpixProvider, "MATHPIX_APP_ID"),
    ],
)
def test_missing_key_raises_at_construction(factory, env):
    with pytest.raises(ProviderNotConfigured) as exc:
        factory()
    assert env in str(exc.value)


def test_mathpix_needs_both_keys(monkeypatch):
    monkeypatch.setenv("MATHPIX_APP_ID", "x")
    with pytest.raises(ProviderNotConfigured) as exc:
        mathpix.MathpixProvider()
    assert "MATHPIX_APP_KEY" in str(exc.value)


def test_import_does_not_need_keys_or_sdks():
    # Importing the package (already done above) with no keys set must succeed;
    # the providers module must not import google/anthropic at module level.
    import sys

    assert "ainl.mathinput.providers.gemini" in sys.modules
    assert "google.genai" not in sys.modules


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def test_registry_names_and_costs_from_report(monkeypatch):
    assert registry.names() == [
        "gemini-flash-lite",
        "qwen-vl-flash",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "mathpix",
    ]
    assert registry.available() == []
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "x")
    assert registry.available() == ["gemini-flash-lite", "qwen-vl-flash"]
    g = registry.get("gemini-flash-lite")
    q = registry.get("qwen-vl-flash")
    assert isinstance(g, VisionProvider) and isinstance(q, VisionProvider)
    assert (g.cost_per_1k, q.cost_per_1k) == (0.35, 0.08)
    with pytest.raises(ProviderNotConfigured):
        registry.get("claude-sonnet-5")
    with pytest.raises(KeyError):
        registry.get("nope")


def test_anthropic_instances(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    s, h = registry.get("claude-sonnet-5"), registry.get("claude-haiku-4-5")
    assert (s.model, s.cost_per_1k) == ("claude-sonnet-5", 3.07)
    assert (h.model, h.cost_per_1k) == ("claude-haiku-4-5-20251001", 1.54)


# --------------------------------------------------------------------------- #
# each httpx provider: request shape + response parsing
# --------------------------------------------------------------------------- #
def test_gemini_request_and_parse(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    calls = _stub_http(
        monkeypatch,
        {"candidates": [{"content": {"parts": [{"text": "$\\frac{1}{2}x^2 + C$\n"}]}}]},
    )
    out = gemini.GeminiProvider().transcribe(JPEG, hint="indefinite integral")
    assert out == r"\frac{1}{2}x^2 + C"
    req = calls[0]
    assert req["url"].endswith("models/gemini-3.1-flash-lite:generateContent")
    assert req["headers"]["x-goog-api-key"] == "x"
    parts = req["json"]["contents"][0]["parts"]
    assert parts[0]["inline_data"]["mime_type"] == "image/jpeg"
    assert "indefinite integral" in parts[1]["text"]
    assert "VERBATIM" in req["json"]["system_instruction"]["parts"][0]["text"]


def test_qwen_request_and_parse(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "x")
    calls = _stub_http(
        monkeypatch, {"choices": [{"message": {"role": "assistant", "content": "```latex\n0.5\\pi\n```"}}]}
    )
    out = qwen.QwenProvider().transcribe(b"\x89PNG\r\n\x1a\nfake")
    assert out == r"0.5\pi"
    req = calls[0]
    assert req["url"] == qwen.ENDPOINT
    assert req["headers"]["Authorization"] == "Bearer x"
    assert req["json"]["model"] == "qwen3-vl-flash"
    user = req["json"]["messages"][1]["content"]
    assert user[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert req["json"]["messages"][0]["content"].startswith("You transcribe")


def test_mathpix_latex_and_mathml(monkeypatch):
    monkeypatch.setenv("MATHPIX_APP_ID", "id")
    monkeypatch.setenv("MATHPIX_APP_KEY", "key")
    calls = _stub_http(
        monkeypatch,
        {
            "text": "\\( \\sqrt{x} \\)",
            "data": [
                {"type": "latex", "value": "\\sqrt{x}"},
                {"type": "mathml", "value": "<math><msqrt><mi>x</mi></msqrt></math>"},
            ],
        },
    )
    p = mathpix.MathpixProvider()
    assert p.transcribe(JPEG) == r"\sqrt{x}"
    assert p.transcribe_mathml(JPEG) == "<math><msqrt><mi>x</mi></msqrt></math>"
    assert calls[0]["headers"]["app_id"] == "id"
    assert calls[0]["json"]["formats"] == ["text", "data"]


def test_mathpix_falls_back_to_text_field(monkeypatch):
    monkeypatch.setenv("MATHPIX_APP_ID", "id")
    monkeypatch.setenv("MATHPIX_APP_KEY", "key")
    _stub_http(monkeypatch, {"text": "\\[ e^{2x} \\]", "data": []})
    p = mathpix.MathpixProvider()
    assert p.transcribe(JPEG) == "e^{2x}"
    assert p.transcribe_mathml(JPEG) == ""


def test_anthropic_provider_uses_shared_prompt(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    calls: list = []

    class _Msgs:
        def create(self, **kw):
            calls.append(kw)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="x^2")])

    monkeypatch.setattr(anthropic, "_default_client", lambda: SimpleNamespace(messages=_Msgs()))
    assert anthropic.haiku().transcribe(JPEG) == "x^2"
    assert calls[0]["model"] == "claude-haiku-4-5-20251001"
    assert "do NOT correct" in calls[0]["system"]


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "a,b",
    [
        (r"\frac{1}{2}", r"\frac{ 1 }{ 2 }"),
        (r"\left(x+1\right)^2", "(x+1)^2"),
        (r"\dfrac{\pi}{4}", r"\frac{\pi}{4}"),
        (r"\bigl| x \bigr|", "|x|"),
        (r"\int x \, \mathrm{d}x", r"\int x dx"),
        (r"x^{2}", "x^2"),
        (r"2 \cdot x", r"2 \times x"),
    ],
)
def test_normalize_equivalent(a, b):
    assert pipeline.same_expression(a, b)


@pytest.mark.parametrize(
    "a,b",
    [
        (r"\frac{1}{2}", r"\frac{1}{3}"),
        (r"x^{12}", r"x^1 2"),
        (r"\rightarrow", r"arrow"),
        (r"\cdots", r"*s"),
        ("", ""),
    ],
)
def test_normalize_not_equivalent(a, b):
    assert not pipeline.same_expression(a, b)


# --------------------------------------------------------------------------- #
# consensus: three paths
# --------------------------------------------------------------------------- #
class _Fake:
    required_env = ()

    def __init__(self, name, cost, reply):
        self.name, self.cost_per_1k, self._reply, self.calls = name, cost, reply, 0

    def transcribe(self, image_bytes, *, hint=""):
        self.calls += 1
        if isinstance(self._reply, Exception):
            raise self._reply
        return self._reply


def _getter(**providers):
    def get(name):
        try:
            return providers[name]
        except KeyError:
            raise ProviderNotConfigured(f"{name} not set") from None

    return get


def test_consensus_agree_no_escalation():
    g = _Fake("gemini-flash-lite", 0.35, r"\frac{1}{2}x^2 + C")
    q = _Fake("qwen-vl-flash", 0.08, r"\frac{ 1 }{ 2 } x^{2}+C")
    s = _Fake("claude-sonnet-5", 3.07, "never")
    r = pipeline.transcribe_with_consensus(
        JPEG, cheap=("gemini-flash-lite", "qwen-vl-flash"),
        get=_getter(**{g.name: g, q.name: q, s.name: s}))
    assert r.agreed and not r.escalated and not r.needs_human
    assert r.latex == r"\frac{1}{2}x^2 + C"
    assert "<mfrac>" in r.mathml
    assert r.candidates == {g.name: g._reply, q.name: q._reply}
    assert r.cost_usd == pytest.approx(0.00043)
    assert s.calls == 0


def test_consensus_escalate_then_agree():
    g = _Fake("gemini-flash-lite", 0.35, r"\sin(2x)")
    q = _Fake("qwen-vl-flash", 0.08, r"\sin(2\pi)")
    s = _Fake("claude-sonnet-5", 3.07, r"\sin\left(2x\right)")
    r = pipeline.transcribe_with_consensus(
        JPEG, cheap=("gemini-flash-lite", "qwen-vl-flash"),
        get=_getter(**{g.name: g, q.name: q, s.name: s}))
    assert r.escalated and not r.agreed and not r.needs_human
    assert r.latex == r"\sin\left(2x\right)"  # the escalation model's reading is used
    assert set(r.candidates) == {g.name, q.name, s.name}
    assert r.cost_usd == pytest.approx((0.35 + 0.08 + 3.07) / 1000)


def test_consensus_needs_human():
    g = _Fake("gemini-flash-lite", 0.35, "x^2")
    q = _Fake("qwen-vl-flash", 0.08, "x^3")
    s = _Fake("claude-sonnet-5", 3.07, "x^4")
    r = pipeline.transcribe_with_consensus(
        JPEG, cheap=("gemini-flash-lite", "qwen-vl-flash"),
        get=_getter(**{g.name: g, q.name: q, s.name: s}))
    assert r.needs_human and r.escalated
    assert r.latex == "" and r.mathml == ""
    assert len(r.candidates) == 3


def test_consensus_skips_unconfigured_cheap_provider():
    """Gemini has no key; the surviving cheap reader is used and nothing escalates."""
    q = _Fake("qwen-vl-flash", 0.08, "x^2")
    s = _Fake("claude-sonnet-5", 3.07, "x^{2}")
    r = pipeline.transcribe_with_consensus(
        JPEG, cheap=("gemini-flash-lite", "qwen-vl-flash"),
        get=_getter(**{q.name: q, s.name: s}))
    assert r.agreed and not r.escalated and not r.needs_human
    assert r.latex == "x^2"
    assert s.calls == 0


def test_consensus_network_error_counts_as_no_vote():
    g = _Fake("gemini-flash-lite", 0.35, RuntimeError("boom"))
    q = _Fake("qwen-vl-flash", 0.08, "x^2")
    s = _Fake("claude-sonnet-5", 3.07, "x^2")
    r = pipeline.transcribe_with_consensus(
        JPEG, cheap=("gemini-flash-lite", "qwen-vl-flash"),
        get=_getter(**{g.name: g, q.name: q, s.name: s}))
    # one reader died, the surviving reading is trusted (student confirms downstream)
    assert r.latex == "x^2" and not r.escalated and r.agreed
    assert "boom" in r.errors["gemini-flash-lite"]
    assert r.cost_usd == pytest.approx(0.08 / 1000)  # failed call not billed, no escalation
    assert s.calls == 0


def test_consensus_uncertain_marker_is_not_a_vote():
    g = _Fake("gemini-flash-lite", 0.35, r"x^\?")
    q = _Fake("qwen-vl-flash", 0.08, r"x^\?")
    s = _Fake("claude-sonnet-5", 3.07, "x^2")
    r = pipeline.transcribe_with_consensus(
        JPEG, cheap=("gemini-flash-lite", "qwen-vl-flash"),
        get=_getter(**{g.name: g, q.name: q, s.name: s}))
    assert r.escalated and r.latex == "x^2"


def test_consensus_no_escalation_key_needs_human():
    """Readings disagree and there is no escalation key -> hand it to the student."""
    g = _Fake("gemini-flash-lite", 0.35, "x^2")
    q = _Fake("qwen-vl-flash", 0.08, "x^3")
    r = pipeline.transcribe_with_consensus(
        JPEG, cheap=("gemini-flash-lite", "qwen-vl-flash"),
        get=_getter(**{g.name: g, q.name: q}))
    assert r.needs_human and r.latex == ""


def test_single_reader_is_accepted_and_doubt_escalates():
    r"""Default chain: one trusted reader. \? in the reading is not a vote."""
    g = _Fake("gemini-flash-lite", 0.35, r"\frac{1}{16}\sin(16x)")
    s = _Fake("claude-sonnet-5", 3.07, "never")
    r = pipeline.transcribe_with_consensus(JPEG, get=_getter(**{g.name: g, s.name: s}))
    assert r.agreed and not r.escalated and r.latex == r"\frac{1}{16}\sin(16x)"
    assert s.calls == 0

    g2 = _Fake("gemini-flash-lite", 0.35, r"x^\?")
    s2 = _Fake("claude-sonnet-5", 3.07, "x^2")
    r2 = pipeline.transcribe_with_consensus(JPEG, get=_getter(**{g2.name: g2, s2.name: s2}))
    assert r2.escalated and r2.latex == "x^2"


def test_split_problems():
    page = (r"1. \sum \frac{2n}{3n+1} \text{ diverge.} "
            r"2. 3\sum (-1)^n \frac{1}{4^n} \text{ converge} "
            r"3. \sum \frac{1}{n^{3/2}}")
    parts = pipeline.split_problems(page)
    assert len(parts) == 3
    assert parts[0].startswith("1.") and parts[2].startswith("3.")
    assert pipeline.split_problems(r"(1) a (2) b") == ["(1) a", "(2) b"]
    assert pipeline.split_problems(r"\sin(x)") == [r"\sin(x)"]     # no numbering
    assert pipeline.split_problems("") == []


# --------------------------------------------------------------------------- #
# evaluator on 3 fake images
# --------------------------------------------------------------------------- #
class _Scripted:
    required_env = ()

    def __init__(self, name, cost, replies: dict[str, str]):
        self.name, self.cost_per_1k, self._replies = name, cost, replies

    def transcribe(self, image_bytes, *, hint=""):
        reply = self._replies[image_bytes.decode()]
        if reply == "ERR":
            raise RuntimeError("http 500")
        return reply


@pytest.fixture
def eval_dir(tmp_path):
    for stem, label in (("001", r"\frac{1}{2}"), ("002", "x^2+C"), ("003", r"\sqrt{x}")):
        (tmp_path / f"{stem}.jpg").write_bytes(stem.encode())
        (tmp_path / f"{stem}.tex").write_text(label + "\n", encoding="utf-8")
    (tmp_path / "004.tex").write_text("orphan", encoding="utf-8")  # no image: ignored
    return tmp_path


def test_run_eval_rates(eval_dir):
    good = _Scripted("a", 0.35, {"001": r"\frac{1}{2}", "002": "x^2+C", "003": r"\sqrt{x}"})
    mixed = _Scripted("b", 2.0, {"001": r"\frac{ 1 }{ 2 }", "002": "x^2+C", "003": "ERR"})
    ticks = iter(range(100))
    scores = evaluate.run_eval(
        eval_dir,
        ["a", "b", "missing"],
        get=_getter(a=good, b=mixed),
        clock=lambda: float(next(ticks)),
    )
    assert [s.name for s in scores] == ["a", "b"]
    a, b = scores
    assert (a.n, a.exact, a.normalized, a.errors) == (3, 3, 3, 0)
    assert a.exact_rate == 1.0
    assert (b.n, b.exact, b.normalized, b.errors) == (3, 1, 2, 1)
    assert b.exact_rate == pytest.approx(1 / 3)
    assert b.normalized_rate == pytest.approx(2 / 3)
    assert a.mean_seconds == 1.0
    assert a.total_cost_usd == pytest.approx(3 * 0.35 / 1000)
    assert b.total_cost_usd == pytest.approx(2 * 2.0 / 1000)  # errored call not billed
    table = evaluate.format_table(scores)
    assert "| a | 3 | 100% | 100% |" in table
    assert "| b | 3 | 33% | 67% |" in table
    out = evaluate.write_report(eval_dir, scores, today=date(2026, 9, 17))
    assert out.name == "eval-2026-09-17.md"
    text = out.read_text(encoding="utf-8")
    assert "ERROR: RuntimeError" in text and "| a | 3 |" in text


def test_run_eval_empty_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        evaluate.run_eval(tmp_path, [])


def test_cli_eval(monkeypatch, eval_dir, capsys):
    good = _Scripted("qwen-vl-flash", 0.08, {"001": r"\frac{1}{2}", "002": "x^2", "003": r"\sqrt{x}"})
    monkeypatch.setattr(evaluate.registry, "get", _getter(**{good.name: good}))
    assert cli.main(["--eval", str(eval_dir), "--providers", "qwen-vl-flash,gemini-flash-lite"]) == 0
    out = capsys.readouterr().out
    assert "| qwen-vl-flash | 3 | 67% | 67% |" in out
    assert "report:" in out and list(eval_dir.glob("eval-*.md"))


def test_cli_eval_no_provider_configured(eval_dir, capsys):
    assert cli.main(["--eval", str(eval_dir)]) == 2
    assert "no provider" in capsys.readouterr().err


def test_cli_image_with_provider_without_key(tmp_path, capsys):
    img = tmp_path / "a.jpg"
    img.write_bytes(JPEG)
    assert cli.main(["--image", str(img), "--provider", "qwen-vl-flash"]) == 2
    assert "DASHSCOPE_API_KEY" in capsys.readouterr().err


def test_no_key_literal_in_sources():
    import pathlib

    root = pathlib.Path(base.__file__).parent
    for f in list(root.glob("*.py")) + [pathlib.Path(pipeline.__file__), pathlib.Path(evaluate.__file__)]:
        src = f.read_text(encoding="utf-8")
        assert "sk-ant-" not in src and "AIza" not in src
