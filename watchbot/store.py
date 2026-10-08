"""Write products to disk and keep a catalog that survives repeated imports.

output/
  products/<date>-<slug>/        one folder per watch
      01.jpg, 02.jpg, ...        photos in the order the seller posted them
      product.json               extracted fields + source info
      source.txt                 the seller's original text
  catalog.csv / catalog.json     every product, one row each
  state.json                     fingerprints used to skip duplicates on re-import
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

CSV_FIELDS = [
    "id", "status", "needs_review", "brand", "model", "reference", "nickname", "year", "case_size_mm",
    "case_material", "dial", "bracelet_strap", "movement", "condition", "box", "papers", "accessories",
    "price", "currency", "price_text", "title", "description", "image_count", "folder", "posted_at",
    "seller", "extracted_by", "confidence",
]


def _sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def _slug(text: str, limit: int = 50) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].rstrip("-") or "watch"


def _norm_text(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


class Store:
    def __init__(self, out_dir: Path):
        self.out = Path(out_dir)
        self.products_dir = self.out / "products"
        self.products_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.out / "state.json"
        self.catalog_path = self.out / "catalog.json"
        state = json.loads(self.state_path.read_text("utf-8")) if self.state_path.exists() else {}
        self.image_hashes: dict[str, str] = state.get("image_hashes", {})  # sha1 -> product id
        self.text_hashes: dict[str, str] = state.get("text_hashes", {})
        self.catalog: list[dict] = json.loads(self.catalog_path.read_text("utf-8")) if self.catalog_path.exists() else []

    def find_duplicate(self, text: str, images: list[Path]) -> str | None:
        """A re-post counts as a duplicate if every photo was seen before, or (no photos) same text."""
        hashes = [_sha1(p) for p in images]
        if hashes and all(h in self.image_hashes for h in hashes):
            return self.image_hashes[hashes[0]]
        if not hashes and text and hashlib.sha1(_norm_text(text).encode()).hexdigest() in self.text_hashes:
            return self.text_hashes[hashlib.sha1(_norm_text(text).encode()).hexdigest()]
        return None

    def add(self, listing: dict, text: str, notes: list[str], images: list[Path], posted_at: datetime,
            seller: str, needs_review: list[str], extracted_by: str) -> dict:
        base = f"{posted_at:%Y%m%d}-{_slug(listing.get('title') or text[:40])}"
        pid, n = base, 2
        while (self.products_dir / pid).exists():
            pid, n = f"{base}-{n}", n + 1
        folder = self.products_dir / pid
        folder.mkdir()

        image_names = []
        for i, src in enumerate(images, 1):
            name = f"{i:02d}{src.suffix.lower()}"
            shutil.copy2(src, folder / name)
            image_names.append(name)
            self.image_hashes.setdefault(_sha1(src), pid)
        if text:
            self.text_hashes.setdefault(hashlib.sha1(_norm_text(text).encode()).hexdigest(), pid)

        record = {
            "id": pid,
            **listing,
            "needs_review": needs_review,
            "images": image_names,
            "image_count": len(image_names),
            "folder": str(folder.relative_to(self.out)).replace("\\", "/"),
            "posted_at": posted_at.isoformat(),
            "seller": seller,
            "extracted_by": extracted_by,
            "source_text": text,
            "seller_notes": notes,
            "source_files": [p.name for p in images],
        }
        (folder / "product.json").write_text(json.dumps(record, indent=2, ensure_ascii=False), "utf-8")
        (folder / "source.txt").write_text(text + ("\n\n--- follow-ups ---\n" + "\n".join(notes) if notes else ""), "utf-8")
        self.catalog.append(record)
        return record

    def update(self, pid: str, changes: dict, seen_at: datetime) -> dict | None:
        """Apply a re-post's new price/status to an existing product, keeping a history."""
        rec = next((r for r in self.catalog if r["id"] == pid), None)
        if rec is None or not changes:
            return None
        rec.setdefault("history", []).append(
            {"at": seen_at.isoformat(), **{k: {"from": rec.get(k), "to": v} for k, v in changes.items()}})
        rec.update(changes)
        (self.out / rec["folder"] / "product.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False), "utf-8")
        return rec

    def remove(self, pid: str) -> bool:
        """Delete a product completely (folder, catalog row, duplicate fingerprints)."""
        before = len(self.catalog)
        self.catalog = [r for r in self.catalog if r["id"] != pid]
        self.image_hashes = {h: p for h, p in self.image_hashes.items() if p != pid}
        self.text_hashes = {h: p for h, p in self.text_hashes.items() if p != pid}
        shutil.rmtree(self.products_dir / pid, ignore_errors=True)
        self.save()
        return len(self.catalog) < before

    def save(self) -> None:
        self.state_path.write_text(json.dumps(
            {"image_hashes": self.image_hashes, "text_hashes": self.text_hashes}, indent=1), "utf-8")
        self.catalog_path.write_text(json.dumps(self.catalog, indent=2, ensure_ascii=False), "utf-8")
        with open(self.out / "catalog.csv", "w", newline="", encoding="utf-8-sig") as f:  # -sig so Excel shows ₹ correctly
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in self.catalog:
                w.writerow({**r, "needs_review": "; ".join(r.get("needs_review") or [])})
