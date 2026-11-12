# Building the handwriting eval set (for the student)
Goal: ~100 photos of your own handwritten answers + the exact LaTeX for each, so we can score providers before choosing one.

1. Write one answer per sheet region, dark pen on white paper, the way you normally write for WebAssign.
2. Photograph straight down, good light, no shadow across the ink; crop so only that answer (1-3 lines) is in frame.
3. Resize to about 1000 px on the long edge, JPEG quality ~85 (phone "medium" export is fine).
4. Name the files `001.jpg`, `002.jpg`, ... in one folder (PNG/WebP also accepted).
5. Next to each image put `001.tex` containing ONE line of LaTeX that says exactly what is on the paper,
   mistakes included. Do not fix your maths in the label; the tool must transcribe, not grade.
6. Use plain commands: `\frac{1}{2}`, `\sqrt{x}`, `\sin(2x)`, `e^{2x}`, `\ln|x|`, `x^2+2x+C`. Spacing and `\left(` vs `(` do not matter.
7. Mix easy and hard: fractions, roots, powers, absolute values, +C, Greek letters, a few messy ones.
8. Run: `python -m ainl.mathinput --eval <folder> [--providers gemini-flash-lite,qwen-vl-flash]`
   (set the provider's API key in your shell first; providers without a key are skipped).
9. Read `<folder>/eval-<date>.md`: "exact" = character-identical, "normalized" = same after ignoring spacing/brackets.
   Look at the per-image outputs to see which symbols each provider gets wrong.
