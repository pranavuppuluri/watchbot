# watchbot

Turns a used-watch dealer's WhatsApp posts into a clean product catalog: one folder per watch with its photos, structured details (brand, model, reference, year, condition, box/papers, price), and a spreadsheet, ready to upload to an online store.

## Why this exists

A lot of India's pre-owned watch trade happens in WhatsApp groups. A dealer posts a few photos of a watch, then a line like:

```
Rolex DJ 41 126334 blue dial fluted
2021 full set, mint unpolished
Price 11.75L
```

…and then does the same for five more watches, back to back. Reselling that stock on a website means someone copies every photo and retypes every detail by hand, and keeps up when the dealer says "sold" or drops a price.

watchbot does that copying automatically. It reads the group, works out which photos belong to which watch, pulls out the details, and keeps the catalog current when the dealer marks a watch sold or changes a price.

## How it works

```
 WhatsApp group
      │
      │  three ways in (pick one)
      ├── 1. Chat export (.zip)          python -m watchbot ingest
      ├── 2. Live listener (Baileys)     listener/index.mjs  +  python -m watchbot watch
      └── 3. Forward to a bot number     python -m watchbot serve   (official Meta Cloud API)
      │
      ▼
 parse      →  messages: who, when, text, attached photo
 group      →  which photos + text make up one watch
 extract    →  brand, model, ref, year, price… (Claude, or regex fallback)
 store      →  output/products/<date>-<title>/  +  catalog.csv / catalog.json
```

All three inputs feed the same pipeline (`watchbot/pipeline.py`), so a watch saved from an export and the same watch seen live are recognised as one product.

## How it was built

**Getting the messages out of WhatsApp.** Meta's official WhatsApp API can only see chats with your own business number. It cannot read a group someone else runs. That left three options, and the project supports all of them:

| Route | How | Trade-off |
|---|---|---|
| Chat export | WhatsApp's built-in *Export chat → Include media*. `parse_export.py` reads the `.txt` plus the photos. | Zero risk, but manual and not real-time. |
| Live listener | [Baileys](https://baileys.wiki) links a spare number to the PC as a "linked device" (like WhatsApp Web) and saves every group message as it arrives. It never posts in the dealer's group; it only sends upload previews to your approval chat. | Fully automatic, but unofficial: the linked number can be banned, so use a spare SIM. |
| Forwarding bot | Official Cloud API webhook (`bot.py`). You forward the dealer's posts to the bot, and it replies with what it saved. | Official and safe, but forwarding is manual and it needs a Meta app and a public URL (ngrok). |

**Parsing exports.** Android and iPhone exports use different line formats (`12/03/2026, 10:15 pm - Name: …` and `[12/03/26, 10:15:30 PM] Name: …`), contain invisible direction marks and narrow no-break spaces, and follow the phone's date locale. The parser strips the invisible characters, detects day-first or month-first from the data, and joins multi-line messages and photo captions.

**Grouping photos into watches** (`grouping.py`) is the hard part, because a dealer posts several watches in a row. The approach:
1. Split the chat into *bursts*: consecutive messages from one person with no long pause.
2. Inside a burst, pair each listing-like text with the photos next to it. Dealers are consistent about whether details come *after* the photos or *before* them, so both layouts are tried across the whole chat and the one leaving the fewest orphaned photos wins.
3. A short follow-up ("Price 4.6L") joins the watch above it; chatter ("ok", "sold", 👍) becomes a note, not a product.
4. Anything uncertain (photos with no details, details with no photos, a suspiciously large photo set) is flagged `needs_review` instead of guessed.

**Extracting details** (`extract.py`). Each listing goes to Claude with a Pydantic schema via structured outputs, so the response is always valid, typed JSON. The prompt explains dealer shorthand (DJ = Datejust, FS = full set, 6.5L = ₹6,50,000) and says to leave a field empty rather than guess. Without an API key, a regex fallback still finds the brand, reference, year, case size, box/papers and Indian-format prices (L / lakh / Cr / k / ₹).

**Staying in sync** (`store.py`, `pipeline.py`). Every photo is fingerprinted (SHA-1), so re-importing an overlapping export or a re-post doesn't create duplicates. When the dealer re-posts a watch with a new price, or *replies* to their own post with "sold" / "on hold" / a new price (the live listener records which message a reply quotes), the existing product is updated and the change is logged under `history`. Older posts never overwrite newer data.

**Testing.** Every input was tested end to end on synthetic data: a fake Android export, simulated Meta webhook calls (signature checks, retries, unknown senders, `undo`), and simulated listener output (seller replies, restarts without re-saving).

## Setup

```
pip install -r requirements.txt
cd listener && npm install          # only for the live listener (Node 20+)
```

Optional: put `ANTHROPIC_API_KEY=...` in a `.env` file for AI extraction. Without it, the basic rules are used.

## Use

### 1. From a chat export

1. In the group: ⋮ → More → **Export chat** → **Include media**, and copy the .zip to the computer.
2. `python -m watchbot senders export.zip`: see the dealer's name as WhatsApp shows it.
3. `python -m watchbot ingest export.zip --seller "Dealer Name" --dry-run`: preview the grouping.
4. `python -m watchbot ingest export.zip --seller "Dealer Name"`: save.

### 2. Live from the group (Baileys)

⚠️ Unofficial: WhatsApp can ban the linked number. Use a spare SIM, never your main WhatsApp.

1. Add a spare number to the dealer's group.
2. `cd listener && node index.mjs`. On the spare phone, go to WhatsApp → Settings → Linked devices → Link a device and scan the QR code. It then lists your groups.
3. `node index.mjs --group "<group name>"`. The group is remembered from then on.
4. Every day: run `start-bot.bat` (Windows). It starts a watchdog that runs the listener and the saver in one window and restarts either if it stops (or if the listener stops checking in for 5 minutes). Settings live in `.env` (copy `.env.example`): `WATCHBOT_OUT` for the folder, `WATCHBOT_SELLER` to save only the dealer's posts. A log is kept in `<WATCHBOT_OUT>/logs/`.

Posts are saved 3 minutes after the dealer stops posting. Open WhatsApp on the spare phone every ~14 days so the link doesn't expire. `listener/auth/` is the login session: keep it private.

### Uploading to the website (WooCommerce)

Each new watch is sent to you on WhatsApp first; nothing goes on the site until you say yes.

1. WordPress admin → Users → Profile → **Application Passwords** → name it `watchbot` → Add. Copy `.env.example` to `.env` and fill in `WC_URL`, `WP_USER` and `WP_APP_PASSWORD`. Check it with `python -m watchbot upload --check`.
2. Set your markup in `.env` (`MARKUP_PERCENT`, `MARKUP_FLAT`, `PRICE_ROUND`).
3. In `.env` set `WATCHBOT_UPLOAD=on`, and `WATCHBOT_APPROVAL` to `self` (the spare number's own "Message yourself" chat) or the name of a group you share with the spare number.
4. For every new watch you get the cover photo, title, price and details. **Reply** to it:
   - `yes`: upload it now, at the price shown
   - `yes 4500`: upload at ₹4,500
   - `no`: skip it
5. Approved watches are published with square photos (same size, location data removed), title, price, brand category, a spec list and stock of 1, and you get the link back. When the dealer marks a watch sold, it's set out of stock on the site; when the dealer changes a price, the site follows (unless you set that watch's price yourself with `yes <price>`, then you're just told).

`python -m watchbot upload --dry-run` shows what would be sent without sending or uploading anything. Upload progress is kept in `<OUT>/uploads.json`.

### 3. Forwarding bot (official Meta Cloud API)

1. developers.facebook.com → Create app (Business) → add **WhatsApp**. API Setup gives a free test number, a Phone number ID and a token; add your own number as a recipient.
2. Copy `.env.example` to `.env` and fill it in.
3. `ngrok http --url=<your-static-domain> 8000`, then `python -m watchbot serve`.
4. In the Meta app, go to WhatsApp → Configuration → Webhook, set it to `https://<domain>/webhook` with your verify token, and subscribe to `messages`.
5. Message **hi** to the test number. Commands: `undo`, `count`, `help`.

## Output

| Path | What |
|---|---|
| `output/products/<date>-<title>/01.jpg …` | photos in the order posted |
| `output/products/<date>-<title>/product.json` | extracted fields, original text, follow-ups, change history |
| `output/catalog.csv` | one row per watch, opens in Excel |
| `output/catalog.json` | the same data, for a store uploader |
| `output/state.json` | photo fingerprints for duplicate checks |
| `output/products/<…>/web/` | square, resized photos made for the website |
| `output/uploads.json` | which watches were previewed, approved, uploaded or skipped |

## Project layout

```
watchbot/
  parse_export.py   WhatsApp export (.zip/.txt) → messages
  grouping.py       messages → one group per watch
  extract.py        listing text → structured fields (Claude + regex fallback)
  store.py          product folders, catalog, duplicate fingerprints, history
  pipeline.py       one group → added / updated / duplicate (shared by all inputs)
  watch.py          live mode: processes what the listener saves
  uploader.py       WhatsApp preview → "yes" → WooCommerce product
  watchdog.py       runs and restarts the listener, saver and uploader
  bot.py            Meta Cloud API forwarding bot (Flask webhook)
  whatsapp.py       minimal Cloud API client
listener/
  index.mjs         Baileys linked-device listener (also sends previews to the approval chat)
```

## Next

- Use the photos to help split groups that the text alone can't separate.

## A note on use

Get the dealer's permission before republishing their photos and descriptions. Baileys is not affiliated with or endorsed by WhatsApp, and automating a personal account is against WhatsApp's terms. The official forwarding bot is the safe route.
