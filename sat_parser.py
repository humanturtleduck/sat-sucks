"""pdfplumber
"""
import json, os, re, statistics, sys
import pdfplumber

QID = re.compile(r"^Question ID:\s*(\w+)")
SEC = re.compile(r"^(Question|Answer|Rationale)$")
CORRECT = re.compile(r"^Correct Answer:\s*([A-D])")
CHOICE = re.compile(r"^([A-D])\.\s+(.*)")
STRUCT = re.compile(r"^(Question|Answer|Rationale|Correct Answer:|Question ID:|[A-D]\.\s)")
ENDERS = (".", "?", "!", '"', "”", "’", ":", "_")


def _clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _inside(b, o, pad=1):
    return b[0] >= o[0] - pad and b[1] >= o[1] - pad and b[2] <= o[2] + pad and b[3] <= o[3] + pad


def _merge(boxes, pad=6):
    boxes = [list(b) for b in boxes]
    again = True
    while again:
        again = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if a[0] - pad <= b[2] and b[0] - pad <= a[2] and a[1] - pad <= b[3] and b[1] - pad <= a[3]:
                    boxes[i] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    del boxes[j]
                    again = True
                    break
            if again:
                break
    return boxes


def _grow(f, rot, lines, padx=48, pady=30):
    f, used = list(f), set()
    cand = [(c, True) for c in rot] + [(l, False) for l in lines]
    while True:
        hit, E = False, (f[0] - padx, f[1] - pady, f[2] + padx, f[3] + pady)
        for k, (o, is_rot) in enumerate(cand):
            if k in used or o["x0"] > E[2] or o["x1"] < E[0] or o["top"] > E[3] or o["bottom"] < E[1]:
                continue
            if not is_rot:
                t = _clean(o["text"])
                if not t or len(t) > 45 or t.endswith((".", "?", "!")) or STRUCT.match(t):
                    continue
            used.add(k)
            hit = True
            f = [min(f[0], o["x0"]), min(f[1], o["top"]), max(f[2], o["x1"]), max(f[3], o["bottom"])]
        if not hit:
            return f


def _join(lines):
    s = lines[0]
    for l in lines[1:]:
        s += l if s.endswith(("-", "—", "–")) else " " + l
    return s


def _scan_page(page, n, assets, counter):
    items, boxes = [], []
    tables = page.find_tables()
    for t in tables:
        rows = [[_clean(c) for c in r] for r in t.extract()]
        if not any(c for r in rows for c in r):
            continue  # empty artefact
        if "Assessment" in rows[0] and "Difficulty" in rows[0]:  # metadata strip at top of each question
            boxes.append(t.bbox)
            if len(rows) > 1:
                items.append(("meta", t.bbox[1], {k.lower(): v for k, v in zip(rows[0], rows[1])}))
        elif len(rows) > 1 and len(rows[0]) > 1 and not any(o is not t and _inside(t.bbox, o.bbox) for o in tables) \
                and not any(_inside((o["x0"], o["top"], o["x1"], o["bottom"]), t.bbox, 2) for o in page.curves + page.images):
            boxes.append(t.bbox)
            items.append(("table", t.bbox[1], {"rows": rows}))

    # Figures: every graphic object (images, curves, lines, rects) outside tables is merged into regions,
    # then each region grows to include its axis labels / legend so the chart is cropped whole.
    gobj = [o for o in page.images + page.curves + page.lines + page.rects
            if not any(_inside((o["x0"], o["top"], o["x1"], o["bottom"]), b, 2) for b in boxes)]
    rot = [c for c in page.chars if not c.get("upright", True)]
    upright = page.filter(lambda o: o.get("object_type") != "char" or o.get("upright", True))
    lines = upright.extract_text_lines(return_chars=False)
    for f in _merge([(o["x0"], o["top"], o["x1"], o["bottom"]) for o in gobj], pad=12):
        inside = [o for o in gobj if _inside((o["x0"], o["top"], o["x1"], o["bottom"]), f, 2)]
        if f[2] - f[0] < 20 or f[3] - f[1] < 20 or (
                len(inside) < 5 and not any(o["object_type"] in ("image", "curve") for o in inside)):
            continue  # a lone rule or border, not a figure
        f = _grow(f, rot, lines)
        counter[0] += 1
        name = f"p{n}_fig{counter[0]}.png"
        os.makedirs(assets, exist_ok=True)
        box = (max(f[0] - 4, 0), max(f[1] - 4, 0), min(f[2] + 4, page.width), min(f[3] + 4, page.height))
        page.crop(box).to_image(resolution=170).save(os.path.join(assets, name))
        boxes.append(box)
        items.append(("image", f[1], {"src": name}))

    def keep(o):  # drop characters that live inside a table or figure; they are handled above
        return o.get("object_type") != "char" or not any(
            _inside((o["x0"], o["top"], o["x1"], o["bottom"]), b, 2) for b in boxes)

    for ln in page.filter(keep).extract_text_lines(return_chars=False):
        d = {k: ln[k] for k in ("x0", "x1", "top", "bottom")}
        d["text"] = _clean(ln["text"])
        if d["text"]:
            items.append(("line", ln["top"], d))
    items.sort(key=lambda i: i[1])
    return items


def _blocks(items, geo):
    
    right, sep = geo
    out, para, prev = [], [], None

    def flush():
        if para:
            out.append({"type": "text", "text": _join([l["text"] for l in para])})
            para.clear()

    for kind, d in items:
        if kind == "line":
            if prev and para and (d["top"] - prev["top"] > sep
                                  or (prev["x1"] < 0.8 * right and prev["text"].endswith(ENDERS))):
                flush()
            para.append(d)
            prev = d
            continue
        flush()
        prev = None
        if kind == "table":
            last = out[-1]["text"] if out and out[-1]["type"] == "text" else ""
            cap = out.pop()["text"] if last and not last.endswith(ENDERS) and len(last) < 120 else ""
            out.append({"type": "table", "caption": cap, "header": d["rows"][0], "rows": d["rows"][1:]})
        elif kind == "image":
            out.append({"type": "image", "src": d["src"]})
    flush()
    return out


def _build(q, geo):
    sec, bk, correct = None, {"Question": [], "Answer": [], "Rationale": []}, None
    for kind, d in q["items"]:
        t = d["text"] if kind == "line" else None
        if t and SEC.match(t):
            sec = t
            continue
        if t and (m := CORRECT.match(t)):
            correct, sec = m[1], None
            continue
        if sec:
            bk[sec].append((kind, d))

    stem = _blocks(bk["Question"], geo)
    prompt = stem.pop()["text"] if len(stem) > 1 and stem[-1]["type"] == "text" else ""

    raw = []
    for kind, d in bk["Answer"]:
        if kind == "line":
            m = CHOICE.match(d["text"])
            if m and m[1] == chr(65 + len(raw)):  # must be next letter in sequence
                raw.append({"label": m[1], "lines": [m[2]]})
            elif raw:
                raw[-1]["lines"].append(d["text"])
        elif kind == "image" and raw:
            raw[-1]["image"] = d["src"]
    choices = [{"label": c["label"], "text": _join(c["lines"]), **({"image": c["image"]} if "image" in c else {})}
               for c in raw]

    rat = "\n\n".join(b["text"] for b in _blocks(bk["Rationale"], geo) if b["type"] == "text")
    parts = re.split(r"(?=Choice [A-D] is (?:the best answer|correct|incorrect))", rat)
    expl = {p[7]: p.strip() for p in parts if p.startswith("Choice ")}

    labels = [c["label"] for c in choices]
    warn = []
    if labels != list("ABCD"):
        warn.append(f"found {len(labels)} answer choices")
    if correct not in labels:
        warn.append("answer key missing or not among the choices")
    if not prompt:
        warn.append("no question prompt detected")
    meta = {k: q["meta"].get(k, "") for k in ("assessment", "test", "domain", "skill", "difficulty")}
    return {"id": q["id"], **meta, "stem": stem, "prompt": prompt, "choices": choices,
            "correct": correct, "rationale": rat, "explanations": expl, "warnings": warn}


def parse_pdf(path, assets_dir):
    items, counter = [], [0]
    with pdfplumber.open(path) as pdf:
        for n, page in enumerate(pdf.pages, 1):
            items += _scan_page(page, n, assets_dir, counter)
    right = max((d["x1"] for k, _, d in items if k == "line"), default=0)
    tops = [d["top"] for k, _, d in items if k == "line"]
    steps = [b - a for a, b in zip(tops, tops[1:]) if 8 < b - a < 20]
    geo = (right, 1.3 * (statistics.median(steps) if steps else 14))  # right edge, paragraph-break pitch

    qs, cur = [], None
    for kind, _, d in items:  # a new "Question ID:" line starts a question; content may span pages
        if kind == "line" and (m := QID.match(d["text"])):
            cur = {"id": m[1], "meta": {}, "items": []}
            qs.append(cur)
        elif cur is None:
            continue
        elif kind == "meta":
            cur["meta"] = d
        else:
            cur["items"].append((kind, d))
    questions = [_build(q, geo) for q in qs]
    return {"source": os.path.basename(path), "count": len(questions), "questions": questions}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    out = parse_pdf(sys.argv[1], os.path.join(os.path.dirname(os.path.abspath(sys.argv[2])), "assets"))
    with open(sys.argv[2], "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    for q in out["questions"]:
        print(q["id"], q["correct"], len(q["choices"]), "choices", q["warnings"] or "ok")
