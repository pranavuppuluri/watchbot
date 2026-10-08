"""Minimal WhatsApp Cloud API client (Meta Graph API): download media, send text."""
from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import urllib.request
from pathlib import Path

_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "video/mp4": ".mp4", "video/3gpp": ".3gp"}


class WhatsApp:
    def __init__(self, token: str, phone_number_id: str, api_version: str = "v23.0"):
        self.token = token
        self.phone_number_id = phone_number_id
        self.base = f"https://graph.facebook.com/{api_version}"

    def _request(self, url: str, data: dict | None = None) -> bytes:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        if data is not None:
            req.data = json.dumps(data).encode()
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()

    def download_media(self, media_id: str, dest_dir: Path) -> Path:
        """Media arrives as an id: look up its short-lived URL, then fetch the bytes (same token)."""
        meta = json.loads(self._request(f"{self.base}/{media_id}"))
        mime = meta.get("mime_type", "").split(";")[0]
        ext = _EXT.get(mime) or mimetypes.guess_extension(mime) or ".bin"
        dest = Path(dest_dir) / f"{media_id}{ext}"
        dest.write_bytes(self._request(meta["url"]))
        return dest

    def send_text(self, to: str, body: str) -> None:
        self._request(f"{self.base}/{self.phone_number_id}/messages", {
            "messaging_product": "whatsapp", "to": to, "type": "text",
            "text": {"body": body[:4000], "preview_url": False},
        })


def valid_signature(app_secret: str, body: bytes, header: str | None) -> bool:
    """Check Meta's X-Hub-Signature-256 so only WhatsApp can post to the webhook."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])
