r"""Local handwritten-math OCR trial: Pix2Text (page) + pix2tex (crops).

Run with the isolated venv:
    D:\code\local\projects\ainl-ocr-venv\Scripts\python.exe scripts\ocr_trial.py

Writes preprocessed images to photosamples\_prep\ and a JSON dump of all
results to photosamples\_prep\ocr_trial_results.json.
"""
import json
import os
import sys
import time
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps, ImageStat

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "photosamples"
PREP = SAMPLES / "_prep"
PREP.mkdir(exist_ok=True)
RESIZED_SHAPE = int(os.environ.get("OCR_RESIZED_SHAPE", "1024"))
PIX2TEX_PAGES = 3
PIX2TEX_TOPK = 3


def preprocess(src: Path, dst: Path) -> dict:
    img = Image.open(src).convert("L")
    mean = ImageStat.Stat(img).mean[0]
    inverted = mean < 128
    if inverted:
        img = ImageOps.invert(img)
    img = img.filter(ImageFilter.GaussianBlur(radius=0.7))
    img = ImageOps.autocontrast(img, cutoff=1)
    img = img.convert("RGB")
    img.save(dst)
    return {"mean_luminance": round(mean, 1), "inverted": inverted, "size": img.size}


def collect_formulas(page) -> list:
    """Every formula region (isolated or embedded) with LaTeX + absolute bbox."""
    out = []
    for el in page.elements:
        tname = getattr(el.type, "name", str(el.type))
        ex, ey = int(el.box[0]), int(el.box[1])
        if isinstance(el.meta, list):
            for d in el.meta:
                if not isinstance(d, dict) or d.get("type") not in ("isolated", "embedding"):
                    continue
                pos = d["position"]
                try:
                    xs = [int(p[0]) for p in pos]
                    ys = [int(p[1]) for p in pos]
                    box = [ex + min(xs), ey + min(ys), ex + max(xs), ey + max(ys)]
                except TypeError:
                    box = [ex + int(pos[0]), ey + int(pos[1]), ex + int(pos[2]), ey + int(pos[3])]
                out.append({"kind": d["type"], "latex": d.get("text", ""), "bbox": box,
                            "score": round(float(d.get("score", 0)), 3), "element": el.id})
        elif tname == "FORMULA":
            out.append({"kind": "isolated", "latex": el.text or "", "bbox": [int(v) for v in el.box],
                        "score": round(float(el.score), 3), "element": el.id})
    return out


def main():
    import torch
    from pix2text import Pix2Text
    device = "cuda" if torch.cuda.is_available() else "cpu"
    gpu = torch.cuda.get_device_name(0) if device == "cuda" else "cpu"
    print(f"device={device} ({gpu}) resized_shape={RESIZED_SHAPE}", flush=True)

    t0 = time.perf_counter()
    try:
        if device == "cuda":
            import onnxruntime as ort
            try:
                ort.preload_dlls()  # pick up CUDA/cuDNN DLLs shipped with torch
            except Exception:  # noqa: BLE001
                pass
            if "CUDAExecutionProvider" not in ort.get_available_providers():
                raise RuntimeError("onnxruntime has no CUDAExecutionProvider")
        p2t = Pix2Text.from_config(device=device)
    except Exception as e:  # noqa: BLE001
        print(f"Pix2Text on {device} failed ({e}); falling back to cpu", flush=True)
        device, gpu = "cpu", "cpu (fallback)"
        p2t = Pix2Text.from_config(device="cpu")
    init_p2t = time.perf_counter() - t0
    print(f"Pix2Text init {init_p2t:.1f}s", flush=True)

    latex_ocr = None
    init_p2tex = None

    results = {"device": device, "gpu": gpu, "resized_shape": RESIZED_SHAPE,
               "init_pix2text_s": round(init_p2t, 1), "pages": []}
    files = sorted(SAMPLES.glob("*.png"))
    for i, src in enumerate(files):
        tag = src.stem.split("_")[-2] if "_" in src.stem else src.stem  # e.g. 196
        dst = PREP / f"p{tag}.png"
        prep_info = preprocess(src, dst)
        rec = {"file": src.name, "prep": str(dst.relative_to(ROOT)), **prep_info}

        t1 = time.perf_counter()
        page = p2t.recognize_page(dst, resized_shape=RESIZED_SHAPE)
        rec["pix2text_s"] = round(time.perf_counter() - t1, 1)
        rec["n_elements"] = len(page.elements)
        rec["layout_types"] = [getattr(el.type, "name", str(el.type)) for el in page.elements]
        rec["page_formulas"] = collect_formulas(page)
        md = page.to_markdown(PREP / f"md_p{tag}", markdown_fn="output.md")
        rec["markdown"] = md

        # Layout-free pass: MFD (formula detection) + MFR + text OCR over the whole page.
        t1 = time.perf_counter()
        regions = p2t.recognize_text_formula(dst, return_text=False, resized_shape=RESIZED_SHAPE)
        rec["text_formula_s"] = round(time.perf_counter() - t1, 1)
        fl, tl = [], []
        for d in regions:
            pos = d["position"]
            xs = [int(p[0]) for p in pos]
            ys = [int(p[1]) for p in pos]
            item = {"kind": d["type"], "latex": d.get("text", ""),
                    "bbox": [min(xs), min(ys), max(xs), max(ys)], "score": round(float(d.get("score", 0)), 3)}
            (fl if d["type"] in ("isolated", "embedding") else tl).append(item)
        rec["formulas"] = fl
        rec["text_lines"] = tl
        print(f"[{src.name}] page {rec['pix2text_s']}s layout={rec['layout_types']} page_formulas={len(rec['page_formulas'])} | text_formula {rec['text_formula_s']}s formulas={len(fl)} text={len(tl)}", flush=True)

        if i < PIX2TEX_PAGES:
            if latex_ocr is None:
                from pix2tex.cli import LatexOCR
                t2 = time.perf_counter()
                latex_ocr = LatexOCR()
                init_p2tex = round(time.perf_counter() - t2, 1)
                results["init_pix2tex_s"] = init_p2tex
            full = Image.open(dst)
            top = sorted(rec["formulas"], key=lambda f: (f["bbox"][2] - f["bbox"][0]) * (f["bbox"][3] - f["bbox"][1]), reverse=True)[:PIX2TEX_TOPK]
            crops = []
            for k, f in enumerate(top):
                x0, y0, x1, y1 = f["bbox"]
                pad = 8
                crop = full.crop((max(0, x0 - pad), max(0, y0 - pad), min(full.width, x1 + pad), min(full.height, y1 + pad)))
                cpath = PREP / f"crop_p{tag}_{k}.png"
                crop.save(cpath)
                t3 = time.perf_counter()
                try:
                    out = latex_ocr(crop)
                except Exception as e:  # noqa: BLE001
                    out = f"<error: {e}>"
                dt = round(time.perf_counter() - t3, 2)
                # also let pix2text's MFR see the same crop for a like-for-like comparison
                t4 = time.perf_counter()
                try:
                    mfr = p2t.recognize_formula(crop)
                except Exception as e:  # noqa: BLE001
                    mfr = f"<error: {e}>"
                dt4 = round(time.perf_counter() - t4, 2)
                crops.append({"crop": cpath.name, "bbox": f["bbox"], "pix2text_page_latex": f["latex"],
                              "pix2text_crop_latex": mfr, "pix2text_crop_s": dt4,
                              "pix2tex_latex": out, "pix2tex_s": dt})
                print(f"   crop{k} pix2tex {dt}s: {out}", flush=True)
            rec["crops"] = crops
        results["pages"].append(rec)

    from importlib.metadata import version as V
    results["versions"] = {"python": sys.version.split()[0], "torch": torch.__version__}
    for name in ("pix2text", "pix2tex", "onnxruntime-gpu", "cnstd", "cnocr", "doclayout-yolo", "transformers"):
        try:
            results["versions"][name] = V(name)
        except Exception:  # noqa: BLE001
            results["versions"][name] = "n/a"
    (PREP / "ocr_trial_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print("done ->", PREP / "ocr_trial_results.json")


if __name__ == "__main__":
    main()
