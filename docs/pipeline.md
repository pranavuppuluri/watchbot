# How watchbot works, in diagrams

1. [The whole system](#1-the-whole-system)
2. [From messages to a product](#2-from-messages-to-a-product)
3. [Duplicate checks](#3-duplicate-checks)
4. [Approval and upload](#4-approval-and-upload)
5. [Life of an upload](#5-life-of-an-upload)
6. [The watchdog](#6-the-watchdog)

All the processes talk through plain files in the output folder (`WATCHBOT_OUT`), never directly to each other. That means any one of them can crash or restart without losing anything: each one keeps its own read position.

## 1. The whole system

```mermaid
flowchart TB
    G(["Dealer's WhatsApp group"])
    A(["Your approval chat<br/>(Message yourself)"])

    subgraph WD["python -m watchbot run (watchdog keeps all three running)"]
        direction LR
        L["listener<br/>listener/index.mjs<br/>(Baileys)"]
        S["saver<br/>watch.py"]
        U["uploader<br/>uploader.py"]
    end

    W[("WooCommerce shop")]

    G -- "posts, photos, replies" --> L
    A <-- "previews out,<br/>yes / no back" --> L
    L -- "_inbox/messages.jsonl<br/>+ photos" --> S
    S -- "products/ + catalog.json" --> U
    U -- "_outbox/ (previews to send)" --> L
    L -. "_inbox/approvals.jsonl<br/>(your replies)" .-> U
    U -- "REST API: photos, product,<br/>stock, price" --> W
```

Files the processes share: `_inbox/` (messages, photos, approvals, heartbeat), `products/` and `catalog.json`/`catalog.csv`/`state.json` (the saved watches), `_outbox/` (messages waiting to be sent), and `uploads.json` (what's on the shop).

The other two inputs skip the listener and go straight to the same pipeline: `python -m watchbot ingest export.zip` for a chat export, and `python -m watchbot serve` for the forwarding bot on the official Cloud API.

## 2. From messages to a product

```mermaid
flowchart TD
    M["New lines in messages.jsonl"] --> F{"From the seller?<br/>(WATCHBOT_SELLER)"}
    F -- no --> X1["ignored"]
    F -- yes --> R{"A reply to an earlier<br/>post we saved?"}
    R -- "yes: sold / hold /<br/>available / new price" --> UPD["update that product<br/>+ add to its history"]
    R -- no --> P["hold in memory per sender"]
    P --> Q{"3 min with no<br/>new posts?"}
    Q -- no --> P
    Q -- yes --> GR["grouping.py<br/>split the burst into watches<br/>(details after or before photos)"]
    GR --> D{"duplicate of a<br/>saved watch?"}
    D -- "yes, same price/status" --> X2["skipped"]
    D -- "yes, new price or sold" --> UPD
    D -- no --> EX["extract.py<br/>Claude with a schema,<br/>or regex fallback"]
    EX --> ST["store.py<br/>products/&lt;date&gt;-&lt;title&gt;/<br/>01.jpg… product.json source.txt"]
    ST --> C["catalog.json + catalog.csv"]
    GR -. "unclear grouping" .-> NR["flagged needs_review"]
```

## 3. Duplicate checks

Duplicates are stopped at three points, from cheapest to most thorough.

```mermaid
flowchart TD
    subgraph L1["1 · Listener: per message"]
        A1{"Message id<br/>already saved?"} -- yes --> S1["skip"]
        A1 -- no --> A2{"Same file hash<br/>(WhatsApp's fileSha256)<br/>already downloaded?"}
        A2 -- yes --> S2["reuse that file,<br/>don't download again"]
        A2 -- no --> S3["download photo"]
    end

    subgraph L2["2 · Store: per watch"]
        B1["each photo: exact match (SHA1),<br/>else visual fingerprint (dHash)<br/>within 4 of 64 bits"] --> B2{"More than half the<br/>photos belong to one<br/>saved watch?"}
        B2 -- yes --> T1["same watch:<br/>update price/status or skip"]
        B2 -- no --> T2["new watch"]
        T2 --> B3["same photo twice<br/>in this post? keep one"]
    end

    subgraph L3["3 · Uploader: per product"]
        C1{"SKU already<br/>on the shop?"} -- yes --> U1["link to it,<br/>don't create again"]
        C1 -- no --> U2["upload, keeping media ids<br/>so a retry doesn't re-upload photos"]
    end

    S2 --> B1
    S3 --> B1
    B3 --> C1
```

The visual fingerprint catches photos that WhatsApp recompressed or resized when the dealer re-posted them. The "more than half" rule still catches a re-post with one photo swapped, but a new listing that only shares a stock shot (box, papers) isn't mistaken for an old one.

## 4. Approval and upload

```mermaid
sequenceDiagram
    autonumber
    participant D as Dealer (group)
    participant L as Listener
    participant S as Saver
    participant U as Uploader
    participant Y as You (approval chat)
    participant W as WooCommerce

    D->>L: photos + "Seiko 6309, ₹4,000"
    L->>S: _inbox/messages.jsonl
    Note over S: waits 3 min of quiet
    S->>U: new row in catalog.json
    U->>L: _outbox/preview.json
    L->>Y: cover photo, title, price, details
    Y->>L: reply "yes 4500"
    L->>U: _inbox/approvals.jsonl
    U->>W: square photos, then product (stock 1)
    W-->>U: product link
    U->>L: _outbox "✅ Uploaded" + link
    L->>Y: confirmation with link preview

    D->>L: reply to own post: "sold"
    L->>S: messages.jsonl
    S->>U: catalog: status = sold
    U->>W: stock 0 (out of stock)
    U->>Y: "marked sold on the website"
```

## 5. Life of an upload

The states kept in `uploads.json`:

```mermaid
stateDiagram-v2
    [*] --> previewed: new watch in catalog<br/>(not sold)
    previewed --> approved: "yes" (brand, category,<br/>price all known)
    previewed --> previewed: brand, category or price missing<br/>→ asked for them (typos and case forgiven)
    previewed --> skipped: "no"
    skipped --> approved: "yes" later
    approved --> approved: upload failed<br/>→ retry with backoff (up to 1 h)
    approved --> uploaded: published on the shop
    uploaded --> uploaded: dealer says sold / available<br/>→ stock 0 / 1
    uploaded --> uploaded: dealer changes price<br/>→ shop follows, unless you set it
```

A reply can carry details in any order and spelling (`yes brand omga catagory swis 4.5k`). Details sent without a "yes" are noted, and the watch waits for the "yes"; a "yes" sent while something is still missing is remembered, so the upload starts as soon as the last missing detail arrives.

Website price = dealer price × (1 + `MARKUP_PERCENT`/100) + `MARKUP_FLAT`, rounded up to `PRICE_ROUND`. If you approved with your own price (`yes 4500`), later dealer price changes are reported to you instead of applied.

## 6. The watchdog

```mermaid
flowchart TD
    START["start-bot.bat<br/>python -m watchbot run"] --> ENV["read .env"]
    ENV --> RUN["start listener, saver,<br/>and uploader (if WATCHBOT_UPLOAD=on)"]
    RUN --> LOOP{"every few seconds"}
    LOOP --> E{"a process exited?"}
    E -- "listener, exit 3<br/>(unlinked from phone)" --> STOP["stop restarting it:<br/>delete listener/auth and re-link"]
    E -- other --> BACK["restart after 5 s,<br/>doubling up to 5 min"]
    E -- no --> HB{"listener heartbeat<br/>older than 5 min?"}
    HB -- yes --> RESTART["restart listener"]
    HB -- no --> LOOP
    BACK --> LOOP
    RESTART --> LOOP
    LOOP -. "everything" .-> LOG["logs/watchdog-&lt;date&gt;.log"]
```

A process that has stayed up for 10 minutes has its restart delay reset to 5 seconds. There's no heartbeat check while the listener is showing a QR code.
