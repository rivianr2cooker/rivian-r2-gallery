"""Two-step tagging with reference photos.

Step 1 (one image, no references): render, view, light, and first guesses for paint, interior, wheels.
Step 2 (the photo plus labeled reference photos for the likely options): final paint, interior, wheels.

References live in references/<group>/<value>/*.jpg (see references/references.json). Step 2 only
sends references for the 2-3 most likely options of each group the photo can show, in a fixed
order, so the cost stays a few images per photo instead of all 42.
"""
import base64
import io
import json
import time

import requests
from PIL import Image

from common import ROOT
from tag import API_URL, MAX_SIDE, MODEL, RENDER_Q, aka, context_text, to_jpeg

REF_SIDE = 512  # px per reference photo
GROUP_VIEWS = {"paint": ("exterior", "detail"), "interior": ("interior",), "wheels": ("exterior", "detail")}
SKIP_VALUES = {"wheels": {"aftermarket"}}  # no references; still a valid answer
LIGHT_Q = {"type": "choice", "name": "light",
           "instructions": "How much light is on the car (or cabin) in this photo?",
           "choices": [
               {"value": "low", "description": "Night, dusk, a dim garage or deep shade: colors look dark and muted."},
               {"value": "mid", "description": "Overcast sky, open shade, or indoors / a showroom: soft, even light."},
               {"value": "high", "description": "Direct sun or bright daylight: strong highlights and shadows."},
           ]}


def load_refs():
    """{group: {value: [jpeg bytes, ...]}} from the references folder, in manifest order."""
    refs = {}
    for m in json.loads((ROOT / "references" / "references.json").read_text(encoding="utf-8")):
        img = Image.open(ROOT / m["file"]).convert("RGB")
        refs.setdefault(m["group"], {}).setdefault(m["value"], []).append(to_jpeg(img, REF_SIDE, 80))
    return refs


def choice_q(name, instructions, opts, fallback):
    return {"type": "choice", "name": name, "instructions": instructions,
            "choices": [{"value": o["value"], "description": f'{o["label"]}{aka(o)}: {o["description"]}'} for o in opts]
                       + [{"value": "unknown", "description": fallback}]}


def group_questions(options):
    return {
        "paint": choice_q("paint", "Which factory paint color is the R2 in the photo? Lighting shifts colors a lot, so "
                                   "compare against the reference photos, which show each color in different light. "
                                   "The post text may name the color and can break ties.",
                          options["paint"], "Exterior paint not visible, wrapped, or can't tell."),
        "interior": choice_q("interior", "Which interior color scheme is visible? Compare with the reference photos.",
                             options["interior"], "Interior not visible or can't tell."),
        "wheels": choice_q("wheels", "Which wheels are on the R2? Look at where the spokes meet the center: on the "
                                     "21\" Liquid Tungsten they run all the way into the hub around the lug nuts; on "
                                     "the 20\" Black Sand All-Terrain they stop short of the center. Also compare tire "
                                     "tread and sidewall height with the reference photos.",
                           options["wheels"], "Wheels not visible or can't tell."),
    }


def step1_questions(options, view_q):
    g = group_questions(options)
    return [RENDER_Q, view_q, LIGHT_Q, g["paint"], g["interior"], g["wheels"]]


class Usage:
    def __init__(self):
        self.calls = self.input = self.cached = self.output = 0

    def add(self, u):
        self.calls += 1
        self.input += u.get("input_tokens", 0)
        self.cached += (u.get("input_tokens_details") or {}).get("cached_tokens", 0)
        self.output += u.get("output_tokens", 0)

    def __str__(self):
        return f"{self.calls} calls, {self.input:,} input tokens ({self.cached:,} cached), {self.output:,} output"


def call(content, questions, key, usage):
    body = {"model": MODEL, "input": [{"role": "user", "content": content}], "questions": questions}
    for attempt in range(5):
        r = requests.post(API_URL, json=body, headers={"Authorization": f"Bearer {key}"}, timeout=180)
        if r.status_code in (429, 500, 502, 503):
            time.sleep(2 ** attempt)
            continue
        if not r.ok:
            raise RuntimeError(f"Decisions API {r.status_code}: {r.text[:300]}")
        j = r.json()
        usage.add(j.get("usage") or {})
        return {a["name"]: a for a in j["answers"]}
    raise RuntimeError("Decisions API kept failing")


def b64(jpeg):
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()


def candidates(answer, values, k=3, floor=0.05):
    """Top options from a step-1 choice answer: always the top 2, a third if it's not negligible."""
    probs = sorted(((p["probability"], p["value"]) for p in answer.get("probabilities", []) if p["value"] in values),
                   reverse=True)
    return [v for n, (p, v) in enumerate(probs[:k]) if n < 2 or p >= floor]


def pick(a):
    if not a or a.get("type") != "choice":
        return {"value": "unknown", "confidence": 0}
    return {"value": a["choice"], "confidence": round(a.get("confidence", 0), 3)}


def tag_two_step(post, img, options, view_q, refs, key, usage):
    """Returns {"render", "view", "light", "tags": {...}, "step1": {...}} for one photo."""
    photo = to_jpeg(img, MAX_SIDE)
    ctx = {"type": "input_text", "text": context_text(post)}
    a1 = call([ctx, {"type": "input_image", "image_url": b64(photo)}], step1_questions(options, view_q), key, usage)
    view = pick(a1.get("view"))["value"]
    r = a1.get("render", {})
    out = {"render": round(r.get("probability", 0), 3), "view": view, "light": pick(a1.get("light"))["value"],
           "tags": {g: pick(a1.get(g)) for g in ("paint", "interior", "wheels")}}
    out["step1"] = {g: dict(out["tags"][g]) for g in out["tags"]}
    if view == "other" or out["render"] >= 0.8:
        return out

    gq = group_questions(options)
    content, questions = [], []
    for g in ("paint", "interior", "wheels"):  # fixed order
        if view not in GROUP_VIEWS[g]:
            continue
        values = [o["value"] for o in options[g] if o["value"] not in SKIP_VALUES.get(g, ())]
        cands = values if len(values) <= 3 else candidates(a1.get(g, {}), values)
        labels = {o["value"]: o["label"] for o in options[g]}
        for v in values:  # keep options order, not probability order
            if v not in cands:
                continue
            for n, ref in enumerate(refs.get(g, {}).get(v, []), 1):
                content += [{"type": "input_text", "text": f"Reference: {labels[v]} ({g}), example {n}"},
                            {"type": "input_image", "image_url": b64(ref)}]
        questions.append(gq[g])
    if not questions:
        return out
    content += [{"type": "input_text", "text": "The photo to classify (the references above are other cars, for "
                                               "comparison only):"},
                ctx, {"type": "input_image", "image_url": b64(photo)}]
    a2 = call(content, questions, key, usage)
    for q in questions:
        out["tags"][q["name"]] = pick(a2.get(q["name"]))
    return out
