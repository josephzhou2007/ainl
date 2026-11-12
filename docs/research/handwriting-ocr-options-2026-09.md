# Handwritten-math photo -> LaTeX: lower-cost options (research, 2026-09-17)

Scope: one phone photo of 1-3 handwritten calculus lines -> one LaTeX line -> MathML -> WebAssign.
Baseline today: Anthropic Messages API, `claude-sonnet-5`, vision. Bar: ~95% symbol-level accuracy on neat writing.
All prices fetched 2026-09-17 from the URL in each row. No model was executed; nothing was installed.

## 0. Cost model used everywhere below

- Standard image **S = 1000x1000 px** (pre-resize the phone photo to ~1 MP; a 1-3 line answer does not need more).
- Prompt 40 text tokens, output 40 tokens (one LaTeX line). Text tokens are ~1-5% of cost; the image dominates.
- Per-1k-image cost = 1000 x [(image_tokens + 40) x in_price + 40 x out_price] / 1e6.
- Batch/async discounts (50% at Anthropic, OpenAI, Google) are noted but **not** used: the student is waiting on the result.

Image-token rules (official docs):

| Provider | Rule | Tokens for S | Source |
|---|---|---|---|
| Anthropic | ceil(w/28) x ceil(h/28); doc table lists 1000x1000 = 1296 | 1296 | https://platform.claude.com/docs/en/build-with-claude/vision |
| OpenAI patch models (gpt-4.1-mini, gpt-5.4-mini/nano) | 32-px patches x multiplier (4.1-mini 1.62; 5.4-mini/nano 1.2) | 1024 patches -> 1659 / 1229 | https://developers.openai.com/api/docs/guides/images-vision |
| OpenAI gpt-4o-mini (tile model) | 2,833 base + 5,667 per 512-px tile (high detail); base only at low detail | 768x768 after scaling = 4 tiles -> 25,501 | same |
| Gemini 2.5 | 258 tokens if <=384 px, else 768-px tiles x 258 | ~1,032 (4 tiles) | https://ai.google.dev/gemini-api/docs/image-understanding |
| Gemini 3.x | `media_resolution`: low 280 / medium 560 / high (default) 1120 / ultra 2240 | 1120 (high) | https://ai.google.dev/gemini-api/docs/media-resolution |
| Qwen VL (Alibaba) | 28x28 block = 1 token, default max 1280 tokens/image | ~1280 | https://www.alibabacloud.com/help/en/model-studio/vision (search snippet; page did not expose the formula text - verify) |
| Mistral / DeepSeek | not verified on official pages this session | assumed ~1,300-4,100 | flagged below |

## 1. Hosted VLMs

| Model | $/1M in / out | Arithmetic (S) | $/1k images | Handwritten-math evidence | Output | Risks | Price source |
|---|---|---|---|---|---|---|---|
| Claude Sonnet 5 (baseline) | 2 / 10 | 1336x2 + 40x10 = 3,072 uUSD | **3.07** | No public HMER benchmark; in-project experience is the only evidence | LaTeX via prompt | Highest cost here; per-min rate limits fine at this volume | https://claude.com/pricing |
| Claude Haiku 4.5 | 1 / 5 | 1336x1 + 40x5 = 1,536 | **1.54** | Unknown (no HMER benchmark found) | LaTeX via prompt | 200K ctx irrelevant; older generation | same |
| Claude Opus 5 | 5 / 25 | 1336x5 + 40x25 = 7,680 | 7.68 | Unknown | LaTeX | Only as last-resort escalation | same |
| OpenAI gpt-4.1-mini | 0.40 / 1.60 | 1699x0.40 + 40x1.6 = 744 | **0.74** | Unknown; GPT-4o-mini scored well below Gemini 2.5 Flash on FERMAT handwritten-math OCR (see sec. 3) | LaTeX | 4.1 family is a 2025 model; watch deprecations | https://developers.openai.com/api/docs/pricing |
| OpenAI gpt-5.4-nano | 0.20 / 1.25 | 1269x0.20 + 40x1.25 = 304 | **0.30** | Unknown | LaTeX | Reasoning tokens may add output cost - set minimal reasoning | same |
| OpenAI gpt-5.4-mini | 0.75 / 4.50 | 1269x0.75 + 40x4.5 = 1,132 | 1.13 | Unknown | LaTeX | same | same |
| OpenAI gpt-4o-mini (high detail) | 0.15 / 0.60 | 25,501x0.15 + 40x0.6 = 3,849 | 3.85 | GPT-4o Mini evaluated in FERMAT (mid-pack) | LaTeX | **Trap**: tile pricing makes it as dear as Sonnet; low-detail (512 px) = $0.45 but too blurry for math | same + images guide |
| OpenAI gpt-5-mini / nano | 0.25/2.00, 0.05/0.40 | n/a | n/a | - | - | Images guide says "Deprecated and scheduled for shutdown" - do not adopt | images guide URL above |
| Gemini 3.1 Flash-Lite | 0.25 / 1.50 | 1160x0.25 + 40x1.5 = 350 | **0.35** | Unknown for 3.x; Gemini 2.5 Flash was #1 of 15 VLMs on FERMAT handwritten-math OCR (PINK 0.935) | LaTeX | Shutdown **May 7, 2027** | https://ai.google.dev/gemini-api/docs/pricing ; https://ai.google.dev/gemini-api/docs/deprecations |
| Gemini 3.5 Flash-Lite | 0.30 / 2.50 | 1160x0.30 + 40x2.5 = 448 | **0.45** | as above | LaTeX | Thinking may bill extra output tokens - set thinking low | pricing URL above |
| Gemini 3.8 Flash | 0.75 / 3.75 (doubles 2027-01-01 to 1.50 / 7.50) | 1160x0.75 + 40x3.75 = 1,020 | 1.02 (-> 2.04 in 2027) | as above | LaTeX | Announced price doubling | pricing URL above |
| Gemini 2.5 Flash-Lite | 0.10 / 0.40 | 1072x0.10 + 40x0.4 = 123 | 0.12 | 2.5 family has the strongest published handwriting evidence | LaTeX | Listed as **deprecated**, "no shutdown date announced" - fine for a semester, not a foundation | pricing + deprecations URLs |
| Gemini free tier | 0 | - | **0.00** | same models | LaTeX | Free-tier data may be used for training; limits only visible in AI Studio; 10-60/day is plausibly within them | https://ai.google.dev/gemini-api/docs/rate-limits |
| Qwen3-VL-Flash (Alibaba intl.) | 0.05-0.12 / 0.40-0.96 (tiered; short prompts hit the low tier) | 1320x0.05 + 40x0.4 = 82 | **0.08** | Qwen2.5-VL-72B was #2 on FERMAT; Flash size unknown; Qwen2.5-VL report claims upgraded OCR | LaTeX | Singapore endpoint; 1M free tokens/90 days; account setup friction; China-region data-handling questions | https://www.alibabacloud.com/help/en/model-studio/model-pricing |
| Qwen3-VL-Plus (Alibaba intl.) | 0.20-0.60 / 1.6-4.8 | 1320x0.20 + 40x1.6 = 328 | 0.33 | as above | LaTeX | same | same |
| qwen-vl-ocr (Alibaba) | 0.07 / 0.16 | 1320x0.07 + 40x0.16 = 99 | 0.10 | Document-OCR model; handwritten math **unknown** | text/LaTeX? | Check whether it emits LaTeX for formulas before relying on it | same |
| Qwen3-VL-8B-Instruct via OpenRouter | 0.117 / 0.455 | 1320x0.117 + 40x0.455 = 173 | 0.17 | 8B class; below 72B on FERMAT (numbers not extracted) | LaTeX | Provider routing varies; quality/latency not guaranteed | https://openrouter.ai/qwen/qwen3-vl-8b-instruct (search snippet) |
| DeepSeek `deepseek-flash` (vision) | 0.30 / 1.20 peak; half off-peak | assume 1300 img tok: 1340x0.30 + 40x1.2 = 450 | ~0.45 peak / 0.22 off-peak | Unknown | LaTeX | Image-token rule not verified; peak/off-peak pricing; `deepseek-v4-pro` has no vision | https://api-docs.deepseek.com/quick_start/pricing |
| Mistral Small 4 / Ministral 3 8B | 0.15 / 0.60 ; 0.15 / 0.15 | image tokens **not verified** (Pixtral used 16-px patches -> ~4k tokens) | ~0.6 (estimate) | Pixtral 12B/124B were mid/low on FERMAT; Pixtral is **retired** (12B 2025-12-31, Large 2026-05-31) | LaTeX | Successors' handwriting quality unknown | https://mistral.ai/pricing/api ; https://docs.mistral.ai/getting-started/models/models_overview |
| Mistral OCR 4.1 | $4 / 1k pages | flat | 4.00 | Document OCR; handwritten math unknown | Markdown+LaTeX | Costlier than Sonnet for single-line images | https://mistral.ai/pricing/api |

## 2. Math-specific OCR API

| Service | Price | $/1k images | Handwriting | Output | Ops | Risks | Source |
|---|---|---|---|---|---|---|---|
| Mathpix Convert API (image OCR) | $0.002/image (<=1M), $19.99 one-time setup, $29 test credit | **2.00** | Explicitly supported: "Printed or handwritten text, math..."; no public accuracy numbers | **LaTeX, MathML, AsciiMath natively** - would delete the LaTeX->MathML step | Trivial REST | Per-image price fixed regardless of image size; no free ongoing tier; single vendor | https://mathpix.com/pricing/api ; https://mathpix.com/ocr |
| Mathpix Snip (desktop) | Free / Pro from $4.99/mo | n/a (interactive) | same engine | LaTeX/MathML | Not an API | Not automatable for a pipeline | https://mathpix.com/pricing |

## 3. Handwritten-math accuracy evidence (what actually exists)

- **FERMAT / over-correction study** (arXiv 2604.22774, 15 VLMs on 2,244 multi-line handwritten math samples): Gemini 2.5 Flash best (PINK 0.935), Qwen2.5-VL-72B 0.920, GPT-4o 0.907; GPT-4o-mini, Pixtral, LLaMA 3.2 lower. **Key finding: VLMs silently "fix" what the student wrote 42-66% of the time**, more so for stronger models. https://arxiv.org/html/2604.22774
- **MathWriting** (Google): offline OCR CER 7.17%, CTC-transformer 5.49%, PaLI-class VLMs ~5.95%. i.e., even purpose-built systems make ~1 character error per 17-20 characters on general handwriting; neat single-line calculus answers are easier than this benchmark. https://arxiv.org/html/2404.10690
- **Specialised HMER models** (ICAL, TAMER etc.): ~69% exact-expression rate on HME100K; CROHME ExpRate in the 60s. Exact-match on whole expressions is hard; symbol-level accuracy is much higher. https://arxiv.org/pdf/2408.08578
- No vendor (Anthropic, OpenAI, Google, Mathpix, Alibaba) publishes a handwritten-math benchmark for its current small model. **The only reliable number will be your own ~100-image eval set** (see sec. 7).

## 4. Self-hosted / open weights (Mac mini, Apple Silicon 16-24 GB)

| Model | Handwriting? | Evidence | Output | Runs on Mac mini? | Verdict | Source |
|---|---|---|---|---|---|---|
| pix2tex / LaTeX-OCR | **No** (rendered LaTeX training data; README: handwriting "kinda done") | BLEU 0.88 / token acc 0.60 on **printed** | LaTeX | Yes (small) | Printed only - skip | https://github.com/lukas-blecher/LaTeX-OCR |
| Nougat (Meta) | No - academic PDF pages | n/a | Markdown+LaTeX | Heavy for CPU | Skip | (project scope; not re-fetched) |
| Pix2Text MFR (TrOCR-based) | Yes, claimed "higher accuracy, especially for handwritten formulas" | Own 485-image test set; no CROHME numbers | LaTeX | Yes, CPU; Mac app exists | Cheapest local candidate; accuracy unproven | https://github.com/breezedeus/pix2text ; https://huggingface.co/breezedeus/pix2text-mfr |
| UniMERNet (via PaddleOCR) | Yes - trained on UniMER-1M incl. CROHME + HME100K; has an HWE (handwritten) eval split | Published per-split BLEU/EditDist | LaTeX | Yes (~0.3B), CPU ok | Best-documented local option for handwriting | http://www.paddleocr.ai/v2.10.0/en/algorithm/formula_recognition/algorithm_rec_unimernet.html |
| PaddleOCR-VL 0.9B | Formula CDM 0.945 (OmniDocBench); handwriting not separately reported | Paper | Markdown+LaTeX | 0.9B fits; paper benchmarks on A100 | Possible, unproven on handwriting | https://arxiv.org/html/2510.14528v1 |
| Qwen2.5-VL-7B / Qwen3-VL-8B (MLX 4-bit) | Yes (general VLM) | 72B is strong on FERMAT; 7-8B weaker, numbers not extracted | LaTeX | Yes: 4-bit ~5-6 GB; base M4 16 GB does ~25 tok/s on 7B text, VL slower; 1-line answer = a few seconds | Best local accuracy/effort ratio | https://huggingface.co/mlx-community/Qwen2.5-VL-32B-Instruct-4bit ; https://www.popularai.org/p/mac-mini-llm-performance-in-2026 |
| MiniCPM-V | General VLM | Not researched this session - unknown | LaTeX | Yes | Unknown | - |
| TrOCR (vanilla) | Text lines, not math | - | text | Yes | Skip | - |

Local ops cost: Mac mini already owned -> $0/image, but you own uptime, model updates, and an eval harness; electricity is negligible at <100 images/day.

## 5. Ranked table (cost vs expected accuracy on neat handwritten calculus)

| Rank | Option | $/1k | Expected accuracy (judgement, not measured) | Why this rank |
|---|---|---|---|---|
| 1 | Gemini 3.x Flash-Lite (3.1 or 3.5) | 0.35-0.45 | High (Gemini family led the only comparative handwriting study) | Cheap, strong lineage, generous free tier for dev |
| 2 | Qwen3-VL-Flash (Alibaba) | 0.08 | Med-high (Qwen VL lineage #2 in study; Flash size unverified) | Cheapest hosted; account/region friction |
| 3 | Mathpix Convert | 2.00 | High on handwriting (purpose-built); gives MathML directly | Removes a conversion step; costs more than Sonnet-batch but less than Sonnet-realtime |
| 4 | OpenAI gpt-5.4-nano / gpt-4.1-mini | 0.30 / 0.74 | Medium (no handwriting evidence for these) | Cheap, but 4o-mini heritage scored mid-pack |
| 5 | Claude Haiku 4.5 | 1.54 | Medium-high (unknown) | Same vendor as today; half of Sonnet |
| 6 | Claude Sonnet 5 (today) | 3.07 | High (in-project evidence) | Keep as the escalation tier, not the first call |
| 7 | Local Qwen3-VL-8B (MLX) | 0.00 | Medium | Free, private, slower; needs an eval before trusting |
| 8 | Local UniMERNet / Pix2Text | 0.00 | Medium-low on messy input, ok on neat single lines | Tiny models; brittle on multi-line + surrounding text |
| - | gpt-4o-mini high detail, Mistral OCR, Opus 5 | 3.85-7.68 | - | Dominated on cost |

## 6. Recommended tiered chain

```
photo -> resize long edge to 1000 px, JPEG q85
T1  Gemini 3.x Flash-Lite  (prompt: "transcribe VERBATIM to one LaTeX line; do not simplify or correct")
T1' Qwen3-VL-Flash          (same prompt)          <- run in parallel with T1
   sanity: both parse (latex2mathml / sympy), and canonicalised strings agree  -> accept (cost ~$0.43/1k)
T2  on disagreement or parse failure -> Claude Sonnet 5 (today's model)  -> accept
T3  (optional) if Sonnet also disagrees with both -> show the student the 3 candidates; never auto-submit
```

Cost per 1k images = 0.35 + 0.08 + p_escalate x 3.07. With p_escalate = 25%: **~$1.20/1k** (vs $3.07 Sonnet-only). With p = 10%: $0.74/1k.

| Load | Images/month | Chain @25% escalation | Sonnet-only today |
|---|---|---|---|
| 1 student, 10/day | 300 | $0.36 | $0.92 |
| 1 student, 60/day | 1,800 | $2.16 | $5.53 |
| 50 students, 10/day | 15,000 | $18 | $46 |
| 50 students, 60/day | 90,000 | $108 | $276 |

At 1 student the absolute dollars are trivial either way; the chain matters at 50 students, and even more as a **quality** mechanism (two independent readings catch single-model hallucinations).

## 7. Confidence-check trick (two cheap models, escalate on disagreement)

- Normalise before comparing: strip whitespace, `\left/\right`, `\dfrac->\frac`, `{x}->x` for single tokens, then compare either the string or `sympy` parse (`latex2sympy2`) or the generated MathML.
- Cost math: 2 cheap calls = $0.43/1k. If they agree with rate a and expensive call costs E=$3.07: total = 0.43 + (1-a)x3.07. a=0.90 -> $0.74; a=0.75 -> $1.20; a=0.50 -> $1.97. Break-even with Sonnet-only is a = 14% agreement, so the trick pays off unless the cheap models are nearly useless.
- Risk: correlated errors (both models "fix" a student's mistake the same way - the FERMAT over-correction effect). Mitigate with the verbatim prompt and by preferring models from different families (Gemini + Qwen, not Gemini + Gemini).
- Cheaper variant for T2: use Haiku 4.5 ($1.54) instead of Sonnet if your own eval shows it is as good; total drops to ~$0.82/1k at 25% escalation.

## 8. Fully local on the Mac mini

- Feasible path: Qwen3-VL-8B (or Qwen2.5-VL-7B) 4-bit via MLX as T1, UniMERNet as the second opinion, hosted Sonnet only when they disagree (or never, for a strictly-offline mode).
- Realistic caveats: (a) no published handwriting number for the 7-8B class - expect noticeably below Gemini/Sonnet on messy strokes, roots, and stacked fractions; (b) 3-10 s latency per image on a base M4; (c) small HMER models are trained on isolated expressions - crop tightly, one expression per image, or they degrade; (d) you must build the eval set anyway to know whether it clears 95%.
- Verdict: good for privacy/zero-cost and for the "second opinion" slot; not a safe sole engine for graded submissions until measured.

## 9. Biggest uncertainties

1. **No vendor benchmark for handwritten math on any current small model.** Build a 100-photo labelled set from the student's own writing and run every T1 candidate on it before switching; that single eval outranks everything in this document.
2. Gemini 2.5 Flash-Lite (cheapest strong model) is deprecated; 3.1 Flash-Lite shuts down 2027-05-07; 3.8 Flash doubles in price 2027-01-01. Abstract the model ID.
3. Qwen/Mistral/DeepSeek image-token rules were not confirmed on official pages this session; their $/1k could be off by 2-3x (still cheap).
4. Over-correction: any VLM may "improve" the student's answer. The verbatim prompt plus disagreement check is the mitigation, not a guarantee.
