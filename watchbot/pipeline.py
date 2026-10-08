"""Save one product group: shared by the export importer and the WhatsApp bot."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .extract import extract, parse_price
from .grouping import Group
from .store import Store


@dataclass
class Outcome:
    action: str                      # "added" | "updated" | "duplicate" | "preview"
    pid: str | None = None
    record: dict | None = None
    changes: dict = field(default_factory=dict)
    label: str = ""
    image_count: int = 0
    needs_review: list[str] = field(default_factory=list)

    def line(self) -> str:
        if self.action == "added":
            r = self.record
            price = f"₹{r['price']:,.0f}" if r.get("price") else "no price"
            return f"{r['title'][:60]} | {self.image_count} photos | {price}" + (" | REVIEW" if self.needs_review else "")
        if self.action == "updated":
            desc = ", ".join(f"{k} -> {v}" for k, v in self.changes.items() if k != "price_text")
            return f"already have {self.pid}; updated {desc}"
        if self.action == "duplicate":
            return f"already have {self.pid}; skipped"
        flag = f"  REVIEW: {'; '.join(self.needs_review)}" if self.needs_review else ""
        return f"{self.image_count} photos | {self.label}{flag}"


def repost_changes(store: Store, pid: str, g: Group) -> dict:
    """Price or status changes when the seller re-posts a watch we already have."""
    rec = next((r for r in store.catalog if r["id"] == pid), {})
    last_seen = max([rec.get("posted_at", "")] + [h["at"] for h in rec.get("history", [])])
    if g.first_seen.isoformat() <= last_seen:
        return {}  # same or older post (e.g. re-importing an overlapping export)
    text = "\n".join([g.text, *g.notes])
    changes = {}
    price, price_text = parse_price(text)
    if price and price != rec.get("price"):
        changes.update(price=price, price_text=price_text)
    status = ("sold" if re.search(r"\bsold\b", text, re.I)
              else "on_hold" if re.search(r"\b(on hold|booked)\b", text, re.I) else None)
    if status and status != rec.get("status"):
        changes["status"] = status
    return changes


def process_group(store: Store, g: Group, use_ai: bool, vision: int = 0, dry_run: bool = False) -> Outcome:
    images = [m.media for m in g.media if m.media]
    label = (g.text.splitlines() or ["(photos only)"])[0][:60]
    dup = store.find_duplicate(g.text, images)
    if dup:
        changes = repost_changes(store, dup, g)
        if changes and not dry_run:
            store.update(dup, changes, g.first_seen)
            store.save()
        return Outcome("updated" if changes else "duplicate", pid=dup, changes=changes, label=label,
                       image_count=len(images))
    if dry_run:
        return Outcome("preview", label=label, image_count=len(images), needs_review=list(g.needs_review))

    listing, method = extract(g.text, g.notes, images, use_ai, vision)
    needs_review = list(g.needs_review)
    if listing.multiple_watches:
        needs_review.append("text seems to describe more than one watch")
    if listing.confidence == "low" and method == "ai":
        needs_review.append("low-confidence extraction")
    rec = store.add(listing.model_dump(), g.text, g.notes, images, g.first_seen, g.sender, needs_review, method)
    store.save()  # save as we go so a crash doesn't lose work
    return Outcome("added", pid=rec["id"], record=rec, label=label, image_count=len(images), needs_review=needs_review)
