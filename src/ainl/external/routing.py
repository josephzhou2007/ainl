"""Where a piece of work actually has to be handed in.

This is not obvious from Canvas, and getting it wrong costs real marks. In
PHYSICS 247 (read 2026-09-19) the three problem sets do three different things:

    Problem Set 1   online_upload   -> Canvas holds the submission
    Problem Set 2   online_upload   -> Canvas, and Gradescope separately
    Problem Set 3   external_tool   -> Gradescope, which sends the grade back

So "do I have to submit this twice?" has no general answer — it is a property
of each assignment, and it is readable from submission_types plus the tool the
assignment points at. The student should be told, not left to guess.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .platforms import classify


@dataclass(frozen=True)
class Route:
    where: str                 # "canvas" | a platform key
    label: str                 # what to tell the student
    syncs_to_canvas: bool      # does a grade there come back to Canvas?
    canvas_native: bool        # can we submit it through the Canvas API?


def route(detail: dict, course_has_gradescope: bool = False) -> Route:
    """Where this assignment is handed in, from what Canvas says about it."""
    types = tuple(detail.get("submission_types") or ())
    tool = detail.get("external_tool_url") or ""

    if "external_tool" in types:
        platform = classify(tool)
        if platform is None:
            return Route("external", "an external site", False, False)
        # An external_tool assignment IS the Canvas submission: the tool reports
        # the grade back over LTI, so handing it in there is handing it in.
        return Route(platform.key, platform.label, True, False)

    # A Canvas-native assignment. If the course also runs Gradescope, that is a
    # separate system with no link to this submission — both are needed.
    if course_has_gradescope:
        return Route("canvas", "Canvas (and Gradescope separately)", False, True)
    return Route("canvas", "Canvas", False, True)


def describe(r: Route) -> str:
    """One line for the student, in terms of what they have to do."""
    if r.where == "canvas" and "Gradescope" in r.label:
        return ("📥 Hands in to <b>Canvas</b> — and this course also uses "
                "<b>Gradescope</b>, which is separate. Both are needed.")
    if r.where == "canvas":
        return "📥 Hands in to <b>Canvas</b>, right here."
    if r.syncs_to_canvas:
        return (f"📥 Hands in on <b>{r.label}</b>. The grade comes back to Canvas "
                f"by itself — one submission, not two.")
    return f"📥 Hands in on <b>{r.label}</b>."


def has_gradescope(tabs: list) -> bool:
    return any("gradescope" in str(t.get("label", "")).lower() for t in (tabs or []))
