"""The chat numbers every picture; answer lines keep every letter of a select-all part."""
from __future__ import annotations

import io
from types import SimpleNamespace

from PIL import Image

from ainl.bot.conversation import Conversation


def _png(w, h):
    out = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(out, "PNG")
    return out.getvalue()



def test_filled_and_graded_pictures_carry_their_question_number():
    ms = SimpleNamespace(targets={1: ["RA_11_0_0_91"], 3: ["RA_33_2_0_93"]},
                         taps=[{"n": 2, "name": "RB_22_1_0_92"}])
    caps = Conversation._shot_captions(ms, ["question22_1", "question33_2", "question11_0",
                                            "question99_9"], 4, "graded")
    assert caps == ["2. graded", "3. graded", "1. graded", "graded"]
    # Fewer ids than pictures (an older WebAssign reply): still one caption each.
    assert Conversation._shot_captions(ms, [], 2, "filled") == ["filled", "filled"]






def test_a_select_all_part_keeps_every_letter():
    """9.2 Homework q1(b), 2026-09-27: Gemini answered "1.c2 B, D" (y = 0.5 and
    y = 1.5) and only B was ticked — the line was read as one letter."""
    from ainl.autosolve import parse_answers, _describe, Item
    got = parse_answers("1.c1 D\n1.c2 B, D\n2.c1 (C)\n3.c1 B (y = 0.5)\n4.c1 A and C\n5.c1 b;d")
    assert got.choices == {1: {1: "D", 2: "BD"}, 2: {1: "C"}, 3: {1: "B"}, 4: {1: "AC"}, 5: {1: "BD"}}
    d = _describe(Item(n=1, choices=[["", ""], ["y = 0", "y = 0.5"]], multi=[False, True]))
    assert "choice c1: A)" in d and "choice c2 (SELECT ALL that apply" in d
