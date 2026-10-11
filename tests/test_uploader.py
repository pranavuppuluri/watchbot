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
from watchbot.extract import extract_with_rules, parse_price


class RulesTest(unittest.TestCase):
    def test_listings(self):
        cases = [
            ("Seiko 6309 \nJDM with Kanji\n₹3650", "Seiko", "6309", 3650),
            ("Casio Mudmaster \nAll original \n₹7150", "Casio", None, 7150),   # price isn't the reference
            ("Nice vintage HMT Janata 1985 Rs 2,800", "HMT", None, 2800),      # "vintage" isn't TAG Heuer
            ("Rolex DJ 41 126334 blue dial\n2021 full set\nPrice 11.75L", "Rolex", "126334", 1_175_000),
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


class FakeWoo:
    base = "https://shop.test"

    def __init__(self):
        self.products, self.media = {}, 0

    def check(self):
        return "tester"

    def category_ids(self, brand):
        return [7] if brand == "Seiko" else []

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

    def _reply(self, text, to_pid=None):
        quoted = "WA-" + self.up.state["products"][to_pid]["outbox_id"] if to_pid else None
        with open(self.out / "_inbox" / "approvals.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": text, "text": text, "quoted_id": quoted}) + "\n")

    def test_preview_approve_upload_and_sync(self):
        seiko, casio = "20261006-seiko-6309", "20261012-casio-mudmaster"
        self.up.tick()
        states = self.up.state["products"]
        self.assertEqual({p: s["state"] for p, s in states.items()}, {seiko: "previewed", casio: "previewed"})
        self._deliver_outbox()

        self._reply("yes", seiko)          # no price yet: asks for one
        self._reply("yes 4,200", seiko)
        self._reply("no")                  # not quoted: the latest preview still waiting (casio)
        self.up.tick()
        self.assertEqual(states[seiko]["state"], "uploaded")
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


if __name__ == "__main__":
    unittest.main()
