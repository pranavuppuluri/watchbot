"""Brand and category matching that ignores case and forgives typos, and reading your WhatsApp
replies to an upload preview ("yes brand omga category swis 4500").

Typos are judged by edit distance (a missing, extra, wrong or swapped letter counts as one edit).
Short words must match exactly, longer ones may be off by one or two letters, and a word that is
equally close to two choices is not guessed.
"""
from __future__ import annotations

import re

from .extract import parse_price

SWISS_BRANDS = {"omega", "rolex", "tudor", "longines", "rado", "tag heuer", "oris", "certina", "mido",
                "favre leuba", "enicar", "west end", "sandoz", "henri sandoz", "roamer", "eterna", "zenith",
                "breitling", "iwc", "cartier", "jaeger-lecoultre", "hmt swiss", "camy", "titoni", "ernest borel",
                "tissot", "swatch", "hamilton", "edox", "victorinox", "frederique constant", "baume mercier",
                "raymond weil", "ball", "alpina", "bulova accutron", "universal geneve", "west end watch co"}
USSR_BRANDS = {"vostok", "poljot", "raketa", "slava", "pobeda", "luch", "sekonda", "zim", "chaika", "zarya",
               "komandirskie", "amphibia", "molnija", "sturmanskie"}
OTHER_BRANDS = {"seiko", "citizen", "casio", "orient", "hmt", "titan", "timex", "allwyn", "sonata", "fastrack",
                "maxima", "ricoh", "bulova", "fossil", "invicta", "q&q", "jaipur", "favre-leuba"}
# Model lines dealers write instead of the brand.
ALIASES = {"g-shock": "Casio", "g shock": "Casio", "gshock": "Casio", "baby-g": "Casio", "edifice": "Casio",
           "pro trek": "Casio", "mudmaster": "Casio", "rangeman": "Casio", "frogman": "Casio",
           "grand seiko": "Seiko", "seiko 5": "Seiko", "presage": "Seiko", "prospex": "Seiko", "king seiko": "Seiko",
           "eco-drive": "Citizen", "promaster": "Citizen", "seamaster": "Omega", "speedmaster": "Omega",
           "constellation": "Omega", "datejust": "Rolex", "submariner": "Rolex", "oyster": "Rolex",
           "janata": "HMT", "kohinoor": "HMT", "komandirskie": "Vostok", "amphibia": "Vostok"}
# Model codes that only one brand uses, for posts that give just the reference ("GA 100", "SKX007").
MODEL_CODES = [
    (r"\b(ga|gw|gm|gbd|gba|gst|dw|gx|gd|gls|gma|gr|mtg|mrg|ecb|efr|efv|eqb|prw|prg)[\s-]?\d{3,4}", "Casio"),
    (r"\b(f-?91w|f-?105w|a-?168w?|a-?158w?|ae-?1200|ae-?1300|mdv-?106|mrw-?200)\b", "Casio"),
    (r"\b(skx|snk|snzg|srpd|srpe|spb|sbdc|sarb|ssk|snxs|sgf|sne)[\s-]?\d{2,3}", "Seiko"),
    (r"\b(6309|6105|7002|7s26|7009|6139|6119|4r36|nh35)\b", "Seiko"),
    (r"\b(ny\d{4}|bn\d{4}|nj\d{4}|nh\d{4}|bm\d{4}|ca\d{4})\b", "Citizen"),
    (r"\b(fac|fab|faa|ra-aa|ra-ac|fe\d{2})[\s-]?\d{2,4}", "Orient"),
]
MODEL_CODES = [(re.compile(p, re.I), b) for p, b in MODEL_CODES]

# A site category for a group of brands, used when the brand has no category of its own.
GROUPS = {"swiss": SWISS_BRANDS, "ussr": USSR_BRANDS}
UPPER = {"hmt", "iwc", "ussr", "q&q"}
NONE_WORDS = {"none", "unknown", "na", "nil", "nobrand", "unbranded", "generic", "-"}

YES_WORDS = {"yes", "y", "ok", "okay", "upload", "approve", "approved", "haan", "ha", "han", "sure", "yeah", "yep", "go", "done"}
NO_WORDS = {"no", "n", "skip", "reject", "cancel", "nahi", "nope", "dont"}
FIELDS = {"brand": "brand", "make": "brand", "category": "category", "cat": "category", "categories": "category",
          "price": "price", "rate": "price", "rs": "price", "inr": "price", "amount": "price"}


def key(text: str) -> str:
    return re.sub(r"[^a-z0-9&]", "", (text or "").lower())


def display(name: str) -> str:
    return name.upper() if key(name) in UPPER else " ".join(w[:1].upper() + w[1:] for w in name.split())


def edits(a: str, b: str) -> int:
    """Optimal string alignment distance: insert, delete, replace, or swap two neighbours."""
    if abs(len(a) - len(b)) > 3:
        return 99
    d = [[i + j if i * j == 0 else 0 for j in range(len(b) + 1)] for i in range(len(a) + 1)]
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[-1][-1]


def _squash(k: str) -> str:
    return re.sub(r"(.)\1+", r"\1", k)  # "yesss" -> "yes", "omegaa" -> "omega"


def closest(word: str, choices, strict: bool = False):
    """The choice `word` most likely means, or None. choices: names, or {name: value}."""
    table = choices if isinstance(choices, dict) else {c: c for c in choices}
    k = key(word)
    if not k:
        return None
    by_key = {}
    for name, value in table.items():
        by_key.setdefault(key(name), value)
    for cand in (k, _squash(k)):
        if cand in by_key:
            return by_key[cand]
        squashed = {_squash(c): v for c, v in by_key.items()}
        if cand in squashed:
            return squashed[cand]
    allowed = 0 if len(k) <= 3 else 1 if len(k) <= 6 or strict else 2
    if strict and len(k) < 6:
        allowed = 0
    if not allowed:
        return None
    scored = sorted((edits(k, c), c) for c in by_key)
    best = [c for d, c in scored if d == scored[0][0]]
    if scored[0][0] <= allowed and len({by_key[c] for c in best}) == 1:
        return by_key[best[0]]
    return None


ALL_BRANDS = {display(b): display(b) for b in SWISS_BRANDS | USSR_BRANDS | OTHER_BRANDS}


def brand_choices(categories: dict[str, int]) -> dict[str, str]:
    """Every brand we know (and every brand-like category on the site), plus the model-line aliases."""
    out = {**ALL_BRANDS, **{n: n for n in categories if key(n) not in GROUPS and key(n) != "anomalies"}}
    return {**out, **ALIASES}


def normalise_brand(brand: str | None, categories: dict[str, int]) -> str | None:
    """'omga' -> 'Omega', 'g shock' -> 'Casio'. Unknown brands are kept, just tidied."""
    if not brand or not brand.strip():
        return None
    return closest(brand, brand_choices(categories)) or display(brand.strip())


def find_brand(text: str, categories: dict[str, int]) -> str | None:
    """A brand named anywhere in the title or description (strict about typos, since this is free text)."""
    words = re.findall(r"[a-z0-9&-]+", (text or "").lower())
    choices = brand_choices(categories)
    for n in (3, 2, 1):
        for i in range(len(words) - n + 1):
            hit = closest(" ".join(words[i:i + n]), choices, strict=True)
            if hit:
                return hit
    for pattern, brand in MODEL_CODES:
        if pattern.search(text or ""):
            return brand
    return None


def category_for(brand: str | None, categories: dict[str, int]) -> str | None:
    """The site category a brand belongs in: its own category, else its group (Swiss, USSR)."""
    if not brand:
        return None
    own = closest(brand, {n: n for n in categories})
    if own:
        return own
    bk = key(brand)
    for group, members in GROUPS.items():
        if bk in {key(m) for m in members}:
            return next((n for n in categories if key(n) == group), None)
    return None


def _field(word: str) -> str | None:
    """'brand', 'category' or 'price' if the word is that keyword, typos allowed: "brnd", "catagory"."""
    k = key(word)
    if k in FIELDS:
        return FIELDS[k]
    return closest(k, {"brand": "brand", "category": "category", "price": "price"}) if len(k) >= 4 else None


def _decision(word: str) -> str | None:
    """yes / no, forgiving "Yes", "YSE", "yesss", "Noo", "okk"; one typo allowed in words of 3+ letters."""
    k = _squash(key(word))
    for words, answer in ((YES_WORDS, "yes"), (NO_WORDS, "no")):
        if k in words:
            return answer
    if len(k) >= 3:
        near = {answer for words, answer in ((YES_WORDS, "yes"), (NO_WORDS, "no"))
                for w in words if len(w) >= 3 and edits(k, w) <= 1}
        if len(near) == 1:
            return near.pop()
    return None


def parse_reply(text: str, categories: dict[str, int]) -> dict:
    """Read a reply to a preview. Returns {decision: yes|no|None, brand, category, price, unknown: [...]}.

    Accepts any order and spelling: "Yes", "yse 4500", "brand: omga, catagory swiss",
    "upload casio 4.5k", "no". A bare word is tried as a category, then as a brand.
    """
    out = {"decision": None, "brand": None, "category": None, "price": None, "unknown": []}
    body = re.sub(r"#\S+", " ", text or "")
    tokens = re.findall(r"₹?\s?\d[\d,.]*\s?(?:k|l|lakh|lakhs|lac)?\b|[^\s,;:=]+", body, re.I)
    words = [t.strip() for t in tokens if t.strip()]
    if words:
        decision = _decision(words[0])
        if decision:
            out["decision"], words = decision, words[1:]
    cats, brands = {n: n for n in categories}, brand_choices(categories)
    is_num = lambda w: re.match(r"₹?\s?\d", w)
    stop = lambda w: _field(w) or is_num(w)

    def longest(i: int, table: dict):
        """The longest run of up to 3 words from i that names something in table."""
        for n in range(3, 0, -1):
            span = words[i:i + n]
            if len(span) == n and not any(stop(w) for w in span):
                hit = closest(" ".join(span), table)
                if hit:
                    return hit, n
        return None, 1

    field, i = None, 0
    while i < len(words):
        w = words[i]
        if _field(w):
            field, i = _field(w), i + 1
            continue
        if is_num(w):
            p, _ = parse_price(w if re.search(r"(?i)₹|\d\s*(k|l|lakh|lakhs|lac)", w) else f"₹{w}")
            if p:
                out["price"] = int(p)
            else:
                out["unknown"].append(w)
            field, i = None, i + 1
            continue
        if field == "brand" and key(w) in NONE_WORDS:
            out["brand"], field, i = "", None, i + 1
            continue
        if field == "brand":
            hit, n = longest(i, brands)
            if not hit:  # a brand we don't know: keep the words up to the next keyword or known name
                n = 1
                while i + n < len(words) and not stop(words[i + n]) and not longest(i + n, {**cats, **brands})[0]:
                    n += 1
                hit = display(" ".join(words[i:i + n]))
            out["brand"] = hit
        elif field == "category":
            hit, n = longest(i, cats)
            if hit:
                out["category"] = hit
            else:
                out["unknown"].append(w)
        else:
            cat, nc = longest(i, cats)
            brand, nb = longest(i, brands)
            n = max(nc if cat else 0, nb if brand else 0) or 1
            if cat and out["category"] is None:
                out["category"] = cat
                if brand and out["brand"] is None and key(cat) not in GROUPS:
                    out["brand"] = brand
            elif brand and out["brand"] is None:
                out["brand"] = brand
            else:
                out["unknown"].append(" ".join(words[i:i + n]))
        field, i = None, i + n
    return out
