"""Upload saved watches to WooCommerce after a "yes" on WhatsApp.

For every product in the catalog that isn't on the website yet:
  1. the photos are squared and resized into products/<id>/web/ (same size, EXIF and GPS stripped)
  2. a preview (cover photo + title, price, details) is queued in _outbox/; the listener sends it
     to the approval chat (your own "Message yourself" chat by default)
  3. reply to that preview with  yes  (or  yes 4500  to set the price,  no  to skip)
  4. approved products are uploaded and published, and a confirmation with the link is sent

It also keeps uploaded watches in step with the dealer: out of stock when sold (back in stock if
available again), and the new price when the dealer changes it, unless you set the price yourself.

State lives in uploads.json next to catalog.json. Settings come from .env:
  WC_URL, WP_USER, WP_APP_PASSWORD           the site and a WordPress application password
  MARKUP_PERCENT, MARKUP_FLAT, PRICE_ROUND   website price = dealer price + markup, rounded up
  IMAGE_SIZE (1200, max side; never upscaled), IMAGE_MODE (crop|pad), IMAGE_BG (#ffffff)
"""
from __future__ import annotations

import html
import json
import math
import os
import re
import time
import uuid
from pathlib import Path

from .details import category_for, find_brand, normalise_brand, parse_reply
from .extract import parse_price


def _env_num(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def site_price(dealer_price: float | None) -> int | None:
    if not dealer_price:
        return None
    price = dealer_price * (1 + _env_num("MARKUP_PERCENT", 0) / 100) + _env_num("MARKUP_FLAT", 0)
    step = _env_num("PRICE_ROUND", 0)
    return int(math.ceil(price / step) * step) if step > 0 else int(round(price))


def prepare_images(folder: Path, names: list[str]) -> list[Path]:
    """Square, same-size JPEGs in folder/web/, so the shop grid lines up. Re-used if already made."""
    from PIL import Image, ImageOps

    size = int(_env_num("IMAGE_SIZE", 1200))
    mode = os.environ.get("IMAGE_MODE", "crop").lower()
    bg = os.environ.get("IMAGE_BG", "#ffffff")
    web = folder / "web"
    web.mkdir(exist_ok=True)
    out = []
    for name in names:
        src = folder / name
        dst = web / f"{Path(name).stem}.jpg"
        if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
            out.append(dst)
            continue
        try:
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")  # phone photos are often stored sideways
                if mode == "pad":
                    side = min(size, max(im.size))
                    im = ImageOps.pad(im, (side, side), Image.LANCZOS, color=bg)
                else:  # centre square; never upscale (WhatsApp photos are ~720px)
                    side = min(size, min(im.size))
                    im = ImageOps.fit(im, (side, side), Image.LANCZOS, centering=(0.5, 0.5))
                im.save(dst, "JPEG", quality=85, optimize=True, progressive=True)  # no EXIF: drops GPS
        except Exception as e:  # videos or broken files are left out
            print(f"    ! skipped {name}: {e}")
            continue
        out.append(dst)
    return out


def clean_description(rec: dict) -> str:
    """The listing text without the dealer's price, contact details or a repeated title line."""
    lines = []
    for ln in (rec.get("description") or "").splitlines():
        ln = ln.strip()
        if not ln or ln.lower() == (rec.get("title") or "").strip().lower():
            continue
        price, raw = parse_price(ln)
        if raw:
            ln = ln.replace(raw, "").strip(" :-,")
        ln = re.sub(r"(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}", "", ln).strip()
        if ln and not re.fullmatch(r"(?i)(price|rs\.?|inr|₹|dm( for price)?)", ln):
            lines.append(ln)
    return "\n".join(lines)


def specs(rec: dict) -> list[tuple[str, str]]:
    yes_no = lambda v: None if v is None else ("Included" if v else "Not included")
    rows = [
        ("Brand", rec.get("brand")), ("Model", rec.get("model")), ("Reference", rec.get("reference")),
        ("Year", rec.get("year")),
        ("Case size", f"{rec['case_size_mm']} mm" if rec.get("case_size_mm") else None),
        ("Case material", rec.get("case_material")), ("Dial", rec.get("dial")),
        ("Strap / bracelet", rec.get("bracelet_strap")),
        ("Movement", (rec.get("movement") or "").capitalize() or None),
        ("Condition", rec.get("condition")), ("Box", yes_no(rec.get("box"))), ("Papers", yes_no(rec.get("papers"))),
        ("Accessories", rec.get("accessories")),
    ]
    return [(k, str(v)) for k, v in rows if v]


class Woo:
    def __init__(self):
        import requests
        self.base = os.environ.get("WC_URL", "").rstrip("/")
        user, password = os.environ.get("WP_USER", ""), os.environ.get("WP_APP_PASSWORD", "")
        if not (self.base and user and password):
            raise SystemExit("Set WC_URL, WP_USER and WP_APP_PASSWORD in .env (see .env.example).")
        self.http = requests.Session()
        self.http.auth = (user, password.replace(" ", ""))
        self.http.headers["User-Agent"] = "watchbot-uploader"
        self._cats, self._cats_at = None, 0.0

    def _call(self, method: str, path: str, **kw):
        r = self.http.request(method, f"{self.base}/wp-json/{path}", timeout=120, **kw)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text[:200])
            except ValueError:
                msg = r.text[:200]
            raise RuntimeError(f"{method} {path}: {r.status_code} {msg}")
        return r.json()

    def check(self) -> str:
        me = self._call("GET", "wp/v2/users/me", params={"context": "edit"})
        self._call("GET", "wc/v3/products", params={"per_page": 1})
        return me.get("name") or me.get("slug")

    def categories(self) -> dict[str, int]:
        """The shop's product categories, name -> id (re-read every 10 minutes, so new ones show up)."""
        if self._cats is None or time.time() - self._cats_at > 600:
            cats = self._call("GET", "wc/v3/products/categories", params={"per_page": 100})
            self._cats = {html.unescape(c["name"]): c["id"] for c in cats if c.get("slug") != "uncategorized"}
            self._cats_at = time.time()
        return self._cats

    def upload_image(self, path: Path, alt: str) -> int:
        media = self._call("POST", "wp/v2/media", data=path.read_bytes(), headers={
            "Content-Type": "image/jpeg", "Content-Disposition": f'attachment; filename="{path.name}"'})
        self._call("POST", f"wp/v2/media/{media['id']}", json={"alt_text": alt, "title": alt})
        return media["id"]

    def find_sku(self, sku: str) -> dict | None:
        found = self._call("GET", "wc/v3/products", params={"sku": sku, "status": "any"})
        return found[0] if found else None

    def create(self, data: dict) -> dict:
        return self._call("POST", "wc/v3/products", json=data)

    def update(self, wc_id: int, data: dict) -> dict:
        return self._call("PUT", f"wc/v3/products/{wc_id}", json=data)

    def orders(self, modified_after: str) -> list[dict]:
        """Orders created or changed since modified_after (UTC, ISO 8601)."""
        return self._call("GET", "wc/v3/orders", params={
            "modified_after": modified_after, "dates_are_gmt": "true", "per_page": 50})


class Uploader:
    def __init__(self, out_dir: Path, dry_run: bool = False):
        self.out = Path(out_dir)
        self.catalog_path = self.out / "catalog.json"
        self.state_path = self.out / "uploads.json"
        self.outbox = self.out / "_outbox"
        self.outbox.mkdir(exist_ok=True)
        self.sent_log = self.outbox / "sent.jsonl"
        self.approvals = self.out / "_inbox" / "approvals.jsonl"
        self.state = json.loads(self.state_path.read_text("utf-8")) if self.state_path.exists() else {}
        self.state.setdefault("products", {})
        self.state.setdefault("approvals_offset", 0)
        self.dry_run = dry_run
        self.woo = None if dry_run else Woo()

    # ------------------------------------------------------------- bookkeeping
    def _save(self) -> None:
        if self.dry_run:
            return
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, ensure_ascii=False), "utf-8")
        tmp.replace(self.state_path)

    def _catalog(self) -> dict[str, dict]:
        if not self.catalog_path.exists():
            return {}
        try:
            return {r["id"]: r for r in json.loads(self.catalog_path.read_text("utf-8"))}
        except json.JSONDecodeError:
            return {}  # the saver is mid-write; try again next tick

    def _send(self, text: str, image: Path | None = None, reply_to: str | None = None) -> str:
        """Queue a WhatsApp message for the listener. Returns the outbox id.
        reply_to: the WhatsApp id of your message to answer, or "out:<outbox id>" for one of ours."""
        oid = uuid.uuid4().hex[:12]
        msg = {"id": oid, "text": text, "image": str(image) if image else None, "reply_to": reply_to}
        if self.dry_run:
            print(f"    [would send] {text.splitlines()[0]}" + (f"  + {image.name}" if image else ""))
            return oid
        tmp = self.outbox / f"{oid}.tmp"
        tmp.write_text(json.dumps(msg, ensure_ascii=False), "utf-8")
        tmp.replace(self.outbox / f"{oid}.json")  # rename so the listener never reads half a file
        return oid

    def _sent_ids(self) -> dict[str, str]:
        """outbox id -> WhatsApp message id, written by the listener after sending."""
        out = {}
        if self.sent_log.exists():
            for line in self.sent_log.read_text("utf-8").splitlines():
                try:
                    r = json.loads(line)
                    out[r["outbox_id"]] = r["wa_id"]
                except (json.JSONDecodeError, KeyError):
                    pass
        return out

    def _about(self, s: dict, text: str, image: Path | None = None, reply_to: str | None = None) -> str:
        """A message about one watch: replies to its preview (unless reply_to says otherwise), and
        replies to this message count as replies about that watch too."""
        oid = self._send(text, image, reply_to or (f"out:{s['outbox_id']}" if s.get("outbox_id") else None))
        s.setdefault("asked_ids", []).append(oid)
        return oid

    def _categories(self) -> dict[str, int]:
        return self.woo.categories() if self.woo else {}

    def _details(self, rec: dict, s: dict) -> tuple[str | None, str | None, list[str]]:
        """Brand and category for the shop (your reply wins over what was read from the post),
        and which of brand / category / price are still missing. Case and typos don't matter."""
        cats = self._categories()
        if "brand" in s:
            brand = s["brand"]  # "" = you said it has no brand
        else:
            brand = (normalise_brand(rec.get("brand"), cats)
                     or find_brand(f"{rec.get('title') or ''}\n{rec.get('description') or ''}", cats))
        category = s.get("category") or category_for(brand, cats)
        missing = [name for name, value in (("brand", brand), ("category", category if cats else "-"),
                                            ("price", s.get("price"))) if value is None or value == 0]
        return brand, category, missing

    def _ask(self, missing: list[str], title: str, first: bool = False) -> str:
        example = {"brand": "brand Casio", "category": "category Casio", "price": "4500"}
        need = ", ".join(missing)
        lines = [f"*Missing: {need}.*" if first else f"Still need *{need}* for {title}.",
                 "Reply with them, e.g. *yes " + " ".join(example[m] for m in missing) + "*"
                 + (" (or *brand none*)" if "brand" in missing else "")]
        if "category" in missing and self._categories():
            lines.append("Categories: " + ", ".join(self._categories()))
        return "\n".join(lines)

    # ------------------------------------------------------------- steps
    def _preview(self, rec: dict) -> None:
        pid = rec["id"]
        folder = self.out / rec["folder"]
        photos = prepare_images(folder, rec.get("images") or [])
        price = site_price(rec.get("price"))
        lines = [f"*New watch for the website*", f"*{rec['title']}*"]
        if price:
            dealer = f" (dealer ₹{rec['price']:,.0f})" if price != rec.get("price") else ""
            lines.append(f"Price: ₹{price:,}{dealer}")
        else:
            lines.append("Price: not found. Reply *yes <price>*, e.g. yes 4500")
        brand, category, missing = self._details(rec, {"price": price})
        lines.append(f"Brand: {brand or ('none' if brand == '' else '❓ missing')}")
        if self.woo:
            lines.append(f"Category: {category or '❓ missing'}")
        lines.append(f"Photos: {len(photos)}")
        for k, v in [r for r in specs(rec) if r[0] != "Brand"][:4]:
            lines.append(f"{k}: {v}")
        desc = clean_description(rec)
        if desc:
            lines += ["", desc[:400]]
        if rec.get("needs_review"):
            lines += ["", "⚠ Check: " + "; ".join(rec["needs_review"])]
        if rec.get("extracted_by") == "rules":
            lines.append("(details read with basic rules, please check)")
        if missing:
            lines += ["", self._ask(missing, rec["title"], first=True)]
        lines += ["", "Reply to this message: *yes* to upload, *yes 4500* to set the price, *no* to skip.", f"#{pid}"]
        oid = self._send("\n".join(lines), photos[0] if photos else None)
        self.state["products"][pid] = {"state": "previewed", "outbox_id": oid, "price": price, "at": time.time()}
        print(f"  ? preview sent: {rec['title']}" + (f" ₹{price:,}" if price else " (no price)"))

    def _read_approvals(self) -> list[dict]:
        return self._read_new(self.approvals, "approvals_offset")

    def _read_new(self, path: Path, key: str) -> list[dict]:
        """New whole lines of a .jsonl file since the offset kept in state[key]."""
        if not path.exists():
            return []
        with open(path, "rb") as f:
            f.seek(self.state.setdefault(key, 0))
            data = f.read()
        end = data.rfind(b"\n") + 1
        self.state[key] += end
        out = []
        for line in data[:end].decode("utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return out

    def _apply_approvals(self, catalog: dict[str, dict]) -> None:
        replies = self._read_approvals()
        if not replies:
            return
        sent = self._sent_ids()
        by_wa = {sent.get(oid): pid for pid, s in self.state["products"].items()
                 for oid in [s.get("outbox_id"), *s.get("asked_ids", [])]}
        for r in replies:
            text = (r.get("text") or "").strip()
            me = r.get("id")  # every answer replies to your message
            # A reply only counts for the watch whose message it tags (or that it names with #id),
            # so with several watches waiting nothing is ever applied to the wrong one.
            pid = by_wa.get(r.get("quoted_id")) if r.get("quoted_id") else None
            if not pid:
                tag = re.search(r"#(\d{8}-[a-z0-9-]+)", text)  # "yes #20261006-seiko-6309" also works
                pid = tag.group(1) if tag and tag.group(1) in self.state["products"] else None
            stock = re.fullmatch(r"(?i)(?:mark\s+(?:as\s+)?)?(sold|available|unsold|in stock)[.!]?", text)
            if not pid:
                if stock or parse_reply(text, self._categories())["decision"] or re.fullmatch(r"\d[\d,.]*k?", text):
                    self._send("Which watch? Reply to (tag) that watch's message, so nothing goes to the wrong one.",
                               reply_to=me)
                continue
            s = self.state["products"][pid]
            rec = catalog.get(pid, {"title": pid})
            title = rec.get("title", pid)
            if stock:
                self._mark(rec, s, "sold" if stock.group(1).lower() == "sold" else "available", me)
                continue
            if s["state"] not in ("previewed", "skipped"):
                continue
            reply = parse_reply(text, self._categories())
            if reply["decision"] == "no":
                s["state"] = "skipped"
                s.pop("wants_upload", None)
                print(f"  - skipped: {title}")
                self._send(f"Skipped: {title}", reply_to=me)
                continue
            given = {k: reply[k] for k in ("brand", "category", "price") if reply[k] is not None}
            s.update(given)
            if reply["decision"] == "yes":
                s["wants_upload"] = True
            notes = []
            if reply["unknown"]:
                cats = ", ".join(self._categories()) or "none on the site"
                notes.append(f"I didn't understand \"{', '.join(reply['unknown'])}\". Categories: {cats}.")
            if not s.get("wants_upload"):  # details without a yes: note them and wait for the yes
                got = ", ".join(f"{k} {'none' if v == '' else v}" for k, v in given.items())
                if got or notes:
                    self._about(s, "\n".join(([f"Noted for {title}: {got}. Reply *yes* to upload."] if got else [])
                                             + notes), reply_to=me)
                continue
            _, _, missing = self._details(rec, s)
            if missing:
                self._about(s, "\n".join(notes + [self._ask(missing, title)]), reply_to=me)
                continue
            s["state"] = "approved"
            s.pop("wants_upload", None)
            print(f"  ✓ approved: {title} ₹{s['price']:,}")

    def _upload(self, rec: dict, s: dict) -> None:
        pid, title = rec["id"], rec["title"]
        existing = self.woo.find_sku(pid)  # uploaded before a crash? don't create it twice
        if existing:
            product = existing
        else:
            photos = prepare_images(self.out / rec["folder"], rec.get("images") or [])
            image_ids = s.setdefault("media_ids", [])
            for i, p in enumerate(photos[len(image_ids):], len(image_ids) + 1):
                image_ids.append(self.woo.upload_image(p, f"{title} photo {i}"))
                self._save()  # keep uploaded media ids so a retry doesn't upload them again
            brand, category, _ = self._details(rec, s)
            rows = specs({**rec, "brand": brand or None})
            desc = clean_description(rec)
            body = "".join(f"<p>{html.escape(p)}</p>" for p in desc.split("\n") if p)
            table = "".join(f"<li><strong>{html.escape(k)}:</strong> {html.escape(v)}</li>" for k, v in rows)
            product = self.woo.create({
                "name": title,
                "type": "simple",
                "status": "publish",
                "sku": pid,
                "regular_price": str(s["price"]),
                "description": body + (f"<ul>{table}</ul>" if table else ""),
                "short_description": f"<ul>{table}</ul>" if table else "",
                "categories": [{"id": self._categories()[category]}] if category in self._categories() else [],
                "images": [{"id": i} for i in image_ids],
                "manage_stock": True,
                "stock_quantity": 0 if rec.get("status") == "sold" else 1,
                "backorders": "no",
                "sold_individually": True,
                "attributes": [{"name": k, "options": [v], "visible": True} for k, v in rows],
                "meta_data": [{"key": "_watchbot_id", "value": pid},
                              {"key": "_watchbot_dealer_price", "value": str(rec.get("price") or "")}],
            })
        s.update(state="uploaded", wc_id=product["id"], url=product.get("permalink"), at=time.time(),
                 synced_status=rec.get("status"), dealer_price=rec.get("price"))
        s.pop("error", None)
        print(f"  ↑ uploaded: {title} -> {product.get('permalink')}")
        self._about(s, f"✅ Uploaded: {title}\n₹{s['price']:,}\n{product.get('permalink')}")

    def _set_stock(self, s: dict, status: str) -> None:
        # out of stock (and no backorders) = WooCommerce hides Add to cart and refuses checkout
        self.woo.update(s["wc_id"], {"manage_stock": True, "backorders": "no",
                                     "stock_quantity": 0 if status == "sold" else 1})
        s["synced_status"] = status

    def _mark(self, rec: dict, s: dict, status: str, me: str | None = None) -> None:
        """You replied "sold" / "available" about a watch (me: your message, to reply to)."""
        title = rec.get("title", rec.get("id"))
        s.pop("asked_at", None)
        s["seen_status"] = rec.get("status")  # the dealer's older status mustn't undo this
        if s["state"] != "uploaded":
            if status == "sold" and s["state"] in ("previewed", "approved"):
                s["state"] = "skipped"
                self._send(f"OK, {title} is sold. It won't be uploaded.", reply_to=me)
            elif status == "available":
                self._send(f"{title} isn't on the website yet. Reply *yes* to its preview to upload it.", reply_to=me)
            return
        if s.get("synced_status") == status:
            self._send(f"{title} is already marked {status} on the website.", reply_to=me)
            return
        self._set_stock(s, status)
        print(f"  ~ {title}: marked {status} by you")
        self._send(f"✅ {title} marked sold. It can't be added to cart or bought now." if status == "sold"
                   else f"✅ {title} is back in stock on the website.", reply_to=me)

    def _ask_reposts(self, catalog: dict[str, dict]) -> None:
        """A watch was posted again: say it already exists and offer to mark it sold."""
        for r in self._read_new(self.out / "_inbox" / "reposts.jsonl", "reposts_offset"):
            pid, s = r.get("pid"), self.state["products"].get(r.get("pid") or "")
            rec = catalog.get(pid or "")
            if not rec or not s:
                continue
            title = rec["title"]
            if s["state"] == "uploaded" and rec.get("status") == "sold" and s.get("synced_status") != "sold":
                continue  # the post itself says sold; the stock sync handles it and tells you
            if s.get("synced_status") == "sold" or (s["state"] != "uploaded" and rec.get("status") == "sold"):
                self._about(s, f"♻ Already exists: {title}. It's already marked sold.")
                continue
            if s["state"] == "uploaded":
                text = (f"♻ Already exists on the website: *{title}*\n₹{s['price']:,}\n{s.get('url') or ''}\n\n"
                        "Reply *sold* to mark it sold (it can't be bought after that).")
            elif s["state"] == "skipped":
                text = f"♻ Already exists: *{title}*. You skipped it earlier. Reply *sold* if it's sold."
            else:
                text = f"♻ Already exists: *{title}*. It's waiting for your *yes*. Reply *sold* if it's sold."
            self._about(s, text)
            s["asked_at"] = time.time()
            print(f"  ♻ already exists: {title}")

    def _sync_status(self, rec: dict, s: dict) -> None:
        status = rec.get("status")
        if status == s.get("seen_status", s.get("synced_status")):
            return  # the dealer's status hasn't changed since we last looked
        s["seen_status"] = status
        if status == s.get("synced_status") or status not in ("sold", "available"):
            return
        self._set_stock(s, status)
        word = "marked sold (out of stock)" if status == "sold" else "back in stock"
        print(f"  ~ {rec['title']}: {word}")
        self._about(s, f"{rec['title']} {word} on the website (dealer's post).")

    def _sync_price(self, rec: dict, s: dict) -> None:
        """Dealer re-posted or replied with a new price: follow it, unless you set the price yourself."""
        dealer = rec.get("price")
        known = s.setdefault("dealer_price", dealer)
        if not dealer or dealer == known:
            return
        followed = known and s["price"] == site_price(known)  # False after a "yes 4500" override
        s["dealer_price"] = dealer
        if not known or site_price(dealer) == s["price"]:
            return  # first price seen, or the site already shows it: nothing to tell
        if followed:
            old, new = s["price"], site_price(dealer)
            self.woo.update(s["wc_id"], {"regular_price": str(new)})
            s["price"] = new
            print(f"  ~ {rec['title']}: price {old} -> {new}")
            self._about(s, f"Price updated on the website: {rec['title']}\n₹{old:,} → ₹{new:,} (dealer changed it)")
        else:
            self._about(s, f"Dealer changed the price of {rec['title']} to ₹{dealer:,.0f}. "
                       f"The website still shows your price, ₹{s['price']:,}.")

    # ------------------------------------------------------------- orders
    def _check_orders(self, catalog: dict[str, dict]) -> None:
        """WhatsApp alert for each new paid / cash-on-delivery order, with a message to forward to the dealer."""
        if time.time() < self.state.get("orders_next", 0):
            return
        self.state["orders_next"] = time.time() + 60
        now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        since = self.state.setdefault("orders_since", now)  # first run: only orders from now on
        seen = self.state.setdefault("orders", {})
        by_sku = {pid: s for pid, s in self.state["products"].items()}
        for o in sorted(self.woo.orders(since), key=lambda o: o["id"]):
            oid, status, old = str(o["id"]), o["status"], seen.get(str(o["id"]))
            if status in ("processing", "on-hold", "completed") and old not in ("processing", "on-hold", "completed"):
                self._order_alert(o, catalog, by_sku)
            elif status in ("cancelled", "refunded", "failed") and old in ("processing", "on-hold", "completed"):
                names = ", ".join(i["name"] for i in o["line_items"])
                self._send(f"↩ Order #{o['number']} {status}: {names}. WooCommerce puts the stock back.")
            seen[oid] = status
        # look back 5 minutes each time, so an order saved while we were polling isn't missed
        self.state["orders_since"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 300))

    def _order_alert(self, o: dict, catalog: dict[str, dict], by_sku: dict[str, dict]) -> None:
        total = float(o.get("total") or 0)
        b, ship = o.get("billing") or {}, o.get("shipping") or {}
        where = ", ".join(x for x in (ship.get("city") or b.get("city"), ship.get("state") or b.get("state")) if x)
        paid = "paid" if o.get("date_paid") else "not paid yet"
        lines = [f"🛒 *New order #{o['number']}*  ₹{total:,.0f}",
                 f"Status: {o['status'].replace('-', ' ')} ({o.get('payment_method_title') or 'payment'}, {paid})"]
        mine = []
        for i in o["line_items"]:
            lines.append(f"• {i['name']} × {i['quantity']}  ₹{float(i.get('total') or 0):,.0f}")
            s = by_sku.get(i.get("sku") or "")
            if s:
                mine.append((s, catalog.get(i["sku"], {})))
        name = " ".join(x for x in (b.get("first_name"), b.get("last_name")) if x)
        lines += ["", f"Customer: {name or 'unknown'}" + (f", {where}" if where else "")]
        if b.get("phone"):
            lines.append(f"Phone: {b['phone']}")
        if mine:
            lines += ["", "Check with the dealer: forward the message below 👇"]
        first = mine[0][0] if len(mine) == 1 else None
        if first:
            self._about(first, "\n".join(lines))
        else:
            self._send("\n".join(lines))
        for s, rec in mine:
            s["ordered"] = o["id"]
            title = rec.get("title") or s.get("title") or "this watch"
            photos = prepare_images(self.out / rec["folder"], (rec.get("images") or [])[:1]) if rec.get("folder") else []
            dealer = f" (₹{rec['price']:,.0f})" if rec.get("price") else ""
            self._send(f"Hi, is the *{title}*{dealer} still available? I'd like to take it.",
                       photos[0] if photos else None)
        print(f"  🛒 order #{o['number']} ₹{total:,.0f}")

    # ------------------------------------------------------------- loop
    def tick(self) -> None:
        catalog = self._catalog()
        products = self.state["products"]
        for pid, rec in catalog.items():
            if pid not in products and rec.get("status") != "sold":
                self._preview(rec)
                self._save()
        self._ask_reposts(catalog)
        self._apply_approvals(catalog)
        self._save()
        if self.dry_run:
            return
        for pid, s in products.items():
            rec = catalog.get(pid)
            if not rec:
                continue
            try:
                if s["state"] == "approved" and time.time() >= s.get("retry_at", 0):
                    self._upload(rec, s)
                elif s["state"] == "uploaded":
                    self._sync_status(rec, s)
                    self._sync_price(rec, s)
            except Exception as e:
                fails = s.get("fails", 0) + 1
                s.update(error=str(e)[:300], fails=fails, retry_at=time.time() + min(60 * 2 ** fails, 3600))
                print(f"  ! {rec['title']}: {e}")
                if fails == 1:
                    self._about(s, f"⚠ Upload failed for {rec['title']}: {str(e)[:200]}\nI'll keep retrying.")
            self._save()
        try:
            self._check_orders(catalog)
        except Exception as e:  # the site being down mustn't stop uploads
            print(f"  ! {e}")
        self._save()

    def run(self, interval: int = 10) -> None:
        if self.woo:
            print(f"Uploader connected to {self.woo.base} as {self.woo.check()}")
        print(f"  Markup {_env_num('MARKUP_PERCENT', 0):g}% + ₹{_env_num('MARKUP_FLAT', 0):g}, "
              f"images {int(_env_num('IMAGE_SIZE', 1200))}px {os.environ.get('IMAGE_MODE', 'crop')}")
        while True:
            self.tick()
            time.sleep(interval)
