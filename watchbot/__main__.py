"""watchbot: WhatsApp group export -> one folder per watch + catalog.csv

  python -m watchbot senders  <export.zip>
  python -m watchbot ingest   <export.zip> [--seller "Ravi Watches"] [--dry-run]
  python -m watchbot watch    [--seller "Ravi"]  # live, with listener/index.mjs running
  python -m watchbot upload   [--once] [--dry-run]  # preview on WhatsApp, upload to WooCommerce on "yes"
  python -m watchbot run      --out <folder> [--upload]  # watchdog: listener + saver (+ uploader), auto-restart
  python -m watchbot serve    [--port 8000]      # alternative: Meta Cloud API bot, configured in .env
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from .extract import ai_available
from .grouping import group_products
from .parse_export import parse_export
from .pipeline import process_group
from .store import Store


def cmd_senders(args) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        export = parse_export(Path(args.export), Path(tmp))
        media_by = {}
        for m in export.messages:
            if m.kind in ("image", "video"):
                media_by[m.sender] = media_by.get(m.sender, 0) + 1
        print(f"{'sender':40} {'messages':>9} {'photos':>7}")
        for s, n in sorted(export.senders.items(), key=lambda kv: -kv[1]):
            print(f"{s[:40]:40} {n:>9} {media_by.get(s, 0):>7}")


def cmd_ingest(args) -> None:
    out = Path(args.out)
    with tempfile.TemporaryDirectory() as tmp:
        export = parse_export(Path(args.export), Path(tmp))
        print(f"Read {len(export.messages)} messages from {len(export.senders)} people")
        if export.missing_media:
            print(f"  ! {export.missing_media} photos are not in the export. Re-export with 'Include media'.")

        sellers = set(args.seller) if args.seller else None
        if sellers is None:
            # Default to whoever posted the most photos: in a dealer group that's the seller.
            counts: dict[str, int] = {}
            for m in export.messages:
                if m.kind in ("image", "video"):
                    counts[m.sender] = counts.get(m.sender, 0) + 1
            if counts:
                top = max(counts, key=counts.get)
                sellers = {top}
                print(f"  Seller (most photos): {top}. Use --seller to change.")
        unknown = (sellers or set()) - set(export.senders)
        if unknown:
            sys.exit(f"Not found in chat: {', '.join(unknown)}. Run `python -m watchbot senders` to see names.")

        groups, layout = group_products(export.messages, sellers, args.gap, args.layout)
        print(f"  Found {len(groups)} products (text {'after' if layout == 'after' else 'before'} photos)")

        use_ai = not args.no_ai and ai_available()
        if not args.no_ai and not use_ai:
            print("  ! No Anthropic API key found; using basic rules. Set ANTHROPIC_API_KEY for better results.")

        store = Store(out)
        added = skipped = review = 0
        for i, g in enumerate(groups, 1):
            o = process_group(store, g, use_ai, args.vision, args.dry_run)
            added += o.action == "added"
            skipped += o.action in ("updated", "duplicate")
            review += o.action == "added" and bool(o.needs_review)
            print(f"  [{i}/{len(groups)}] {o.line()}")

        if not args.dry_run:
            store.save()
            print(f"\nAdded {added}, skipped {skipped} duplicates, {review} flagged for review.")
            print(f"Catalog: {(out / 'catalog.csv').resolve()}")


def cmd_watch(args) -> None:
    from .bot import load_env
    from .watch import Watcher
    load_env(Path.cwd() / ".env")  # picks up ANTHROPIC_API_KEY if it's kept there
    Watcher(Path(args.out), args.seller, args.quiet, args.layout, args.vision).run()


def cmd_upload(args) -> None:
    from .bot import load_env
    from .uploader import Uploader
    load_env(Path.cwd() / ".env")
    up = Uploader(Path(args.out), dry_run=args.dry_run)
    if args.check:
        print(f"Connected to {up.woo.base} as {up.woo.check()}")
    elif args.once or args.dry_run:
        up.tick()
    else:
        up.run()


def cmd_run(args) -> None:
    from .bot import load_env
    from .watchdog import Watchdog
    load_env(Path.cwd() / ".env")
    upload = args.upload or os.environ.get("WATCHBOT_UPLOAD", "").lower() in ("1", "on", "yes", "true")
    out = args.out or os.environ.get("WATCHBOT_OUT") or "output"
    sellers = args.seller or [s.strip() for s in os.environ.get("WATCHBOT_SELLER", "").split(",") if s.strip()]
    Watchdog(Path(out), sellers, upload).run()


def cmd_serve(args) -> None:
    from .bot import create_app  # Flask is only needed for the bot
    app = create_app()
    print(f"  Webhook listening on http://localhost:{args.port}/webhook (expose it with ngrok)")
    app.run(host="0.0.0.0", port=args.port, threaded=True)


def main() -> None:
    p = argparse.ArgumentParser(prog="watchbot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("senders", help="list who posted in the chat")
    s.add_argument("export", help="exported chat .zip or unzipped folder")
    s.set_defaults(func=cmd_senders)

    i = sub.add_parser("ingest", help="turn an export into product folders + catalog")
    i.add_argument("export", help="exported chat .zip or unzipped folder")
    i.add_argument("--out", default="output", help="output folder (default: ./output)")
    i.add_argument("--seller", action="append", help="only use this sender's posts (repeatable)")
    i.add_argument("--layout", choices=["auto", "after", "before"], default="auto",
                   help="does the seller write details after or before the photos (default: detect)")
    i.add_argument("--gap", type=int, default=15, help="minutes of silence that end a posting burst (default 15)")
    i.add_argument("--no-ai", action="store_true", help="don't call Claude; use basic rules only")
    i.add_argument("--vision", type=int, default=0, metavar="N",
                   help="also send the first N photos to Claude (reads dial, bracelet; costs more)")
    i.add_argument("--dry-run", action="store_true", help="show the grouping without writing anything")
    i.set_defaults(func=cmd_ingest)

    lw = sub.add_parser("watch", help="save posts live from the Baileys listener (listener/index.mjs)")
    lw.add_argument("--out", default="output", help="output folder (default: ./output)")
    lw.add_argument("--seller", action="append", help="only this sender: name or number (repeatable)")
    lw.add_argument("--quiet", type=int, default=180, help="seconds without new posts before saving (default 180)")
    lw.add_argument("--layout", choices=["auto", "after", "before"], default="auto")
    lw.add_argument("--vision", type=int, default=0, metavar="N")
    lw.set_defaults(func=cmd_watch)

    u = sub.add_parser("upload", help="send previews to WhatsApp and upload approved watches to WooCommerce")
    u.add_argument("--out", default="output", help="output folder (default: ./output)")
    u.add_argument("--once", action="store_true", help="one pass, then exit")
    u.add_argument("--dry-run", action="store_true", help="prepare photos and show previews; send and upload nothing")
    u.add_argument("--check", action="store_true", help="test the WooCommerce login, then exit")
    u.set_defaults(func=cmd_upload)

    r = sub.add_parser("run", help="watchdog: run listener + saver (+ uploader) and restart them if they stop")
    r.add_argument("--out", help="output folder (default: WATCHBOT_OUT in .env, else ./output)")
    r.add_argument("--seller", action="append", help="only this sender: name or number (repeatable; or WATCHBOT_SELLER)")
    r.add_argument("--upload", action="store_true", help="also run the uploader (or set WATCHBOT_UPLOAD=on in .env)")
    r.set_defaults(func=cmd_run)

    w = sub.add_parser("serve", help="(alternative) official Meta Cloud API bot you forward posts to; see .env.example")
    w.add_argument("--port", type=int, default=8000)
    w.set_defaults(func=cmd_serve)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
