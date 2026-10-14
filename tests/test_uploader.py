"""python -m unittest discover tests"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from watchbot import uploader as U
from watchbot.details import category_for, find_brand, normalise_brand, parse_reply
from watchbot.extract import extract_with_rules, parse_price


class RulesTest(unittest.TestCase):
    def test_listings(self):
        cases = [
            ("Seiko 6309 \nJDM with Kanji\n₹3650", "Seiko", "6309", 3650),
            ("Casio Mudmaster \nAll original \n₹7150", "Casio", None, 7150),   # price isn't the reference
            ("Nice vintage HMT Janata 1985 Rs 2,800", "HMT", None, 2800),      # "vintage" isn't TAG Heuer
            ("Rolex DJ 41 126334 blue dial\n2021 full set\nPrice 11.75L", "Rolex", "126334", 1_175_000),
            ("Swiss Adriatica Tank \nAll original \nLarge Size\n3450", None, None, 3450),  # bare last line is the price
            ("HMT Janata\n1985", "HMT", None, None),                                       # a year isn't a price
        ]
        for text, brand, ref, price in cases:
            got = extract_with_rules(text)
            self.assertEqual((got.brand, got.reference, got.price), (brand, ref, price), text)

    def test_plain_numbers_are_not_prices(self):
        self.assertEqual(parse_price("ref 6309 from 1985, 38mm"), (None, None))


class HelpersTest(unittest.TestCase):
    def test_site_price(self):
        with mock.patch.dict(os.environ, {"MARKUP_PERCENT": "", "MARKUP_FLAT": "", "PRICE_ROUND": ""}):
            self.assertEqual(U.site_price(3650), 3650)
            self.assertIsNone(U.site_price(None))
        with mock.patch.dict(os.environ, {"MARKUP_PERCENT": "20", "MARKUP_FLAT": "0", "PRICE_ROUND": "100"}):
            self.assertEqual(U.site_price(3650), 4400)

    def test_description_drops_title_price_and_phone(self):
        rec = {"title": "Seiko 6309", "description": "Seiko 6309\nJDM with Kanji\n₹3650\nCall 98765 43210"}
        self.assertEqual(U.clean_description(rec), "JDM with Kanji\nCall")

    def test_photos_are_square_without_upscaling(self):
        with tempfile.TemporaryDirectory() as tmp:
            Image.new("RGB", (720, 1280), "gray").save(Path(tmp) / "01.jpg")
            out = U.prepare_images(Path(tmp), ["01.jpg"])
            self.assertEqual(Image.open(out[0]).size, (720, 720))


CATS = {"Allwyn": 1, "Anomalies": 2, "Casio": 3, "HMT": 4, "Seiko": 5, "Swiss": 6, "Timex": 7, "Titan": 8, "USSR": 9}


class DetailsTest(unittest.TestCase):
    def test_replies_ignore_case_and_typos(self):
        cases = {
            "YES": ("yes", None, None, None),
            "yse 4,500": ("yes", None, None, 4500),
            "Noo": ("no", None, None, None),
            "brand omga catagory swis 4.5k": (None, "Omega", "Swiss", 4500),
            "Yes Brnd: Tag Heuer, CATEGORY swiss, price 12000": ("yes", "Tag Heuer", "Swiss", 12000),
            "yes sieko 4200": ("yes", "Seiko", "Seiko", 4200),
            "yes g shock": ("yes", "Casio", None, None),
            "yes brand none category anomalies 3000": ("yes", "", "Anomalies", 3000),
            "brand Ajeeth Special": (None, "Ajeeth Special", None, None),
        }
        for text, want in cases.items():
            r = parse_reply(text, CATS)
            self.assertEqual((r["decision"], r["brand"], r["category"], r["price"]), want, text)
        self.assertEqual(parse_reply("yes category watches", CATS)["unknown"], ["watches"])

    def test_brand_and_category_from_the_post(self):
        self.assertEqual(find_brand("New G-shock available 4.2k", CATS), "Casio")
        self.assertEqual(find_brand("Rolax datejust 36", CATS), "Rolex")
        self.assertIsNone(find_brand("time only, good condition", CATS))  # "time" is not Timex
        self.assertEqual(normalise_brand("SEIKO", CATS), "Seiko")
        self.assertEqual((category_for("Omega", CATS), category_for("Raketa", CATS), category_for("titan", CATS)),
                         ("Swiss", "USSR", "Titan"))


class FakeWoo:
    base = "https://shop.test"

    def __init__(self):
        self.products, self.media = {}, 0

    def check(self):
        return "tester"

    def categories(self):
        return {"Seiko": 7, "Casio": 8, "Swiss": 9, "Anomalies": 10}

    def upload_image(self, path, alt):
        self.media += 1
        return self.media

    def find_sku(self, sku):
        return next((p for p in self.products.values() if p["sku"] == sku), None)

    def create(self, data):
        pid = len(self.products) + 1
        self.products[pid] = {**data, "id": pid, "permalink": f"{self.base}/product/{pid}"}
        return self.products[pid]

    def update(self, wc_id, data):
        self.products[wc_id].update(data)


class ApprovalFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        for pid, price in (("20261006-seiko-6309", None), ("20261012-casio-mudmaster", 7150.0)):
            folder = self.out / "products" / pid
            folder.mkdir(parents=True)
            Image.new("RGB", (720, 1280), "gray").save(folder / "01.jpg")
        self.catalog = [
            {"id": "20261006-seiko-6309", "title": "Seiko 6309", "brand": "Seiko", "price": None,
             "description": "JDM", "images": ["01.jpg"], "folder": "products/20261006-seiko-6309"},
            {"id": "20261012-casio-mudmaster", "title": "Casio Mudmaster", "brand": "Casio", "price": 7150.0,
             "description": "", "images": ["01.jpg"], "folder": "products/20261012-casio-mudmaster"},
        ]
        self._write_catalog()
        (self.out / "_inbox").mkdir()
        with mock.patch.object(U, "Woo", FakeWoo):
            self.up = U.Uploader(self.out)
        self.addCleanup(self.tmp.cleanup)

    def _write_catalog(self):
        (self.out / "catalog.json").write_text(json.dumps(self.catalog), "utf-8")

    def _deliver_outbox(self) -> dict[str, str]:
        """Do what the listener does: send queued messages and log their WhatsApp ids."""
        ids = {}
        with open(self.out / "_outbox" / "sent.jsonl", "a") as f:
            for o in sorted((self.out / "_outbox").glob("*.json")):
                msg = json.loads(o.read_text("utf-8"))
                ids[msg["id"]] = "WA-" + msg["id"]
                f.write(json.dumps({"outbox_id": msg["id"], "wa_id": ids[msg["id"]]}) + "\n")
                o.unlink()
        return ids

    def _reply(self, text, to_pid=None, to_msg=None):
        """Your reply in the approval chat, tagging a watch's preview (to_pid) or one of our messages (to_msg)."""
        oid = to_msg or (self.up.state["products"][to_pid]["outbox_id"] if to_pid else None)
        with open(self.out / "_inbox" / "approvals.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": "ME-" + text, "text": text, "quoted_id": "WA-" + oid if oid else None}) + "\n")

    def _sent(self) -> list[dict]:
        msgs = [json.loads(o.read_text("utf-8")) for o in sorted((self.out / "_outbox").glob("*.json"),
                                                                 key=lambda o: o.stat().st_mtime_ns)]
        self._deliver_outbox()
        return msgs

    def test_preview_approve_upload_and_sync(self):
        seiko, casio = "20261006-seiko-6309", "20261012-casio-mudmaster"
        self.up.tick()
        states = self.up.state["products"]
        self.assertEqual({p: s["state"] for p, s in states.items()}, {seiko: "previewed", casio: "previewed"})
        self._deliver_outbox()

        self._reply("yes", seiko)          # no price yet: asks for one
        self._reply("yes 4,200", seiko)
        self._reply("no")                  # not tagged: with two watches waiting, it applies to neither
        self.up.tick()
        self.assertEqual(states[seiko]["state"], "uploaded")
        self.assertEqual(states[casio]["state"], "previewed")
        asked = [m for m in self._sent() if m["text"].startswith("Which watch?")]
        self.assertEqual([m["reply_to"] for m in asked], ["ME-no"])   # answers your message
        self._reply("no", casio)
        self.up.tick()
        self.assertEqual(states[casio]["state"], "skipped")
        product = self.up.woo.products[states[seiko]["wc_id"]]
        self.assertEqual((product["regular_price"], product["categories"], product["stock_quantity"]),
                         ("4200", [{"id": 7}], 1))

        self.catalog[0]["status"] = "sold"
        self._write_catalog()
        self.up.tick()
        self.assertEqual(product["stock_quantity"], 0)

        before = len(self.up.woo.products)
        again = U.Uploader.__new__(U.Uploader)  # restart: nothing is previewed or uploaded twice
        with mock.patch.object(U, "Woo", lambda: self.up.woo):
            again.__init__(self.out)
        self._deliver_outbox()
        again.tick()
        self.assertEqual(len(again.woo.products), before)
        self.assertEqual(list((self.out / "_outbox").glob("*.json")), [])

    def test_repost_asks_and_sold_reply_stops_sales(self):
        seiko = "20261006-seiko-6309"
        self.up.tick()
        self._deliver_outbox()
        self._reply("yes 3650", seiko)
        self.up.tick()
        s = self.up.state["products"][seiko]
        product = self.up.woo.products[s["wc_id"]]
        self.catalog[0]["status"] = "available"  # an older dealer reply
        self._write_catalog()
        self.up.tick()
        self._deliver_outbox()

        with open(self.out / "_inbox" / "reposts.jsonl", "a") as f:  # what the saver writes for a re-post
            f.write(json.dumps({"pid": seiko, "at": 1}) + "\n")
        self.up.tick()
        self.assertIn("Already exists on the website", self._sent_texts()[-1])
        self._deliver_outbox()

        self._reply("Sold")                       # not tagged: asks which watch, changes nothing
        self.up.tick()
        self.assertEqual(product["stock_quantity"], 1)
        self.assertTrue(self._sent()[-1]["text"].startswith("Which watch?"))
        self._reply("Sold", to_msg=s["asked_ids"][-1])   # tagging the "Already exists" message
        self.up.tick()
        self.assertEqual((product["stock_quantity"], product["backorders"]), (0, "no"))
        self.assertEqual(self._sent()[-1]["reply_to"], "ME-Sold")
        self.up.tick()                            # the dealer's older "available" doesn't undo it
        self.assertEqual(product["stock_quantity"], 0)
        self._reply("available", seiko)
        self.up.tick()
        self.assertEqual(product["stock_quantity"], 1)

    def _sent_texts(self) -> list[str]:
        texts = [json.loads(o.read_text("utf-8"))["text"] for o in sorted((self.out / "_outbox").glob("*.json"),
                                                                          key=lambda o: o.stat().st_mtime_ns)]
        self._deliver_outbox()
        return texts

    def test_missing_details_are_asked_for(self):
        pid = "20261012-very-rare-ajeeth"
        folder = self.out / "products" / pid
        folder.mkdir(parents=True)
        Image.new("RGB", (800, 800), "gray").save(folder / "01.jpg")
        self.catalog = [{"id": pid, "title": "Very rare Ajeeth", "brand": None, "price": None, "description": "",
                         "images": ["01.jpg"], "folder": f"products/{pid}"}]
        self._write_catalog()
        self.up.tick()
        preview = self._sent_texts()[0]
        self.assertIn("Missing: brand, category, price", preview)
        self.assertIn("Categories: Seiko, Casio, Swiss, Anomalies", preview)

        s = self.up.state["products"][pid]
        self._reply("Yes", pid)                      # still missing everything: asks again, no upload
        self.up.tick()
        self.assertEqual(s["state"], "previewed")
        self.assertIn("Still need *brand, category, price*", self._sent_texts()[-1])

        self._reply("brnd ajeeth", pid)              # the earlier yes still counts; two left
        self.up.tick()
        self.assertIn("Still need *category, price*", self._sent_texts()[-1])

        self._reply("CATAGORY anomalys 3k", pid)     # typo'd keyword and category, short price
        self.up.tick()
        self.assertEqual(s["state"], "uploaded")
        product = self.up.woo.products[s["wc_id"]]
        self.assertEqual((product["regular_price"], product["categories"]), ("3000", [{"id": 10}]))
        self.assertIn({"name": "Brand", "options": ["Ajeeth"], "visible": True}, product["attributes"])

    def test_details_without_yes_wait_for_yes(self):
        seiko = "20261006-seiko-6309"
        self.up.tick()
        self._deliver_outbox()
        self._reply("price 4200", seiko)
        self.up.tick()
        self.assertEqual(self.up.state["products"][seiko]["state"], "previewed")
        self.assertIn("Noted for Seiko 6309: price 4200", self._sent_texts()[-1])
        self._reply("ok", seiko)
        self.up.tick()
        self.assertEqual(self.up.state["products"][seiko]["state"], "uploaded")


if __name__ == "__main__":
    unittest.main()
