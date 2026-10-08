"""WhatsApp bot: forward the dealer's posts to the bot number and it saves them.

You forward a few watches (photos + details) in one go. The bot collects everything
you send, and once you've been quiet for QUIET_SECONDS it groups the messages into
watches, saves them the same way the export importer does, and replies with a summary.

Commands (type them, don't forward): help, undo, count
"""
from __future__ import annotations

import itertools
import os
import queue
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, request

from .extract import ai_available
from .grouping import group_products
from .parse_export import Message
from .pipeline import process_group
from .store import Store
from .whatsapp import WhatsApp, valid_signature

HELP = (
    "Forward the seller's watch posts here (photos and their details). "
    "When you stop forwarding I'll save them and reply with what I got.\n\n"
    "Tips: forward the photos and the text of a watch together, in the order they were posted.\n"
    "Commands: *undo* removes the last batch, *count* shows totals, *help* shows this."
)


def load_env(path: Path) -> None:
    """Read KEY=VALUE lines from .env into os.environ (real env vars win)."""
    if not path.exists():
        return
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass
class Batch:
    messages: list[Message] = field(default_factory=list)
    generation: int = 0          # bumps on every message; a flush only runs if nothing newer arrived
    acknowledged: bool = False


class Bot:
    def __init__(self, wa: WhatsApp, store: Store, allowed: set[str], quiet_seconds: int, vision: int):
        self.wa = wa
        self.store = store
        self.allowed = allowed
        self.quiet = quiet_seconds
        self.vision = vision
        self.use_ai = ai_available()
        self.inbox = store.out / "_inbox"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.batches: dict[str, Batch] = {}
        self.last_added: dict[str, list[str]] = {}
        self.seen_ids: dict[str, None] = {}
        self.seq = itertools.count()
        # One worker handles every job in arrival order, so downloads finish before their flush.
        self.jobs: queue.Queue = queue.Queue()
        threading.Thread(target=self._work, daemon=True).start()

    # ---------------------------------------------------------------- webhook side (fast)
    def receive(self, payload: dict) -> None:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                for msg in change.get("value", {}).get("messages", []):
                    if msg["id"] in self.seen_ids:
                        continue  # Meta retries deliveries; handle each message once
                    self.seen_ids[msg["id"]] = None
                    if len(self.seen_ids) > 5000:
                        self.seen_ids.pop(next(iter(self.seen_ids)))
                    self.jobs.put(("message", msg, next(self.seq)))

    # ---------------------------------------------------------------- worker side
    def _work(self) -> None:
        while True:
            kind, *args = self.jobs.get()
            try:
                if kind == "message":
                    self._handle_message(*args)
                else:
                    self._flush(*args)
            except Exception:
                traceback.print_exc()

    def _reply(self, to: str, text: str) -> None:
        try:
            self.wa.send_text(to, text)
        except Exception as e:
            print(f"  ! could not reply to {to}: {e}")

    def _handle_message(self, msg: dict, seq: int) -> None:
        sender = msg["from"]
        if self.allowed and sender not in self.allowed:
            print(f"  ignoring message from {sender} (not in ALLOWED_NUMBERS)")
            return
        mtype = msg.get("type")
        forwarded = msg.get("context", {}).get("forwarded", False)

        if mtype == "text" and not forwarded:
            command = msg["text"]["body"].strip().lower()
            if command in ("help", "hi", "hello", "start"):
                return self._reply(sender, HELP)
            if command == "undo":
                return self._undo(sender)
            if command in ("count", "status"):
                return self._count(sender)

        ts = datetime.fromtimestamp(int(msg.get("timestamp", 0)) or datetime.now().timestamp())
        m = Message(timestamp=ts, sender=sender, raw_index=seq)
        if mtype == "text":
            m.text = msg["text"]["body"]
        elif mtype in ("image", "video"):
            media = msg[mtype]
            day_dir = self.inbox / f"{datetime.now():%Y%m%d}"
            day_dir.mkdir(exist_ok=True)
            try:
                m.media = self.wa.download_media(media["id"], day_dir)
            except Exception as e:
                print(f"  ! download failed for {media['id']}: {e}")
                m.media_missing = True
            m.text = media.get("caption", "")
        else:
            print(f"  skipping unsupported message type {mtype!r}")
            return

        batch = self.batches.setdefault(sender, Batch())
        batch.messages.append(m)
        batch.generation += 1
        print(f"  + {mtype} from {sender} ({len(batch.messages)} in batch)")
        if not batch.acknowledged:
            batch.acknowledged = True
            self._reply(sender, "📥 Receiving… I'll save everything once you stop forwarding.")
        gen = batch.generation
        threading.Timer(self.quiet, lambda: self.jobs.put(("flush", sender, gen))).start()

    def _flush(self, sender: str, gen: int) -> None:
        batch = self.batches.get(sender)
        if not batch or batch.generation != gen:
            return  # more messages arrived after this timer was set
        del self.batches[sender]
        # Forwarded messages can share a timestamp, so arrival order breaks ties.
        msgs = sorted(batch.messages, key=lambda m: (m.timestamp, m.raw_index))
        groups, layout = group_products(msgs, None, gap_minutes=10**6, layout="auto")
        print(f"  flushing {len(msgs)} messages from {sender}: {len(groups)} watches (text {layout} photos)")

        lines, added = [], []
        for i, g in enumerate(groups, 1):
            o = process_group(self.store, g, self.use_ai, self.vision)
            print(f"    [{i}/{len(groups)}] {o.line()}")
            if o.action == "added":
                added.append(o.pid)
                r = o.record
                price = f"₹{r['price']:,.0f}" if r.get("price") else "no price"
                lines.append(f"✅ {r['title'][:70]}\n    {price} · {o.image_count} photos"
                             + (f"\n    ⚠️ check: {'; '.join(o.needs_review)}" if o.needs_review else ""))
            elif o.action == "updated":
                what = ", ".join(
                    f"price ₹{v:,.0f}" if k == "price" else f"{k} {v}"
                    for k, v in o.changes.items() if k != "price_text")
                lines.append(f"🔄 Already saved, updated {what}: {o.pid}")
            else:
                lines.append(f"↩️ Already saved: {o.pid}")
        self.last_added[sender] = added
        summary = f"Saved {len(added)} new watch{'es' if len(added) != 1 else ''}."
        if not self.use_ai:
            summary += " (basic mode: no AI key set)"
        footer = "\n\nWrong? Reply *undo* to remove this batch." if added else ""
        self._reply(sender, summary + "\n\n" + "\n\n".join(lines) + footer if lines else "Nothing to save in that batch.")

    def _undo(self, sender: str) -> None:
        pids = self.last_added.pop(sender, [])
        removed = [p for p in pids if self.store.remove(p)]
        self._reply(sender, f"🗑️ Removed {len(removed)} watch{'es' if len(removed) != 1 else ''} from the last batch."
                    if removed else "Nothing to undo.")

    def _count(self, sender: str) -> None:
        cat = self.store.catalog
        sold = sum(r.get("status") == "sold" for r in cat)
        review = sum(bool(r.get("needs_review")) for r in cat)
        self._reply(sender, f"📊 {len(cat)} watches saved ({sold} sold, {review} need review).")


def create_app(env_file: Path | None = None) -> Flask:
    load_env(env_file or Path.cwd() / ".env")
    missing = [k for k in ("WA_TOKEN", "WA_PHONE_NUMBER_ID", "WA_VERIFY_TOKEN") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing settings in .env: {', '.join(missing)} (see .env.example)")

    wa = WhatsApp(os.environ["WA_TOKEN"], os.environ["WA_PHONE_NUMBER_ID"], os.environ.get("WA_API_VERSION", "v23.0"))
    allowed = {n.strip().lstrip("+").replace(" ", "") for n in os.environ.get("ALLOWED_NUMBERS", "").split(",") if n.strip()}
    store = Store(Path(os.environ.get("OUT_DIR", "output")))
    bot = Bot(wa, store, allowed, int(os.environ.get("QUIET_SECONDS", "30")), int(os.environ.get("VISION_IMAGES", "0")))
    app_secret = os.environ.get("WA_APP_SECRET", "")
    verify_token = os.environ["WA_VERIFY_TOKEN"]

    if not allowed:
        print("  ! ALLOWED_NUMBERS is empty: anyone who messages the bot can add products.")
    if not app_secret:
        print("  ! WA_APP_SECRET not set: webhook signatures are not being checked.")
    print(f"  AI extraction: {'on' if bot.use_ai else 'off (set ANTHROPIC_API_KEY)'}; saving to {store.out.resolve()}")

    app = Flask(__name__)
    app.config["bot"] = bot

    @app.get("/webhook")
    def verify():
        # Meta calls this once when you save the webhook URL in the app dashboard.
        if request.args.get("hub.mode") == "subscribe" and request.args.get("hub.verify_token") == verify_token:
            return request.args.get("hub.challenge", ""), 200
        abort(403)

    @app.post("/webhook")
    def webhook():
        body = request.get_data()
        if app_secret and not valid_signature(app_secret, body, request.headers.get("X-Hub-Signature-256")):
            abort(401)
        bot.receive(request.get_json(force=True, silent=True) or {})
        return "ok", 200  # answer fast; Meta retries if we're slow

    @app.get("/")
    def health():
        return f"watchbot running: {len(store.catalog)} watches saved", 200

    return app
