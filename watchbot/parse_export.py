"""Parse a WhatsApp "Export chat" (Android or iPhone) into a flat list of messages.

Accepts either the exported .zip or a folder it was unzipped into. Handles:
  Android:  12/03/2026, 10:15 pm - Ravi Watches: IMG-20260312-WA0004.jpg (file attached)
  iPhone:   [12/03/26, 10:15:30 PM] Ravi Watches: <attached: 00000012-PHOTO-2026-03-12-10-15-30.jpg>
Lines that don't start with a timestamp are continuations of the previous message
(multi-line listings, or the caption under an attachment).
"""
from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# WhatsApp sprinkles invisible direction marks and narrow no-break spaces into exports.
_INVISIBLE = dict.fromkeys(map(ord, "‎‏‪‬﻿"), None)

_ANDROID = re.compile(
    r"^(?P<date>\d{1,2}/\d{1,2}/\d{2,4}),?\s+(?P<time>\d{1,2}[:.]\d{2}(?:[:.]\d{2})?)\s*(?P<ampm>[ap]\.?\s?m\.?)?\s+-\s+(?P<rest>.*)$",
    re.IGNORECASE,
)
_IOS = re.compile(
    r"^\[(?P<date>\d{1,2}/\d{1,2}/\d{2,4}),?\s+(?P<time>\d{1,2}[:.]\d{2}(?:[:.]\d{2})?)\s*(?P<ampm>[ap]\.?\s?m\.?)?\]\s+(?P<rest>.*)$",
    re.IGNORECASE,
)
_ATTACH_ANDROID = re.compile(r"^(?P<file>\S+\.\w{2,5}) \(file attached\)\s*(?P<caption>.*)$", re.DOTALL)
_ATTACH_IOS = re.compile(r"^<attached: (?P<file>[^>]+)>\s*(?P<caption>.*)$", re.DOTALL)
_MEDIA_OMITTED = re.compile(r"^<?(media omitted|image omitted|video omitted)>?$", re.IGNORECASE)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
VIDEO_EXT = {".mp4", ".mov", ".3gp"}


@dataclass
class Message:
    timestamp: datetime
    sender: str
    text: str = ""                 # message body, or the caption for an attachment
    media: Path | None = None      # resolved path to the attached file, if any
    media_missing: bool = False    # export was made "without media" or file not found
    raw_index: int = 0             # position in the chat, for stable ordering
    msg_id: str = ""               # WhatsApp message id (live listener only)
    quoted_id: str = ""            # id of the message this one replies to (live listener only)

    @property
    def kind(self) -> str:
        if self.media is not None or self.media_missing:
            ext = (self.media.suffix.lower() if self.media else "")
            if ext in VIDEO_EXT:
                return "video"
            if ext in IMAGE_EXT or self.media_missing:
                return "image"
            return "file"
        return "text"


@dataclass
class ChatExport:
    root: Path
    messages: list[Message] = field(default_factory=list)
    senders: dict[str, int] = field(default_factory=dict)
    missing_media: int = 0


def _open_export(source: Path, workdir: Path) -> tuple[Path, Path]:
    """Return (chat_txt, media_dir). Unzips into workdir if given a zip."""
    if source.is_file() and source.suffix.lower() == ".zip":
        target = workdir / source.stem
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(source) as zf:
            zf.extractall(target)
        source = target
    if source.is_file():
        return source, source.parent
    txts = sorted(source.rglob("*.txt"), key=lambda p: p.stat().st_size, reverse=True)
    if not txts:
        raise FileNotFoundError(f"No chat .txt found in {source}")
    return txts[0], txts[0].parent


def _detect_day_first(date_strings: list[str]) -> bool:
    """WhatsApp uses the phone's locale. India is DD/MM, but check the data."""
    for d in date_strings:
        a, b, _ = d.split("/")
        if int(a) > 12:
            return True
        if int(b) > 12:
            return False
    return True


def _parse_dt(date: str, time: str, ampm: str | None, day_first: bool) -> datetime:
    a, b, y = date.split("/")
    day, month = (a, b) if day_first else (b, a)
    year = int(y) + (2000 if len(y) == 2 else 0)
    parts = [int(p) for p in re.split(r"[:.]", time)]
    hour, minute, second = parts[0], parts[1], parts[2] if len(parts) > 2 else 0
    if ampm:
        pm = ampm.lower().startswith("p")
        hour = hour % 12 + (12 if pm else 0)
    return datetime(year, int(month), int(day), hour, minute, second)


def parse_export(source: Path, workdir: Path) -> ChatExport:
    chat_txt, media_dir = _open_export(Path(source), Path(workdir))
    lines = chat_txt.read_text(encoding="utf-8", errors="replace").splitlines()
    lines = [ln.translate(_INVISIBLE).replace(" ", " ").replace(" ", " ") for ln in lines]

    # First pass: split into (header_match, body_lines) records.
    records: list[tuple[re.Match, list[str]]] = []
    for ln in lines:
        m = _IOS.match(ln) or _ANDROID.match(ln)
        if m:
            records.append((m, [m.group("rest")]))
        elif records:
            records[-1][1].append(ln)

    day_first = _detect_day_first([m.group("date") for m, _ in records])
    media_index = {p.name: p for p in media_dir.rglob("*") if p.is_file()}

    export = ChatExport(root=media_dir)
    for i, (m, body) in enumerate(records):
        rest = "\n".join(body)
        if ": " not in rest.split("\n", 1)[0]:
            continue  # system line: "X joined", "Messages are end-to-end encrypted", ...
        sender, content = rest.split(": ", 1)
        content = content.strip()
        msg = Message(
            timestamp=_parse_dt(m.group("date"), m.group("time"), m.group("ampm"), day_first),
            sender=sender.strip(),
            raw_index=i,
        )
        att = _ATTACH_ANDROID.match(content) or _ATTACH_IOS.match(content)
        if att:
            fname = att.group("file").strip()
            msg.media = media_index.get(fname)
            msg.media_missing = msg.media is None
            msg.text = att.group("caption").strip()
        elif _MEDIA_OMITTED.match(content):
            msg.media_missing = True
        else:
            msg.text = content
        if msg.media_missing:
            export.missing_media += 1
        export.messages.append(msg)
        export.senders[msg.sender] = export.senders.get(msg.sender, 0) + 1
    return export
