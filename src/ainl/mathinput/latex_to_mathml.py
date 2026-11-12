"""LaTeX -> WebAssign (WIRIS MathType dialect) MathML.

Pipeline: ``latex2mathml`` produces standard MathML; we then parse it into an
ElementTree and normalise the tree into the dialect that WebAssign stores in
its ``RA_*`` hidden fields (see docs/cengage-webassign-api.md, section 9):

* fixed shell ``<math xmlns=...><mspace/>...</math>``
* function names as ``<mi>sin</mi>`` followed by ``<mfenced separators="">``
* ordinary parentheses as ``<mfenced>`` wrapping one ``<mrow>``
* decimals split into ``<mn>0</mn><mo>.</mo><mn>5</mn>``
* wrapper ``<mrow>``/``<mstyle>`` with a single child removed
* non-ASCII characters emitted as short hex entities (``&#x3C0;``)

Structures we do not recognise are passed through unchanged.

Known not to be handled (so an answer using one should be checked by eye in the
rendered preview before it is submitted):

* multi-letter text — ``\\text{}`` / ``\\mathrm{}`` pass through as a bare ``<mi>``
* matrices and any ``\\begin{...}`` environment; ``\\underbrace`` and friends
* ``\\left.`` / ``\\right.`` empty delimiters and ``\\{ \\}`` braces — unverified
* limits on ``\\int`` ``\\sum`` ``\\lim`` come through as ``msubsup``; WIRIS may
  itself use ``munderover``
* ``|x|`` is paired by simple alternation, so nested absolute values can mis-pair
* function names outside ``FUNCTION_NAMES`` (other than ``\\operatorname``) do not
  get ``separators=""``
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from latex2mathml.converter import convert as _l2m_convert

MATHML_NS = "http://www.w3.org/1998/Math/MathML"
SHELL_OPEN = f'<math xmlns="{MATHML_NS}"><mspace/>'
SHELL_CLOSE = "</math>"

# Function names that WebAssign renders as <mi>name</mi><mfenced separators="">.
FUNCTION_NAMES = frozenset(
    """sin cos tan sec csc cot
    sinh cosh tanh sech csch coth
    arcsin arccos arctan arcsec arccsc arccot
    ln log exp lg
    min max lim det dim gcd deg arg sup inf
    """.split()
)

_OPEN_TO_CLOSE = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = set(_OPEN_TO_CLOSE.values())
_SCRIPT_TAGS = {"msup", "msub", "msubsup"}
# Operator characters that the dialect writes as plain ASCII.
_CHAR_MAP = {"−": "-"}
_DECIMAL_RE = re.compile(r"^(\d+)\.(\d+)$")


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def latex_to_webassign_mathml(latex: str) -> str:
    """Convert a LaTeX string to WebAssign-dialect MathML (with the outer shell)."""
    body = latex_to_webassign_body(latex)
    return f"{SHELL_OPEN}{body}{SHELL_CLOSE}"


def latex_to_webassign_body(latex: str) -> str:
    """Same as :func:`latex_to_webassign_mathml` but without the ``<math>`` shell."""
    latex = latex.strip()
    if not latex:
        return ""
    raw = _l2m_convert(latex)
    root = _parse(raw)
    children = list(root)
    children = _normalise_children(children)
    # The dialect has no top-level <mrow>: content sits directly after <mspace/>.
    if len(children) == 1 and children[0].tag == "mrow":
        children = list(children[0])
    return "".join(_serialise(c) for c in children)


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def _parse(raw: str) -> ET.Element:
    # Drop the default namespace so tags come back as plain "mi", "mo", ...
    raw = re.sub(r'\sxmlns="[^"]*"', "", raw, count=1)
    return ET.fromstring(raw)


# --------------------------------------------------------------------------- #
# tree normalisation
# --------------------------------------------------------------------------- #
def _normalise_children(children: list[ET.Element]) -> list[ET.Element]:
    """Normalise a sibling list. Returns a new list (elements may be rebuilt)."""
    out: list[ET.Element] = []
    for el in children:
        out.extend(_normalise_element(el))
    out = _fence(out)
    out = _mark_function_fences(out)
    return out


def _normalise_element(el: ET.Element) -> list[ET.Element]:
    """Normalise one element; may return several (inline-unwrap) or none."""
    tag = el.tag

    if tag == "mspace":  # thin spaces from \, \; etc. -- the dialect has none
        return []

    if tag in ("mrow", "mstyle"):
        inner = _normalise_children(list(el))
        # A wrapper mrow is only kept when it groups several siblings.
        if tag == "mstyle" or len(inner) <= 1:
            return inner
        row = ET.Element("mrow")
        row.extend(inner)
        return [row]

    if tag == "mn":
        text = (el.text or "").strip()
        m = _DECIMAL_RE.match(text)
        if m:
            return [_leaf("mn", m.group(1)), _leaf("mo", "."), _leaf("mn", m.group(2))]
        return [_leaf("mn", text)]

    if tag == "mo":
        text = _map_chars((el.text or "").strip())
        # \operatorname{...} arrives as an alphabetic <mo>; the dialect wants <mi>.
        if text.isalpha() and len(text) > 1:
            return [_leaf("mi", text)]
        return [_leaf("mo", text, keep=_fence_attrs(el))]

    if tag in ("mi", "mtext"):
        text = _map_chars((el.text or "").strip())
        new = _leaf(tag, text)
        if el.get("mathvariant") and el.get("mathvariant") != "normal":
            new.set("mathvariant", el.get("mathvariant"))
        return [new]

    # Structural elements: normalise each child slot individually.
    new = ET.Element(tag, dict(el.attrib))
    for child in el:
        slot = _normalise_children([child])
        new.append(_as_single(slot))
    return [new]


def _as_single(items: list[ET.Element]) -> ET.Element:
    """Collapse a list into one element (wrapping in mrow when needed)."""
    if len(items) == 1:
        return items[0]
    row = ET.Element("mrow")
    row.extend(items)
    return row


def _leaf(tag: str, text: str, keep: dict | None = None) -> ET.Element:
    el = ET.Element(tag, keep or {})
    el.text = text
    return el


def _map_chars(text: str) -> str:
    return "".join(_CHAR_MAP.get(ch, ch) for ch in text)


def _fence_attrs(mo: ET.Element) -> dict:
    """Keep only the ``form`` hint of \\left/\\right fences (used for ``|``)."""
    form = mo.get("form")
    return {"form": form} if form in ("prefix", "postfix") else {}


# --------------------------------------------------------------------------- #
# fencing: <mo>(</mo> ... <mo>)</mo>  ->  <mfenced><mrow>...</mrow></mfenced>
# --------------------------------------------------------------------------- #
def _is_mo(el: ET.Element, chars: set[str] | str) -> bool:
    return el.tag == "mo" and (el.text or "") in chars


def _opener_of(el: ET.Element) -> str | None:
    """Return the opening char if ``el`` opens a fence, else None."""
    if el.tag != "mo":
        return None
    t = el.text or ""
    if t in _OPEN_TO_CLOSE:
        return t
    if t == "|" and el.get("form") != "postfix":
        return t
    return None


def _closes(el: ET.Element, close_char: str) -> bool:
    """Does ``el`` close a fence expecting ``close_char``?

    ``el`` may be the closing <mo> itself, or a script (msup/msub/msubsup)
    whose base is the closing <mo> -- latex2mathml emits ``(4x)^2`` as
    ``... <msup><mo>)</mo><mn>2</mn></msup>``.
    """
    base = el
    if el.tag in _SCRIPT_TAGS and len(el):
        base = el[0]
    if base.tag != "mo" or (base.text or "") != close_char:
        return False
    if close_char == "|" and base.get("form") == "prefix":
        return False
    return True


def _fence(items: list[ET.Element]) -> list[ET.Element]:
    out: list[ET.Element] = []
    i = 0
    while i < len(items):
        el = items[i]
        open_char = _opener_of(el)
        if open_char is None:
            out.append(el)
            i += 1
            continue
        close_char = _OPEN_TO_CLOSE.get(open_char, open_char)
        j = _find_close(items, i, open_char, close_char)
        if j is None:  # unmatched -> pass through
            out.append(_strip_form(el))
            i += 1
            continue
        inner = _fence(items[i + 1 : j])
        fenced = ET.Element("mfenced")
        if open_char != "(":
            fenced.set("open", open_char)
            fenced.set("close", close_char)
        row = ET.Element("mrow")
        row.extend(inner)
        fenced.append(row)
        closer = items[j]
        if closer.tag in _SCRIPT_TAGS:
            # (4x)^2 : the script's base becomes the fenced group
            closer.remove(closer[0])
            closer.insert(0, fenced)
            out.append(closer)
        else:
            out.append(fenced)
        i = j + 1
    return out


def _find_close(items, start: int, open_char: str, close_char: str) -> int | None:
    depth = 0
    for k in range(start, len(items)):
        el = items[k]
        if k > start and _closes(el, close_char):
            if depth == 0:
                return k
            depth -= 1
            continue
        if k > start and _opener_of(el) == open_char and open_char != "|":
            depth += 1
    return None


def _strip_form(el: ET.Element) -> ET.Element:
    el.attrib.pop("form", None)
    return el


def _is_function_head(el: ET.Element) -> bool:
    """<mi>sin</mi>, or a script whose base is such an <mi> (sin^2, log_2)."""
    if el.tag == "mi":
        return (el.text or "") in FUNCTION_NAMES
    if el.tag in _SCRIPT_TAGS and len(el):
        return _is_function_head(el[0])
    return False


def _mark_function_fences(items: list[ET.Element]) -> list[ET.Element]:
    for idx in range(1, len(items)):
        el = items[idx]
        if el.tag == "mfenced" and "open" not in el.attrib and _is_function_head(items[idx - 1]):
            el.set("separators", "")
    return items


# --------------------------------------------------------------------------- #
# serialisation
# --------------------------------------------------------------------------- #
def _escape_text(text: str) -> str:
    out = []
    for ch in text:
        if ch == "&":
            out.append("&amp;")
        elif ch == "<":
            out.append("&lt;")
        elif ch == ">":
            out.append("&gt;")
        elif ord(ch) > 0x7E:
            out.append(f"&#x{ord(ch):X};")
        else:
            out.append(ch)
    return "".join(out)


def _serialise(el: ET.Element) -> str:
    attrs = "".join(
        f' {k}="{v.replace("&", "&amp;").replace(chr(34), "&quot;")}"'
        for k, v in el.attrib.items()
        if k != "form"
    )
    if len(el) == 0:
        text = el.text or ""
        if not text and el.tag == "mspace":
            return "<mspace/>"
        return f"<{el.tag}{attrs}>{_escape_text(text)}</{el.tag}>"
    inner = "".join(_serialise(c) for c in el)
    return f"<{el.tag}{attrs}>{inner}</{el.tag}>"
