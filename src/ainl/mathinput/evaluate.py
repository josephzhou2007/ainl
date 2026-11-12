"""Score vision providers on a labelled directory of handwritten answers.

Layout: ``<dir>/NNN.jpg`` (or .png/.jpeg/.webp) + ``<dir>/NNN.tex`` holding the
human-labelled one-line LaTeX.  See docs/eval-set-howto.md.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from .pipeline import same_expression
from .providers import registry
from .providers.base import ProviderNotConfigured, VisionProvider

IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


@dataclass
class Sample:
    stem: str
    image: Path
    expected: str


@dataclass
class ProviderScore:
    name: str
    cost_per_1k: float
    n: int = 0
    exact: int = 0
    normalized: int = 0
    errors: int = 0
    seconds: float = 0.0
    outputs: dict[str, str] = field(default_factory=dict)  # stem -> latex or "ERROR: ..."

    @property
    def exact_rate(self) -> float:
        return self.exact / self.n if self.n else 0.0

    @property
    def normalized_rate(self) -> float:
        return self.normalized / self.n if self.n else 0.0

    @property
    def mean_seconds(self) -> float:
        return self.seconds / self.n if self.n else 0.0

    @property
    def total_cost_usd(self) -> float:
        return (self.n - self.errors) * self.cost_per_1k / 1000.0


def load_samples(directory: Path) -> list[Sample]:
    samples = []
    for tex in sorted(directory.glob("*.tex")):
        image = next((tex.with_suffix(s) for s in IMAGE_SUFFIXES if tex.with_suffix(s).exists()), None)
        if image is None:
            continue
        samples.append(Sample(tex.stem, image, tex.read_text(encoding="utf-8").strip()))
    return samples


def run_eval(
    directory: Path,
    providers: list[str] | None = None,
    *,
    hint: str = "",
    get: Callable[[str], VisionProvider] | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> list[ProviderScore]:
    """Run every provider on every sample; providers without a key are skipped."""
    samples = load_samples(directory)
    if not samples:
        raise FileNotFoundError(f"no NNN.tex + NNN.jpg pairs in {directory}")
    names = providers if providers is not None else registry.available()
    get = get or registry.get
    scores: list[ProviderScore] = []
    for name in names:
        try:
            provider = get(name)
        except ProviderNotConfigured:
            continue
        score = ProviderScore(name=provider.name, cost_per_1k=provider.cost_per_1k)
        for s in samples:
            image_bytes = s.image.read_bytes()
            score.n += 1
            t0 = clock()
            try:
                latex = provider.transcribe(image_bytes, hint=hint)
            except Exception as exc:
                score.errors += 1
                score.outputs[s.stem] = f"ERROR: {type(exc).__name__}"
                score.seconds += clock() - t0
                continue
            score.seconds += clock() - t0
            score.outputs[s.stem] = latex
            if latex.strip() == s.expected:
                score.exact += 1
            if same_expression(latex, s.expected):
                score.normalized += 1
        scores.append(score)
    return scores


def format_table(scores: list[ProviderScore]) -> str:
    lines = [
        "| provider | n | exact | normalized | mean s | est. cost USD | errors |",
        "|---|---|---|---|---|---|---|",
    ]
    for sc in scores:
        lines.append(
            f"| {sc.name} | {sc.n} | {sc.exact_rate:.0%} | {sc.normalized_rate:.0%} | "
            f"{sc.mean_seconds:.2f} | {sc.total_cost_usd:.4f} | {sc.errors} |"
        )
    return "\n".join(lines)


def write_report(directory: Path, scores: list[ProviderScore], *, today: date | None = None) -> Path:
    today = today or date.today()
    out = directory / f"eval-{today.isoformat()}.md"
    body = [f"# Handwriting eval {today.isoformat()}", "", format_table(scores), ""]
    for sc in scores:
        body.append(f"## {sc.name}")
        body.append("")
        for stem, latex in sc.outputs.items():
            body.append(f"- {stem}: `{latex}`")
        body.append("")
    out.write_text("\n".join(body), encoding="utf-8")
    return out
