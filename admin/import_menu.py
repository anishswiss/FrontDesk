#!/usr/bin/env python3
"""
import_menu.py - load a restaurant's menu into the bot's `menu_items` table.

  scan     Read a menu from ANY mix of: web page URL, PDF (file or URL), photo/screenshot
           (jpg/png/webp), or a text file. An AI model reads it and returns the items.
  csv      Load a spreadsheet saved as CSV (columns: category,name,description,price,available)
  export   Write the menu saved in the database to CSV (edit it, then load it back with `csv`)
  show     Print the menu text the bot will see on a call

Examples:
  python import_menu.py scan https://example.com/menu --dry-run --export review.csv
  python import_menu.py scan page1.jpg page2.jpg menu.pdf --restaurant +14695551234
  python import_menu.py csv menu.csv --restaurant +14695551234
  python import_menu.py export --restaurant +14695551234 --out menu.csv
  python import_menu.py show --restaurant +14695551234

`scan` needs OPENAI_API_KEY or ANTHROPIC_API_KEY in .env (or MENU_LLM_PROVIDER to choose).
An AI can misread a price or skip an item, so it never saves without asking you first,
and you should always check the result with the restaurant.
"""
import argparse
import base64
import csv
import json
import mimetypes
import os
import re
import sys
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html.parser import HTMLParser

# Make `business/` importable whether this file sits in the project root or a subfolder like admin/
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

DEFAULT_MODELS = {"openai": "gpt-4.1-mini", "anthropic": "claude-haiku-5-5"}
MAX_TEXT_CHARS = 60_000
IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif"}

PROMPT = """You are reading a restaurant menu (web page text, a PDF, or a photo of a printed menu).
Extract every item a customer can order. Return ONLY JSON, with no commentary:

{"items": [{"category": "...", "name": "...", "description": "...", "price": 12.99, "confidence": "high"}]}

Rules:
- Copy names and prices exactly as printed. Never invent items, prices or descriptions.
- "price" is a plain number in dollars (no currency symbol). If an item has several sizes or
  options with different prices, output one item per size, e.g. name "Chicken Biryani - Large".
- If an item's price is missing or unclear, set price to null and confidence to "low".
- Use confidence "low" when text is blurry, cut off, or you are unsure of a name or price.
- "category" is the section heading the item is under (e.g. "Appetizers"); use "" if none.
- "description" is optional: one short line copied from the menu, or "".
- Include drinks, sides and add-ons as items too.
- Ignore opening hours, addresses, promotions, and anything that is not an orderable item.
- If this source has no menu items, return {"items": []}."""


# ===========================================================================
# Loading sources: URL, PDF, image, text
# ===========================================================================


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "template"}
    BLOCK = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6",
             "section", "article", "ul", "ol", "table"}

    def __init__(self):
        super().__init__()
        self.parts, self.jsonld, self.skip, self.in_ld = [], [], 0, False

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type") == "application/ld+json":
            self.in_ld = True
            return
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "script" and self.in_ld:
            self.in_ld = False
            return
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_ld:
            self.jsonld.append(data)
        elif not self.skip and data.strip():
            self.parts.append(data.strip() + " ")


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    text = re.sub(r"[ \t]+\n", "\n", "".join(p.parts))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if p.jsonld:  # structured data some sites embed (often includes the menu)
        text += "\n\nSTRUCTURED DATA:\n" + "\n".join(p.jsonld)
    return text


def load_source(src: str) -> dict:
    """Return {'kind': 'text'|'image'|'pdf', 'label', 'text' or 'data'+'media_type'}."""
    if re.match(r"^https?://", src):
        import requests

        r = requests.get(src, headers={"User-Agent": "Mozilla/5.0 (compatible; menu-import/1.0)"},
                         timeout=30)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if "pdf" in ctype or src.lower().split("?")[0].endswith(".pdf"):
            return {"kind": "pdf", "label": src, "data": r.content}
        if ctype.startswith("image/"):
            return {"kind": "image", "label": src, "data": r.content, "media_type": ctype}
        text = html_to_text(r.text)
        if len(text) < 300:
            print(f"WARNING: {src} has very little text. The menu may load with JavaScript or "
                  "sit inside an image. Try a screenshot or PDF of the menu instead.")
        return {"kind": "text", "label": src, "text": text}

    if not os.path.exists(src):
        sys.exit(f"File not found: {src}")
    ext = os.path.splitext(src)[1].lower()
    raw = open(src, "rb").read()
    if ext == ".pdf":
        return {"kind": "pdf", "label": src, "data": raw}
    if ext in IMAGE_TYPES:
        return {"kind": "image", "label": src, "data": raw, "media_type": IMAGE_TYPES[ext]}
    text = raw.decode("utf-8", errors="replace")
    if ext in (".html", ".htm"):
        text = html_to_text(text)
    return {"kind": "text", "label": src, "text": text}


# ===========================================================================
# Calling the AI model (plain HTTPS, no extra SDKs)
# ===========================================================================


def pick_provider(arg):
    prov = arg or os.getenv("MENU_LLM_PROVIDER")
    if not prov:
        if os.getenv("OPENAI_API_KEY"):
            prov = "openai"
        elif os.getenv("ANTHROPIC_API_KEY"):
            prov = "anthropic"
        else:
            sys.exit("Set OPENAI_API_KEY or ANTHROPIC_API_KEY in .env to use `scan`.")
    if prov not in DEFAULT_MODELS:
        sys.exit(f"Unknown provider {prov!r}; use openai or anthropic.")
    key = os.getenv(f"{prov.upper()}_API_KEY")
    if not key:
        sys.exit(f"{prov.upper()}_API_KEY is not set.")
    return prov, key


def ask_model(provider, key, model, source):
    import requests

    b64 = base64.b64encode(source["data"]).decode() if "data" in source else None
    kind = source["kind"]

    if provider == "openai":
        content = [{"type": "text", "text": PROMPT}]
        if kind == "text":
            content.append({"type": "text", "text": source["text"][:MAX_TEXT_CHARS]})
        elif kind == "image":
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{source['media_type']};base64,{b64}"}})
        else:
            content.append({"type": "file", "file": {
                "filename": os.path.basename(source["label"]) or "menu.pdf",
                "file_data": f"data:application/pdf;base64,{b64}"}})
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "temperature": 0,
                  "response_format": {"type": "json_object"},
                  "messages": [{"role": "user", "content": content}]},
            timeout=180,
        )
        if r.status_code != 200:
            sys.exit(f"OpenAI error {r.status_code}: {r.text[:500]}")
        choice = r.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            print("WARNING: the answer was cut off. Split the menu into smaller pieces.")
        return choice["message"]["content"]

    # anthropic
    content = []
    if kind == "text":
        content.append({"type": "text", "text": source["text"][:MAX_TEXT_CHARS]})
    elif kind == "image":
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": source["media_type"], "data": b64}})
    else:
        content.append({"type": "document", "source": {
            "type": "base64", "media_type": "application/pdf", "data": b64}})
    content.append({"type": "text", "text": PROMPT})
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        json={"model": model, "max_tokens": 8000,
              "messages": [{"role": "user", "content": content}]},
        timeout=180,
    )
    if r.status_code != 200:
        sys.exit(f"Anthropic error {r.status_code}: {r.text[:500]}")
    data = r.json()
    if data.get("stop_reason") == "max_tokens":
        print("WARNING: the answer was cut off. Split the menu into smaller pieces.")
    return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")


def parse_items(reply: str):
    text = re.sub(r"^```(?:json)?|```$", "", reply.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("model did not return JSON")
    data = json.loads(text[start:end + 1])
    return data.get("items", [])


def normalize(raw_items):
    items, skipped = [], []
    for it in raw_items:
        name = str(it.get("name") or "").strip()
        if not name:
            continue
        price = it.get("price")
        try:
            cents = int((Decimal(str(price).replace("$", "").strip()) * 100)
                        .to_integral_value(ROUND_HALF_UP))
            if cents < 0:
                raise ValueError
        except (InvalidOperation, ValueError, TypeError):
            skipped.append(name)
            continue
        items.append({
            "category": str(it.get("category") or "").strip(),
            "name": name,
            "description": str(it.get("description") or "").strip()[:150],
            "price_cents": cents,
            "available": None,
            "check": str(it.get("confidence", "high")).lower() == "low",
        })
    return items, skipped


def scan_sources(args):
    provider, key = pick_provider(args.provider)
    model = args.model or os.getenv("MENU_LLM_MODEL") or DEFAULT_MODELS[provider]
    print(f"Using {provider} / {model}")
    merged, skipped_all = {}, []
    for src in args.sources:
        print(f"\nReading {src} ...")
        source = load_source(src)
        reply = ask_model(provider, key, model, source)
        try:
            raw = parse_items(reply)
        except (ValueError, json.JSONDecodeError) as e:
            print(f"  Could not read the model's answer ({e}). Skipping this source.")
            continue
        items, skipped = normalize(raw)
        skipped_all += skipped
        print(f"  {len(items)} items found" + (f", {len(skipped)} without a price" if skipped else ""))
        for it in items:
            k = (it["category"].lower(), it["name"].lower())
            if k in merged and merged[k]["price_cents"] != it["price_cents"]:
                print(f"  Note: '{it['name']}' appears twice with different prices; keeping the first.")
            merged.setdefault(k, it)
    if skipped_all:
        print("\nNo price found for (not saved, add these by hand in a CSV):")
        for n in dict.fromkeys(skipped_all):
            print("  -", n)
    return list(merged.values())


# ===========================================================================
# CSV
# ===========================================================================

TRUE = {"1", "y", "yes", "true", "t", "available"}
FALSE = {"0", "n", "no", "false", "f", "sold out", "soldout", "unavailable"}


def price_to_cents(text):
    cleaned = re.sub(r"[^\d.]", "", str(text))
    try:
        return int((Decimal(cleaned) * 100).to_integral_value(ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        raise ValueError(f"bad price {text!r}")


def csv_items(path):
    items = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for n, row in enumerate(csv.DictReader(f), start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            if not row.get("name"):
                print(f"Skipped line {n}: missing name")
                continue
            try:
                cents = price_to_cents(row.get("price", ""))
            except ValueError as e:
                print(f"Skipped line {n} ({row['name']}): {e}")
                continue
            avail = row.get("available", "").lower()
            items.append({"category": row.get("category", ""), "name": row["name"],
                          "description": row.get("description", ""), "price_cents": cents,
                          "available": True if avail in TRUE else False if avail in FALSE else None})
    return items


def write_csv(path, items):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["category", "name", "description", "price", "available"])
        for it in items:
            avail = "" if it.get("available") is None else int(bool(it["available"]))
            w.writerow([it.get("category", ""), it["name"], it.get("description", ""),
                        f"{it['price_cents'] / 100:.2f}", avail])
    print(f"Wrote {len(items)} items to {path}")


# ===========================================================================
# Shared steps and command line
# ===========================================================================


def preview(items, limit=60):
    flagged = sum(1 for i in items if i.get("check"))
    print(f"\n{len(items)} items" + (f" ({flagged} marked ? = AI was unsure, check these)" if flagged else "") + ":")
    for it in items[:limit]:
        mark = "?" if it.get("check") else " "
        print(f" {mark}[{it['category'] or 'Menu'}] {it['name']} - ${it['price_cents'] / 100:.2f}")
    if len(items) > limit:
        print(f"  ... and {len(items) - limit} more (use --export to see all)")


def get_restaurant_or_exit(number):
    from business.orders import get_restaurant

    rest = get_restaurant(number)
    if not rest:
        sys.exit(f"No restaurant with inbound number {number}. Check the number (+1...).")
    return rest


def finish(items, args):
    if not items:
        sys.exit("No items found.")
    preview(items)
    if args.export:
        write_csv(args.export, items)
    if args.dry_run or not args.restaurant:
        print("\nNothing saved." + ("" if args.restaurant else " Add --restaurant +1XXXXXXXXXX to save."))
        return
    rest = get_restaurant_or_exit(args.restaurant)
    if not args.yes:
        if not sys.stdin.isatty():
            sys.exit("Not saving: run in a terminal to confirm, or pass --yes.")
        if input(f"\nSave {len(items)} items for {rest['name']}? [y/N] ").strip().lower() != "y":
            print("Cancelled. Nothing saved.")
            return
    from business.orders import save_menu_items

    result = save_menu_items(rest["id"], items, deactivate_missing=args.deactivate_missing)
    print(f"Saved for {rest['name']}: {result}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--restaurant", help="restaurant inbound number, e.g. +14695551234")
        sp.add_argument("--dry-run", action="store_true", help="preview only, save nothing")
        sp.add_argument("--export", help="also write the items to this CSV for review")
        sp.add_argument("--yes", action="store_true", help="save without asking")
        sp.add_argument("--deactivate-missing", action="store_true",
                        help="hide existing items that are not in this import")

    s = sub.add_parser("scan", help="read a menu from URLs, PDFs, photos or text files")
    s.add_argument("sources", nargs="+", help="URL(s) and/or file path(s)")
    s.add_argument("--provider", choices=list(DEFAULT_MODELS), help="openai or anthropic")
    s.add_argument("--model", help="override the default model")
    common(s)

    c = sub.add_parser("csv", help="import from a CSV file")
    c.add_argument("path")
    common(c)

    e = sub.add_parser("export", help="export the saved menu to CSV")
    e.add_argument("--restaurant", required=True)
    e.add_argument("--out", default="menu.csv")

    sh = sub.add_parser("show", help="print the menu text the bot will see")
    sh.add_argument("--restaurant", required=True)

    args = ap.parse_args()
    if args.cmd == "scan":
        finish(scan_sources(args), args)
    elif args.cmd == "csv":
        finish(csv_items(args.path), args)
    elif args.cmd == "export":
        from business.orders import get_menu_rows

        rest = get_restaurant_or_exit(args.restaurant)
        write_csv(args.out, [{"category": r["category"], "name": r["name"],
                              "description": r["description"], "price_cents": r["price_cents"],
                              "available": bool(r["available"])} for r in get_menu_rows(rest["id"])])
    elif args.cmd == "show":
        from business.orders import get_menu_text

        rest = get_restaurant_or_exit(args.restaurant)
        print(get_menu_text(rest["id"]) or "(no menu items saved)")


if __name__ == "__main__":
    main()