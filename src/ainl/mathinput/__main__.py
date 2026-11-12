"""Smoke CLI.

    python -m ainl.mathinput --latex '\\frac{1}{16}\\sin(16x)'
    python -m ainl.mathinput --image answer.jpg [--hint "indefinite integral, include +C"]
                                                [--provider qwen-vl-flash | --consensus]
    python -m ainl.mathinput --eval <dir> [--providers a,b,c]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .latex_to_mathml import latex_to_webassign_mathml
from .providers import registry
from .providers.base import ProviderNotConfigured
from .vision import API_KEY_ENV, MissingApiKeyError, handwriting_to_latex


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ainl.mathinput")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--latex", help="LaTeX expression to convert")
    group.add_argument("--image", type=Path, help="photo of a handwritten answer")
    group.add_argument("--eval", type=Path, metavar="DIR", help="score providers on DIR/NNN.jpg + NNN.tex")
    parser.add_argument("--hint", default="", help="question type hint for the vision model")
    parser.add_argument("--provider", help=f"one of: {', '.join(registry.names())}")
    parser.add_argument("--consensus", action="store_true", help="two cheap models + escalation")
    parser.add_argument("--providers", help="comma-separated provider names for --eval")
    args = parser.parse_args(argv)

    if args.latex is not None:
        print(latex_to_webassign_mathml(args.latex))
        return 0

    if args.eval is not None:
        return _eval(args)

    try:
        image_bytes = args.image.read_bytes()
    except OSError as exc:
        print(f"error: cannot read image: {exc}", file=sys.stderr)
        return 2

    if args.consensus:
        from .pipeline import transcribe_with_consensus

        result = transcribe_with_consensus(image_bytes, hint=args.hint)
        for name, latex in result.candidates.items():
            print(f"{name}: {latex}")
        for name, err in result.errors.items():
            print(f"{name}: {err}", file=sys.stderr)
        print(f"cost:   ${result.cost_usd:.4f}  agreed={result.agreed} escalated={result.escalated}")
        if result.needs_human:
            print("needs human review: candidates disagree", file=sys.stderr)
            return 1
        print(f"LaTeX:  {result.latex}")
        print(f"MathML: {result.mathml}")
        return 0

    try:
        if args.provider:
            latex = registry.get(args.provider).transcribe(image_bytes, hint=args.hint)
        else:
            latex = handwriting_to_latex(image_bytes, hint=args.hint)
    except KeyError as exc:
        print(f"error: {exc.args[0]}", file=sys.stderr)
        return 2
    except (MissingApiKeyError, ProviderNotConfigured) as exc:
        # exc already names the missing variable; don't append a second "is not set"
        print(f"error: {exc}", file=sys.stderr)
        print("hint: on Windows, [Environment]::SetEnvironmentVariable(...,'User') only "
              "affects NEW shells — open a fresh PowerShell window, or set it for this "
              "session with  $env:NAME = '...'", file=sys.stderr)
        return 2
    print(f"LaTeX:  {latex}")
    print(f"MathML: {latex_to_webassign_mathml(latex)}")
    return 0


def _eval(args: argparse.Namespace) -> int:
    from .evaluate import format_table, run_eval, write_report

    providers = [p.strip() for p in args.providers.split(",") if p.strip()] if args.providers else None
    try:
        scores = run_eval(args.eval, providers, hint=args.hint)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not scores:
        print("error: no provider is configured (set an API key env var)", file=sys.stderr)
        return 2
    print(format_table(scores))
    print(f"report: {write_report(args.eval, scores)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
