"""Process what the Baileys listener saves, as it arrives.

The listener appends each group message to output/_inbox/messages.jsonl (photos next to it).
This loop reads new lines, holds each seller's messages until they've been quiet for a few
minutes (they post several photos + details in a row), then groups and saves them exactly
like the export importer does.

If the seller *replies* to one of their earlier posts with "sold", "on hold" or a new price,
the matching product is updated.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

from .extract import ai_available, parse_price
from .grouping import group_products
from .parse_export import Message
from .pipeline import process_group
from .store import Store


class Watcher:
    def __init__(self, out_dir: Path, sellers: list[str] | None, quiet_seconds: int, layout: str, vision: int):
        self.store = Store(out_dir)
        self.inbox = self.store.out / "_inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.log = self.inbox / "messages.jsonl"
        self.offset_path = self.inbox / "processed_offset.txt"
        self.map_path = self.inbox / "message_map.json"   # WhatsApp message id -> product id
        self.reposts = self.inbox / "reposts.jsonl"       # watches posted again, for the uploader to ask about
        self.offset = int(self.offset_path.read_text()) if self.offset_path.exists() else 0
        self.msg_map: dict[str, str] = json.loads(self.map_path.read_text("utf-8")) if self.map_path.exists() else {}
        self.sellers = [s.lower().lstrip("+") for s in sellers or []]
        self.quiet = quiet_seconds
        self.layout = layout
        self.vision = vision
        self.use_ai = ai_available()
        self.pending: dict[str, list[Message]] = {}
        self.last_arrival: dict[str, float] = {}
        self.seq = 0

    def _is_seller(self, rec: dict) -> bool:
        if not self.sellers:
            return True
        name, number = rec.get("sender_name", "").lower(), rec.get("sender", "")
        return any(s == number or s in name for s in self.sellers)

    def _read_new(self) -> list[dict]:
        if not self.log.exists():
            return []
        with open(self.log, "rb") as f:
            f.seek(self.offset)
            data = f.read()
        end = data.rfind(b"\n") + 1  # only whole lines; the listener may be mid-write
        self.offset += end
        out = []
        for line in data[:end].decode("utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out

    def _to_message(self, rec: dict) -> Message:
        self.seq += 1
        m = Message(timestamp=datetime.fromtimestamp(rec["ts"]),
                    sender=rec.get("sender_name") or rec.get("sender") or "unknown",
                    text=rec.get("text", ""), raw_index=self.seq,
                    msg_id=rec.get("id", ""), quoted_id=rec.get("quoted_id") or "")
        if rec["type"] in ("image", "video"):
            if rec.get("file") and (self.inbox / rec["file"]).exists():
                m.media = self.inbox / rec["file"]
            else:
                m.media_missing = True
        return m

    def _apply_reply(self, m: Message) -> bool:
        """Seller replied to an earlier post: update that product's status/price."""
        pid = self.msg_map.get(m.quoted_id)
        if not pid or m.media:
            return False
        changes = {}
        if re.search(r"\bsold\b", m.text, re.I):
            changes["status"] = "sold"
        elif re.search(r"\b(on hold|hold|booked)\b", m.text, re.I):
            changes["status"] = "on_hold"
        elif re.search(r"\b(available|avl|back)\b", m.text, re.I):
            changes["status"] = "available"
        price, price_text = parse_price(m.text)
        if price:
            changes.update(price=price, price_text=price_text)
        if changes and self.store.update(pid, changes, m.timestamp):
            self.store.save()
            print(f"  ~ {pid}: " + ", ".join(f"{k} -> {v}" for k, v in changes.items() if k != "price_text"))
            return True
        return False

    def _flush(self, sender: str) -> None:
        msgs = sorted(self.pending.pop(sender), key=lambda m: (m.timestamp, m.raw_index))
        self.last_arrival.pop(sender, None)
        groups, layout = group_products(msgs, None, gap_minutes=10**6, layout=self.layout)
        if not groups:
            return
        print(f"\n{datetime.now():%H:%M} {sender}: {len(msgs)} messages -> {len(groups)} watches (text {layout} photos)")
        for i, g in enumerate(groups, 1):
            o = process_group(self.store, g, self.use_ai, self.vision)
            print(f"  [{i}/{len(groups)}] {o.line()}")
            if o.action in ("duplicate", "updated"):
                with open(self.reposts, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"pid": o.pid, "at": time.time()}) + "\n")
            if o.pid:
                for m in g.messages:
                    if m.msg_id:
                        self.msg_map[m.msg_id] = o.pid
        self.map_path.write_text(json.dumps(self.msg_map), "utf-8")

    def tick(self) -> None:
        for rec in self._read_new():
            if not self._is_seller(rec):
                continue
            m = self._to_message(rec)
            if m.quoted_id and self._apply_reply(m):
                continue
            self.pending.setdefault(m.sender, []).append(m)
            self.last_arrival[m.sender] = time.time()
            what = "photo" if m.kind == "image" else m.kind
            print(f"  + {m.sender}: {what} ({len(self.pending[m.sender])} waiting)")
        # Commit the read position only after messages are in memory; a crash before a flush
        # re-reads them next start, and the duplicate check stops double-saving.
        for sender, t in list(self.last_arrival.items()):
            if time.time() - t >= self.quiet:
                self._flush(sender)
        if not self.pending:
            self.offset_path.write_text(str(self.offset))

    def run(self) -> None:
        print(f"Watching {self.log}")
        print(f"  AI extraction: {'on' if self.use_ai else 'off (set ANTHROPIC_API_KEY)'}; "
              f"saving after {self.quiet}s of quiet; sellers: {', '.join(self.sellers) or 'everyone posting photos'}")
        try:
            while True:
                self.tick()
                time.sleep(3)
        except KeyboardInterrupt:
            for sender in list(self.pending):
                self._flush(sender)
            self.offset_path.write_text(str(self.offset))
            print("\nStopped. Waiting messages were saved.")
