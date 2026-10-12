"""Turn a seller's free-text listing into structured watch fields.

Uses Claude when ANTHROPIC_API_KEY (or an `ant auth login` profile) is available,
and falls back to simple regex rules otherwise so the pipeline always runs.
"""
from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

MODEL = "claude-opus-5-5"


class WatchListing(BaseModel):
    brand: Optional[str] = Field(None, description="Manufacturer, properly capitalised, e.g. 'Rolex', 'Omega', 'Audemars Piguet'")
    model: Optional[str] = Field(None, description="Model line, expanded from slang, e.g. 'Datejust 36', 'Submariner Date', 'Speedmaster Professional'")
    reference: Optional[str] = Field(None, description="Reference number exactly as written, e.g. '126300', '311.30.42.30.01.005'")
    nickname: Optional[str] = Field(None, description="Collector nickname if used, e.g. 'Hulk', 'Pepsi', 'Wimbledon'")
    year: Optional[str] = Field(None, description="Year or card date, e.g. '2019' or '03/2021'")
    case_size_mm: Optional[str] = Field(None, description="Case diameter in mm, digits only, e.g. '41'")
    case_material: Optional[str] = None
    dial: Optional[str] = Field(None, description="Dial colour/type")
    bracelet_strap: Optional[str] = None
    movement: Optional[Literal["automatic", "manual", "quartz"]] = None
    condition: Optional[str] = Field(None, description="Condition as described, e.g. 'mint', 'unpolished, light scratches'")
    box: Optional[bool] = Field(None, description="True if the original box is included, false if stated absent, null if not mentioned")
    papers: Optional[bool] = Field(None, description="True if warranty card / papers included")
    accessories: Optional[str] = Field(None, description="Other inclusions: extra links, tags, service papers")
    price: Optional[float] = Field(None, description="Asking price as a plain number in rupees (6.5L -> 650000, 1.2Cr -> 12000000). Null if not given or 'DM for price'")
    currency: str = "INR"
    price_text: Optional[str] = Field(None, description="The price exactly as the seller wrote it")
    status: Literal["available", "sold", "on_hold", "unknown"] = "unknown"
    title: str = Field(description="Clean shop title, e.g. 'Rolex Datejust 36 126200 Blue Dial (2019, Full Set)'")
    description: str = Field(description="Tidy 2-5 sentence product description using only facts from the listing. No phone numbers, names or 'DM me'.")
    multiple_watches: bool = Field(False, description="True if the text clearly describes more than one watch")
    confidence: Literal["high", "medium", "low"] = "medium"


SYSTEM = """You extract pre-owned watch listings posted by a dealer in an Indian WhatsApp group.
Listings are informal: abbreviations (DJ = Datejust, Sub = Submariner, GMT, AP = Audemars Piguet,
PP = Patek Philippe, FS / full set = box and papers, NOS = new old stock), Indian price units
(L / lakh / lac = 100,000; Cr = 10,000,000; k = 1,000) and emojis.
Only use facts that are in the text or clearly visible in the photos. Leave a field null rather
than guessing. Never put the seller's name, phone number or contact instructions in title or description."""


def _client():
    try:
        import anthropic
        client = anthropic.Anthropic()
        return client, anthropic
    except Exception:
        return None, None


def ai_available() -> bool:
    client, _ = _client()
    if client is None:
        return False
    try:
        return bool(client.api_key or client.auth_token)
    except AttributeError:
        return True


def _image_block(path: Path) -> dict | None:
    media_type = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower())
    if not media_type or path.stat().st_size > 4_500_000:
        return None
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def extract_with_ai(text: str, notes: list[str], images: list[Path], max_images: int = 0) -> WatchListing:
    client, anthropic = _client()
    content: list[dict] = []
    for p in images[:max_images]:
        block = _image_block(p)
        if block:
            content.append(block)
    prompt = f"Listing text:\n<listing>\n{text or '(no text, photos only)'}\n</listing>"
    if notes:
        prompt += "\n\nFollow-up messages from the seller about this item:\n" + "\n".join(f"- {n}" for n in notes)
    content.append({"type": "text", "text": prompt})

    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM,
        output_config={"effort": "low"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": content}],
        output_format=WatchListing,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        raise RuntimeError(f"model returned no listing (stop_reason={response.stop_reason})")
    return response.parsed_output


# ---------------------------------------------------------------- regex fallback

_BRANDS = {
    "rolex": "Rolex", "omega": "Omega", "tudor": "Tudor", "cartier": "Cartier", "patek": "Patek Philippe",
    "pp ": "Patek Philippe", "audemars": "Audemars Piguet", "ap ": "Audemars Piguet", "iwc": "IWC",
    "panerai": "Panerai", "breitling": "Breitling", "tag": "TAG Heuer", "hublot": "Hublot",
    "jaeger": "Jaeger-LeCoultre", "jlc": "Jaeger-LeCoultre", "longines": "Longines", "seiko": "Seiko",
    "grand seiko": "Grand Seiko", "zenith": "Zenith", "vacheron": "Vacheron Constantin", "tissot": "Tissot",
    "oris": "Oris", "rado": "Rado", "chopard": "Chopard", "bvlgari": "Bvlgari", "bulgari": "Bvlgari",
    "casio": "Casio", "g-shock": "Casio", "citizen": "Citizen", "orient": "Orient", "hmt": "HMT",
    "titan": "Titan", "timex": "Timex", "allwyn": "Allwyn", "fastrack": "Fastrack", "ricoh": "Ricoh",
    "favre leuba": "Favre Leuba", "west end": "West End", "enicar": "Enicar", "certina": "Certina",
    "mido": "Mido", "roamer": "Roamer", "henri sandoz": "Henri Sandoz", "sandoz": "Sandoz",
    "camy": "Camy", "poljot": "Poljot", "vostok": "Vostok", "raketa": "Raketa", "slava": "Slava",
}
_PRICE = re.compile(
    r"(?:₹|rs\.?|inr|price|@)?\s*(?P<num>\d+(?:[.,]\d+)*)\s*(?P<unit>cr|crore|l|lac|lacs|lakh|lakhs|k)?\b",
    re.IGNORECASE,
)
_UNITS = {"cr": 1e7, "crore": 1e7, "l": 1e5, "lac": 1e5, "lacs": 1e5, "lakh": 1e5, "lakhs": 1e5, "k": 1e3}
_PHONE = re.compile(r"(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}")


def parse_price(text: str) -> tuple[float | None, str | None]:
    best = None
    for m in _PRICE.finditer(text):
        raw, unit = m.group("num"), (m.group("unit") or "").lower()
        prefixed = bool(re.match(r"\s*(₹|rs|inr|price|@)", m.group(0), re.IGNORECASE))
        if not unit and not prefixed:
            continue  # plain numbers are usually refs, sizes or years
        value = float(raw.replace(",", "")) * _UNITS.get(unit, 1)
        if 500 <= value <= 5e8:  # vintage pieces sell from a few hundred rupees
            best = (value, m.group(0).strip())
    if best is None:
        # dealers often put the price alone on the last line: "...All original\n3650"
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        m = re.fullmatch(r"(\d{1,3}(?:,\d{3})+|\d{3,9})(?:\s*/-)?", lines[-1]) if len(lines) > 1 else None
        if m and not re.fullmatch(r"19[5-9]\d|20[0-3]\d", m.group(1)) and 500 <= float(m.group(1).replace(",", "")) <= 5e8:
            best = (float(m.group(1).replace(",", "")), lines[-1])
    return best if best else (None, None)


def extract_with_rules(text: str) -> WatchListing:
    low = f" {text.lower()} "
    # whole words only, so "vintage" isn't TAG Heuer
    brand = next((v for k, v in sorted(_BRANDS.items(), key=lambda kv: -len(kv[0]))
                  if re.search(rf"\b{re.escape(k.strip())}\b", low)), None)
    price, price_text = parse_price(text)
    no_price = text.replace(price_text, " ") if price_text else text  # "₹7150" isn't a reference
    # a reference is written as "ref 1234" or next to the name on the title line ("Seiko 6309")
    title_line = next((ln for ln in no_price.splitlines() if ln.strip()), "")
    ref = (re.search(r"\bref\.?\s*(\d{4,6}[A-Z]{0,4}(?:[-/.]\d+)*)\b", no_price, re.IGNORECASE)
           or re.search(r"\b(\d{4,6}[A-Z]{0,4}(?:[-/.]\d+)*)\b", title_line))
    year = re.search(r"\b(19[5-9]\d|20[0-3]\d)\b", text)
    size = re.search(r"\b(\d{2}(?:\.\d)?)\s?mm\b", text, re.IGNORECASE)
    full_set = bool(re.search(r"full\s*set|\bfs\b|box\s*(?:&|and|\+)\s*papers", low))
    status = "sold" if re.search(r"\bsold\b", low) else "on_hold" if "on hold" in low else "unknown"
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "Watch")
    clean = _PHONE.sub("", text).strip()
    return WatchListing(
        brand=brand,
        reference=ref.group(1) if ref and (not year or ref.group(1) != year.group(1)) else None,
        year=year.group(1) if year else None,
        case_size_mm=size.group(1) if size else None,
        box=True if full_set else None,
        papers=True if full_set else None,
        price=price,
        price_text=price_text,
        status=status,
        title=_PHONE.sub("", first_line)[:120].strip() or "Watch",
        description=clean,
        confidence="low",
    )


def extract(text: str, notes: list[str], images: list[Path], use_ai: bool, vision_images: int = 0) -> tuple[WatchListing, str]:
    """Return (listing, method) where method is 'ai' or 'rules'."""
    if use_ai:
        try:
            return extract_with_ai(text, notes, images, vision_images), "ai"
        except Exception as e:  # network, auth, refusal: keep going with the rules
            print(f"    ! AI extraction failed ({type(e).__name__}: {e}); using rules")
    return extract_with_rules("\n".join([text, *notes])), "rules"
