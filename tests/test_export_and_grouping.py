"""python -m unittest discover tests

Chat exports as Android and iPhone write them, and the grouping of a dealer's
photos and text into one candidate product per watch.
"""
from __future__ import annotations

import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

from watchbot.grouping import bursts, group_products, is_listing_text
from watchbot.parse_export import Message, parse_export

LRM, NNBSP = "‎", " "

ANDROID = "\n".join([
    "12/03/2026, 10:14 pm - Messages and calls are end-to-end encrypted. Tap to learn more.",
    "12/03/2026, 10:15 pm - Ravi Watches: IMG-20260312-WA0004.jpg (file attached)",
    "12/03/2026, 10:15 pm - Ravi Watches: IMG-20260312-WA0005.jpg (file attached)",
    "Seiko 6309 JDM",
    "12/03/2026, 10:16 pm - Ravi Watches: Rolex DJ 41 126334 blue dial",
    "2021 full set",
    "Price 11.75L",
    "13/03/2026, 9:02 am - Asha: sold?",
    "13/03/2026, 9:03 am - Ravi Watches: <Media omitted>",
])

IPHONE = "\n".join([
    f"{LRM}[3/14/26, 10:15:30{NNBSP}PM] Ravi Watches: {LRM}<attached: 00000012-PHOTO-2026-03-14-22-15-30.jpg>",
    f"[3/14/26, 10:16:02{NNBSP}PM] Ravi Watches: Omega Speedmaster 3570.50",
    "Box and papers",
])


def write_export(folder: Path, chat: str, photos: list[str]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "WhatsApp Chat with Ravi Watches.txt").write_text(chat, encoding="utf-8")
    for name in photos:
        (folder / name).write_bytes(b"\xff\xd8")
    return folder


class ParseExportTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_android(self):
        src = write_export(self.tmp / "android", ANDROID, ["IMG-20260312-WA0004.jpg"])
        export = parse_export(src, self.tmp / "work")
        msgs = export.messages

        self.assertEqual(len(msgs), 5)  # the encryption notice is not a message
        self.assertEqual(export.senders, {"Ravi Watches": 4, "Asha": 1})
        self.assertEqual(msgs[0].timestamp, datetime(2026, 3, 12, 22, 15))  # 13/03 later on: day first
        self.assertEqual(msgs[0].media, src / "IMG-20260312-WA0004.jpg")
        self.assertEqual(msgs[1].kind, "image")
        self.assertTrue(msgs[1].media_missing)                  # attached, but not in the folder
        self.assertEqual(msgs[1].text, "Seiko 6309 JDM")        # the line under a photo is its caption
        self.assertEqual(msgs[2].text, "Rolex DJ 41 126334 blue dial\n2021 full set\nPrice 11.75L")
        self.assertEqual(msgs[3].timestamp, datetime(2026, 3, 13, 9, 2))
        self.assertTrue(msgs[4].media_missing)                  # exported without media
        self.assertEqual(export.missing_media, 2)

    def test_iphone(self):
        src = write_export(self.tmp / "iphone", IPHONE, ["00000012-PHOTO-2026-03-14-22-15-30.jpg"])
        msgs = parse_export(src, self.tmp / "work").messages

        self.assertEqual(len(msgs), 2)
        # invisible marks and the narrow no-break space before PM are cleaned away
        self.assertEqual(msgs[0].timestamp, datetime(2026, 3, 14, 22, 15, 30))  # 3/14: month first
        self.assertEqual(msgs[0].sender, "Ravi Watches")
        self.assertEqual(msgs[0].media.name, "00000012-PHOTO-2026-03-14-22-15-30.jpg")
        self.assertEqual(msgs[1].text, "Omega Speedmaster 3570.50\nBox and papers")

    def test_zip(self):
        src = write_export(self.tmp / "android", ANDROID, ["IMG-20260312-WA0004.jpg"])
        archive = self.tmp / "WhatsApp Chat with Ravi Watches.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            for p in src.iterdir():
                zf.write(p, p.name)
        export = parse_export(archive, self.tmp / "work")
        self.assertEqual(len(export.messages), 5)
        self.assertEqual(export.messages[0].media.name, "IMG-20260312-WA0004.jpg")

    def test_folder_without_chat(self):
        (self.tmp / "empty").mkdir()
        with self.assertRaises(FileNotFoundError):
            parse_export(self.tmp / "empty", self.tmp / "work")


T0 = datetime(2026, 3, 12, 22, 0)


def chat(*items: tuple[int, str, str]) -> list[Message]:
    """(minute, sender, body) where a body ending in .jpg is a photo."""
    out = []
    for i, (minute, sender, body) in enumerate(items):
        m = Message(timestamp=T0 + timedelta(minutes=minute), sender=sender, raw_index=i)
        if body.endswith(".jpg"):
            m.media = Path(body)
        else:
            m.text = body
        out.append(m)
    return out


def photos(group) -> list[str]:
    return [m.media.name for m in group.media]


class GroupingTest(unittest.TestCase):
    def test_listing_text(self):
        for text in ("sold", "Ok", "👍", "on hold!", "", "   "):
            self.assertFalse(is_listing_text(text), text)
        for text in ("Price 4.6L", "Seiko 6309", "Omega Seamaster blue"):
            self.assertTrue(is_listing_text(text), text)

    def test_bursts_split_on_sender_and_pause(self):
        msgs = chat((0, "Ravi", "a.jpg"), (1, "Ravi", "b.jpg"), (2, "Asha", "nice"),
                    (3, "Ravi", "c.jpg"), (30, "Ravi", "d.jpg"))
        self.assertEqual([len(b) for b in bursts(msgs, gap_minutes=15)], [2, 1, 1, 1])

    def test_details_after_photos(self):
        msgs = chat(
            (0, "Ravi", "1.jpg"), (0, "Ravi", "2.jpg"), (1, "Ravi", "Seiko 6309 JDM\n₹3650"),
            (2, "Ravi", "3.jpg"), (2, "Ravi", "4.jpg"), (2, "Ravi", "5.jpg"), (3, "Ravi", "HMT Janata 1985"),
            (4, "Ravi", "Price 2800"),   # a short follow-up belongs to the watch above
            (5, "Ravi", "sold"),         # chatter is a note, not a product
        )
        groups, layout = group_products(msgs)
        self.assertEqual(layout, "after")
        self.assertEqual([photos(g) for g in groups], [["1.jpg", "2.jpg"], ["3.jpg", "4.jpg", "5.jpg"]])
        self.assertEqual(groups[1].text, "HMT Janata 1985\nPrice 2800")
        self.assertEqual(groups[1].notes, ["sold"])
        self.assertFalse(any(g.needs_review for g in groups))

    def test_details_before_photos(self):
        msgs = chat(
            (0, "Ravi", "Seiko 6309 JDM ₹3650"), (0, "Ravi", "1.jpg"), (0, "Ravi", "2.jpg"),
            (1, "Ravi", "HMT Janata 1985 Rs 2800"), (1, "Ravi", "3.jpg"),
        )
        groups, layout = group_products(msgs)
        self.assertEqual(layout, "before")
        self.assertEqual([photos(g) for g in groups], [["1.jpg", "2.jpg"], ["3.jpg"]])
        self.assertEqual([g.text for g in groups], ["Seiko 6309 JDM ₹3650", "HMT Janata 1985 Rs 2800"])

    def test_only_the_chosen_sellers(self):
        msgs = chat((0, "Asha", "x.jpg"), (0, "Asha", "Casio for sale 900"),
                    (1, "Ravi", "1.jpg"), (1, "Ravi", "Seiko 6309 ₹3650"))
        groups, _ = group_products(msgs, sellers={"Ravi"})
        self.assertEqual([g.sender for g in groups], ["Ravi"])

    def test_text_only_chat_makes_no_products(self):
        msgs = chat((0, "Ravi", "Seiko 6309 ₹3650"), (1, "Ravi", "HMT Janata 1985"))
        self.assertEqual(group_products(msgs)[0], [])

    def test_doubts_are_flagged(self):
        msgs = chat(
            (0, "Ravi", "1.jpg"), (0, "Ravi", "2.jpg"),                      # photos, never described
            (30, "Ravi", "Seiko 6309 ₹3650"), *[(31, "Ravi", f"s{i}.jpg") for i in range(13)],
        )
        groups, _ = group_products(msgs, layout="before")
        self.assertEqual(groups[0].needs_review, ["photos without any details text"])
        self.assertEqual(groups[1].needs_review, ["13 photos on one listing; may be two watches"])

    def test_missing_photos_are_flagged(self):
        msgs = chat((0, "Ravi", "1.jpg"), (0, "Ravi", "Seiko 6309 ₹3650"))
        msgs[0].media, msgs[0].media_missing = None, True
        groups, _ = group_products(msgs)
        self.assertIn("some photos missing from the export (export with media)", groups[0].needs_review)


if __name__ == "__main__":
    unittest.main()
