"""Split the seller's messages into one candidate group per product.

The seller posts several watches back-to-back, so we work in two steps:
  1. Bursts: consecutive messages from the same sender with no long pause.
  2. Inside a burst, each listing-like text is paired with the photos next to it.
     Sellers are consistent about whether the text comes *after* the photos
     (photos, photos, details) or *before* them (details, photos, photos), so we
     try both layouts across the whole chat and keep the one that leaves the
     fewest photos without details. A caption on a photo counts as the text.
Anything we aren't sure about is flagged `needs_review` rather than guessed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta

from .parse_export import Message

# Short chatter that should not start a new product.
_CHATTER = re.compile(
    r"^(sold|on hold|hold|booked|dm|pm|available|avl|price drop|any one|anyone|"
    r"ok|okay|thanks|thank you|yes|no|done|👍|🙏|.{0,3})[\s.!?]*$",
    re.IGNORECASE,
)


@dataclass
class Group:
    messages: list[Message] = field(default_factory=list)
    text_messages: list[Message] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    needs_review: list[str] = field(default_factory=list)

    @property
    def media(self) -> list[Message]:
        return [m for m in self.messages if m.kind in ("image", "video")]

    @property
    def text(self) -> str:
        return "\n".join(m.text for m in self.text_messages if m.text).strip()

    @property
    def first_seen(self):
        return self.messages[0].timestamp

    @property
    def sender(self) -> str:
        return self.messages[0].sender


def is_listing_text(text: str) -> bool:
    t = text.strip()
    if not t or _CHATTER.match(t):
        return False
    return len(t) >= 15 or bool(re.search(r"\d", t))


def _is_media(m: Message) -> bool:
    name = m.media.name if m.media else ""
    return m.kind in ("image", "video") and not name.upper().startswith("STK-")


def bursts(messages: list[Message], gap_minutes: int) -> list[list[Message]]:
    out: list[list[Message]] = []
    gap = timedelta(minutes=gap_minutes)
    for m in messages:
        if out and out[-1][-1].sender == m.sender and m.timestamp - out[-1][-1].timestamp <= gap:
            out[-1].append(m)
        else:
            out.append([m])
    return out


def _segment(burst: list[Message], layout: str) -> list[Group]:
    groups: list[Group] = []
    pending: list[Message] = []  # photos waiting for their details ("after" layout)
    current: Group | None = None  # open product ("before" layout)

    for m in burst:
        media = _is_media(m)
        listing = is_listing_text(m.text)
        if not media and not listing:
            if m.text.strip():
                target = current or (groups[-1] if groups else None)
                if target:
                    target.notes.append(m.text.strip())
            continue

        # A second text with no photos in between (e.g. "Price 9.5L" sent separately)
        # continues the product we're on rather than starting a new one.
        # A full listing without photos is its own product, not a follow-on.
        if listing and not media and len(m.text.strip()) < 80:
            follow_on = (groups[-1] if layout == "after" and not pending and groups
                         else current if layout == "before" and current and not current.media
                         else None)
            if follow_on is not None:
                follow_on.messages.append(m)
                follow_on.text_messages.append(m)
                continue

        if layout == "after":
            if media:
                pending.append(m)
            if listing:
                g = Group(messages=pending + ([] if media else [m]), text_messages=[m])
                groups.append(g)
                pending = []
        else:  # "before"
            if listing:
                current = Group(messages=[m], text_messages=[m])
                groups.append(current)
            elif media:
                if current is None:
                    current = Group()
                    groups.append(current)
                current.messages.append(m)

    if pending:
        groups.append(Group(messages=pending))
    for g in groups:
        if not g.text_messages:
            g.needs_review.append("photos without any details text")
        elif not g.media:
            g.needs_review.append("details text without photos")
    return groups


def _score(groups: list[Group]) -> int:
    return sum(len(g.media) for g in groups if g.text_messages) - sum(
        len(g.media) for g in groups if not g.text_messages
    )


def group_products(
    messages: list[Message],
    sellers: set[str] | None = None,
    gap_minutes: int = 15,
    layout: str = "auto",
) -> tuple[list[Group], str]:
    """Return (product groups, layout used). Groups with neither photos nor text are dropped."""
    msgs = [m for m in messages if not sellers or m.sender in sellers]
    all_bursts = [b for b in bursts(msgs, gap_minutes) if any(_is_media(m) for m in b)]

    if layout == "auto":
        tries = {lay: [g for b in all_bursts for g in _segment(b, lay)] for lay in ("after", "before")}
        layout = max(tries, key=lambda lay: _score(tries[lay]))
        groups = tries[layout]
    else:
        groups = [g for b in all_bursts for g in _segment(b, layout)]

    for g in groups:
        if len(g.text_messages) == 1 and len(g.media) > 12:
            g.needs_review.append(f"{len(g.media)} photos on one listing; may be two watches")
        if any(m.media_missing for m in g.media):
            g.needs_review.append("some photos missing from the export (export with media)")
    return [g for g in groups if g.media or g.text_messages], layout
