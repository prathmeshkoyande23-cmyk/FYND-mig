#!/usr/bin/env python3
"""
Meridian legacy item master -> clean catalog + validation report.

Reads the legacy export (JSON or CSV), cleans it, splits sizes and colours into
individual sellable items, checks every row, and writes:

  out/clean_items.csv          one line per sellable item (size + colour)
  out/catalog_products.json    items grouped into products, ready for ingest_fynd.py
  out/validation_report.csv    one line per legacy row, plain English
  out/validation_report.html   the same report, easier for a merchant to read

No row is ever dropped silently. Every row ends up as Loaded, Loaded with
warning, or Rejected, and the report says why.

Python 3.9+, standard library only.
"""
import argparse
import csv
import html
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

# --------------------------------------------------------------------------
# 1. Field names. Legacy files call the same thing by many names.
#    Keys are compared in lower case with spaces and underscores treated alike.
# --------------------------------------------------------------------------
FIELD_ALIASES = {
    "sku": ["sku", "item code", "article code"],
    "name": ["item name", "name", "title", "product name"],
    "brand": ["brand", "brand name"],
    "sizes": ["size", "sizes"],
    "colours": ["colour", "colours", "color", "colors"],
    "mrp": ["mrp", "max retail price"],
    "price": ["selling price", "sale price", "price", "sp"],
    "hsn": ["hsn", "hsn code"],
    "gst": ["gst", "gst %", "gst%", "tax", "tax %"],
    "image": ["image", "image url", "img"],
    "description": ["description", "desc"],
}

# Fields that must have a value or the row is rejected.
MANDATORY = ["sku", "name", "mrp", "price", "hsn", "gst"]

FIELD_LABELS = {
    "sku": "SKU", "name": "product name", "mrp": "MRP",
    "price": "selling price", "hsn": "HSN code", "gst": "GST rate",
}

VALID_GST_RATES = {0, 3, 5, 12, 18, 28}
VALID_HSN_LENGTHS = {4, 6, 8}
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
FREE_SIZE_WORDS = {"free size", "freesize", "one size", "onesize", "free", "os"}
LETTER_SIZES = {"XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL"}


def norm_key(key):
    return re.sub(r"[\s_]+", " ", str(key).strip().lower())


ALIAS_LOOKUP = {norm_key(a): field for field, names in FIELD_ALIASES.items() for a in names}


def clean_text(value):
    """Trim and squeeze spaces. None becomes an empty string."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


# --------------------------------------------------------------------------
# 2. Cleaning helpers
# --------------------------------------------------------------------------
def map_fields(row):
    """Turn a legacy row with any field names into one with our standard names."""
    out = {field: "" for field in FIELD_ALIASES}
    for key, value in row.items():
        field = ALIAS_LOOKUP.get(norm_key(key))
        if field and not out[field]:
            out[field] = value
    return out


def parse_price(raw):
    """'Rs. 1,499/-' -> 1499.0. Returns None if blank. Raises ValueError if unreadable."""
    text = clean_text(raw)
    if not text:
        return None
    text = re.sub(r"(?i)(inr|rs\.?|rupees?|₹|/-)", "", text)
    text = text.replace(",", "").strip()
    if not re.fullmatch(r"\d+(\.\d{1,2})?", text):
        raise ValueError(f"'{clean_text(raw)}' is not a readable price")
    return round(float(text), 2)


def split_values(raw, separators):
    """'S/M/L' -> ['S', 'M', 'L'], keeping order and removing repeats."""
    parts = re.split(separators, clean_text(raw))
    seen, result = set(), []
    for part in parts:
        part = clean_text(part)
        if part and part.lower() not in seen:
            seen.add(part.lower())
            result.append(part)
    return result


def normalise_size(size):
    upper = size.upper()
    if size.lower() in FREE_SIZE_WORDS:
        return "Free Size"
    if upper in LETTER_SIZES:
        return upper
    return size


def normalise_colour(colour):
    return colour.title()


def code_part(text):
    """Letters and digits only, upper case. Fynd item codes should have no spaces or symbols."""
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def valid_image_url(url):
    if not url or re.search(r"\s", url):
        return False
    parsed = urlparse(url)
    return (parsed.scheme in ("http", "https") and bool(parsed.netloc)
            and parsed.path.lower().endswith(IMAGE_EXTENSIONS))


# --------------------------------------------------------------------------
# 3. Validate and transform one row
# --------------------------------------------------------------------------
class Issue:
    def __init__(self, severity, message, fix):
        self.severity = severity  # "error" stops the row, "warning" does not
        self.message = message
        self.fix = fix


def process_row(raw_row, accepted_skus):
    """
    Returns (summary_dict, products_list).
    summary_dict has sku, name, issues. products_list is empty if the row is rejected.
    """
    issues = []

    # Malformed: not a record at all (for example a loose line of text)
    if not isinstance(raw_row, dict):
        snippet = clean_text(raw_row)[:60]
        issues.append(Issue(
            "error",
            f"This line could not be read as a product record. It looks like loose text: \"{snippet}\".",
            "Re-export this product from the ERP, or type it in as a proper row."))
        return {"sku": "", "name": "", "issues": issues}, []

    row = map_fields(raw_row)
    sku = clean_text(row["sku"]).upper()
    name = clean_text(row["name"])
    brand = clean_text(row["brand"])
    description = clean_text(row["description"])
    summary = {"sku": sku, "name": name, "issues": issues}

    # Mandatory fields
    for field in MANDATORY:
        if not clean_text(row[field]):
            issues.append(Issue(
                "error", f"The {FIELD_LABELS[field]} is missing.",
                f"Fill in the {FIELD_LABELS[field]} for this product and send it again."))

    # SKU format and duplicates
    if sku:
        if not re.fullmatch(r"[A-Z0-9-]+", sku):
            issues.append(Issue(
                "error", f"SKU '{sku}' has spaces or symbols. Fynd needs letters, numbers and hyphens only.",
                "Correct the SKU in the ERP."))
        elif sku in accepted_skus:
            issues.append(Issue(
                "error", f"SKU {sku} already appears earlier in this file. The first one was kept.",
                "Check which row is right. If this is a price change, update the original row instead."))

    # Prices
    mrp = price = None
    try:
        mrp = parse_price(row["mrp"])
    except ValueError as err:
        issues.append(Issue("error", f"MRP: {err}.", "Correct the MRP so it is a plain number."))
    try:
        price = parse_price(row["price"])
    except ValueError as err:
        issues.append(Issue("error", f"Selling price: {err}.", "Correct the selling price so it is a plain number."))
    if mrp is not None and price is not None:
        if mrp <= 0 or price <= 0:
            issues.append(Issue("error", "MRP and selling price must be above zero.", "Correct the prices."))
        elif price > mrp:
            issues.append(Issue(
                "error", f"Selling price ({price:g}) is higher than MRP ({mrp:g}).",
                "Check both prices. A product cannot be sold above its MRP."))

    # HSN
    hsn = clean_text(row["hsn"])
    if hsn and not (hsn.isdigit() and len(hsn) in VALID_HSN_LENGTHS):
        issues.append(Issue(
            "error", f"HSN code '{hsn}' does not look right. It should be 4, 6 or 8 digits.",
            "Look up the correct HSN code for this product and update it."))

    # GST
    gst = None
    gst_text = clean_text(row["gst"]).replace("%", "").strip()
    if gst_text:
        try:
            gst = float(gst_text)
            if gst not in VALID_GST_RATES:
                raise ValueError
            gst = int(gst) if gst == int(gst) else gst
        except ValueError:
            gst = None
            issues.append(Issue(
                "error", f"GST rate '{clean_text(row['gst'])}' is not a standard rate (0, 3, 5, 12, 18 or 28).",
                "Correct the GST rate."))

    # Sizes and colours. If blank, use a default and warn.
    sizes = [normalise_size(s) for s in split_values(row["sizes"], r"[,;|/]")]
    if not sizes:
        sizes = ["Free Size"]
        issues.append(Issue("warning", "No size given, so it was loaded as 'Free Size'.", "Add the sizes if there are any."))
    colours = [normalise_colour(c) for c in split_values(row["colours"], r"[,;|]")]
    if not colours:
        colours = ["Default"]
        issues.append(Issue("warning", "No colour given, so it was loaded as 'Default'.", "Add the colour if there is one."))

    # Image: a broken link does not stop the product, but we say so
    image = clean_text(row["image"])
    if not image:
        issues.append(Issue("warning", "No image link. The product will load without a picture.", "Add an image link."))
    elif not valid_image_url(image):
        issues.append(Issue(
            "warning", f"The image link does not work: \"{image}\". The product will load without a picture.",
            "Send a working image link (https, ending in .jpg, .png or .webp)."))
        image = ""

    if any(i.severity == "error" for i in issues):
        return summary, []

    # Build one product per colour. Each product holds its sizes.
    accepted_skus.add(sku)
    products = []
    for colour in colours:
        item_code = f"{sku}-{code_part(colour)}"
        products.append({
            "item_code": item_code,
            "style_sku": sku,
            "name": f"{name.title()} - {colour}" if len(colours) > 1 else name.title(),
            "slug": slugify(f"{name} {colour} {sku}"),
            "brand": brand,
            "colour": colour,
            "description": description,
            "hsn_code": hsn,
            "gst_rate": gst,
            "image": image,
            "sizes": [{
                "size": size,
                "sku": f"{item_code}-{code_part(size)}",
                "mrp": mrp,
                "price_effective": price,
            } for size in sizes],
        })
    summary["name"] = name.title()
    return summary, products


# --------------------------------------------------------------------------
# 4. Read the file and run everything
# --------------------------------------------------------------------------
def load_rows(path):
    suffix = path.suffix.lower()
    if suffix == ".json":
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            sys.exit("The JSON file must contain a list of products at the top level.")
        return data
    if suffix == ".csv":
        with open(path, encoding="utf-8-sig", newline="") as fh:
            return list(csv.DictReader(fh))
    sys.exit(f"Unsupported file type '{suffix}'. Use .json or .csv.")


def transform(rows):
    accepted_skus = set()
    report, all_products = [], []
    for number, raw in enumerate(rows, start=1):
        summary, products = process_row(raw, accepted_skus)
        errors = [i for i in summary["issues"] if i.severity == "error"]
        warnings = [i for i in summary["issues"] if i.severity == "warning"]
        if errors:
            status = "Rejected"
        elif warnings:
            status = "Loaded with warning"
        else:
            status = "Loaded"
        items = sum(len(p["sizes"]) for p in products)
        report.append({
            "row": number,
            "sku": summary["sku"],
            "product": summary["name"],
            "status": status,
            "sellable_items": items,
            "problems": " | ".join(i.message for i in summary["issues"]),
            "what_to_do": " | ".join(i.fix for i in summary["issues"]),
        })
        all_products.extend(products)
    return report, all_products


def write_outputs(report, products, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)

    # Clean items, one line per sellable item
    with open(out_dir / "clean_items.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["item_sku", "product_code", "name", "brand", "size", "colour",
                         "mrp", "selling_price", "hsn", "gst_rate", "image_url", "description"])
        for p in products:
            for s in p["sizes"]:
                writer.writerow([s["sku"], p["item_code"], p["name"], p["brand"], s["size"], p["colour"],
                                 s["mrp"], s["price_effective"], p["hsn_code"], p["gst_rate"],
                                 p["image"], p["description"]])

    with open(out_dir / "catalog_products.json", "w", encoding="utf-8") as fh:
        json.dump(products, fh, indent=2, ensure_ascii=False)

    columns = ["row", "sku", "product", "status", "sellable_items", "problems", "what_to_do"]
    with open(out_dir / "validation_report.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        writer.writerows(report)

    write_html_report(report, out_dir / "validation_report.html")


def counts(report):
    return {
        "rows": len(report),
        "loaded": sum(r["status"] == "Loaded" for r in report),
        "warning": sum(r["status"] == "Loaded with warning" for r in report),
        "rejected": sum(r["status"] == "Rejected" for r in report),
        "items": sum(r["sellable_items"] for r in report),
    }


def write_html_report(report, path):
    c = counts(report)
    colours = {"Loaded": "#e6f4ea", "Loaded with warning": "#fff4e0", "Rejected": "#fce8e6"}
    rows_html = []
    for r in report:
        problem = html.escape(r["problems"]).replace(" | ", "<br>") or "Nothing to fix."
        fix = html.escape(r["what_to_do"]).replace(" | ", "<br>") or "-"
        rows_html.append(
            f"<tr style='background:{colours[r['status']]}'>"
            f"<td>{r['row']}</td><td>{html.escape(r['sku'])}</td><td>{html.escape(r['product'])}</td>"
            f"<td><b>{r['status']}</b></td><td>{r['sellable_items']}</td><td>{problem}</td><td>{fix}</td></tr>")
    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Meridian item master: check report</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 32px; color: #222; }}
table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
th, td {{ border: 1px solid #ccc; padding: 8px; text-align: left; vertical-align: top; }}
th {{ background: #333; color: #fff; }}
</style></head><body>
<h2>Meridian item master: check report</h2>
<p>We read <b>{c['rows']}</b> lines from your file.
<b>{c['loaded']}</b> loaded cleanly, <b>{c['warning']}</b> loaded with a warning,
and <b>{c['rejected']}</b> were not loaded and need your attention.
In total <b>{c['items']}</b> sellable items (each size and colour) are ready to go onto Fynd.</p>
<p><b>How to read this:</b> green rows are fine. Orange rows were loaded, but something small is
worth fixing. Red rows were not loaded. The last column says what to change. Fix them in your
ERP and send the file again.</p>
<table>
<tr><th>Row</th><th>SKU</th><th>Product</th><th>Result</th><th>Items</th><th>What we found</th><th>What to do</th></tr>
{''.join(rows_html)}
</table></body></html>"""
    path.write_text(page, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Clean and check a legacy item master export.")
    parser.add_argument("--input", default="data/meridian_legacy_item_master.json")
    parser.add_argument("--out", default="out")
    args = parser.parse_args()

    rows = load_rows(Path(args.input))
    report, products = transform(rows)
    write_outputs(report, products, Path(args.out))

    c = counts(report)
    print(f"Read {c['rows']} rows from {args.input}")
    print(f"  Loaded cleanly:        {c['loaded']}")
    print(f"  Loaded with warning:   {c['warning']}")
    print(f"  Rejected:              {c['rejected']}")
    print(f"  Sellable items ready:  {c['items']} in {len(products)} products")
    print(f"Reports written to {args.out}/ (open validation_report.html)")
    for r in report:
        if r["status"] != "Loaded":
            print(f"  row {r['row']:>2} {r['status']:<20} {r['sku'] or '-':<12} {r['problems']}")


if __name__ == "__main__":
    main()
