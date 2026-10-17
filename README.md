# Meridian Apparel: item master migration to Fynd

Part A of the Fynd Technical Implementation Manager case study.

Meridian's legacy POS and ERP only export flat files, and the data in them is messy. This project cleans one export, checks every row, writes a report a merchant can read, and sends the good products to a Fynd Commerce Platform demo company.

## How it works

```
data/meridian_legacy_item_master.json
        |  transformer.py   (clean, split sizes and colours, check each row)
        v
out/catalog_products.json   out/clean_items.csv   out/validation_report.html / .csv
        |  ingest_fynd.py   (send to the Fynd Platform Catalog API)
        v
Products on the Fynd demo company + out/api_responses/ as evidence
```

## How to run it

Python 3.9 or newer. No packages to install.

```bash
# 1. Clean and check the file (nothing is sent anywhere)
python3 transformer.py
# open out/validation_report.html

# 2. Check the tests pass
python3 -m unittest discover -s tests

# 3. Dry run of the Fynd upload (builds the requests, sends nothing)
python3 ingest_fynd.py

# 4. Real upload. Copy config.example.env to .env and fill it in first (see "Signing in to Fynd").
set -a; source .env; set +a
python3 ingest_fynd.py --check-auth         # tests the sign-in, sends nothing
python3 ingest_fynd.py --live --limit 1     # try one product first
python3 ingest_fynd.py --live               # then the rest
```

To use your own export, run `python3 transformer.py --input yourfile.json` (a `.csv` also works).

## Signing in to Fynd

The Platform API needs an access token, and a token lasts only about an hour. The script gets and renews it for you.

1. In the Fynd Commerce panel for your demo company, go to Company, then Developers, then Clients, and click **Create Client**. Give it the catalog permissions.
2. Copy the **Client ID** and **Client Secret** into your `.env` file as `FYND_CLIENT_ID` and `FYND_CLIENT_SECRET`, with your `FYND_COMPANY_ID`.
3. Run `python3 ingest_fynd.py --check-auth`. It swaps the client ID and secret for a token and tells you how long it lasts. Nothing is sent to the catalog.
4. During a live run, if Fynd answers 401 because the token has run out, the script gets a new one and carries on with the same product.

The request follows Fynd's own "Authentication" page for Platform APIs (client credentials: a Basic header made from the client ID and secret, and `grant_type` set to `client_credentials`). If you would rather paste a token yourself, set `FYND_ACCESS_TOKEN` and leave the client ID and secret empty. The script cannot renew a pasted token. The client secret and any token are never printed or saved, and `.env` is in `.gitignore`.

## Settings you need to find first

Fynd needs real ids from your own company before it will accept a product. Put each one in `.env`.

| Setting | What it is | How to find it |
|---|---|---|
| `FYND_COMPANY_ID` | Your demo company | The number in the panel address after `/company/` |
| `FYND_BRAND_UID` | A brand on your company | In the panel under Catalog, Brands. Create one if there is none |
| `FYND_DEPARTMENTS` | The department that "Others" sits under. Fynd takes exactly one | `python3 ingest_fynd.py --get "/service/platform/catalog/v1.0/company/{company_id}/departments/"` |
| `FYND_TAX_RULE_ID` | A tax rule that already exists on the company | In the panel's tax settings, or the Taxation section of Fynd's Platform REST API reference. Create one if needed |
| `FYND_CATEGORY_SLUG` | Defaults to `others` | Check the exact slug with `--get ".../category/?q=others"` |
| `FYND_TEMPLATE_TAG` | Defaults to `supplementary` | Check the exact slug in the product templates list in the panel |

The `--get` command signs in for you, calls any Platform API address you give it, and prints the answer. `{company_id}` is filled in automatically.

## If the upload fails

- **HTTP 405 or 404:** the create address is wrong. The default is `POST /service/platform/catalog/v3.0/company/{company_id}/products/`. The `v2.0` address is the list endpoint and cannot create products.
- **HTTP 400:** the message says which required field is missing or wrong. It is printed on screen and saved in `out/api_responses/`. Copy it and fix that setting, or the matching line in `build_payload()`.
- **HTTP 401 or 403:** the sign-in or the client's permissions. Run `--check-auth`, and make sure the client was created in the Commerce panel with catalog access.
- If the first 3 products all fail, the script stops by itself so it does not keep sending bad requests.

## The sample file

`data/meridian_legacy_item_master.json` has 14 records, made up to be realistic and messy:

| Row | What is wrong | What the transformer does |
|---|---|---|
| 1 to 7, 14 | Different field names and casing ("Item Name", "name", "title"), sizes and colours in one field with different separators, prices like "Rs. 1,499/-", SKU in lower case with spaces | Maps the field names, splits into items, cleans prices and SKU. **Loaded** |
| 5 | Duplicate SKU (same as row 1, different price) | **Rejected**. The first one is kept and the report says so |
| 8 | Product name missing | **Rejected** |
| 9 | MRP and selling price both blank | **Rejected**. Both missing fields are listed |
| 10 | Malformed: a loose line of text, not a record | **Rejected** |
| 11 | Broken image link | **Loaded with warning**, without a picture |
| 12 | HSN code "6204XYZ" is not a number | **Rejected** |
| 13 | Selling price is higher than MRP | **Rejected** |

A GST rate that is not a standard slab (for example "118%") is also rejected. That case is covered by the unit tests rather than a row in the sample file.

Result on the sample: 7 rows loaded cleanly, 1 with a warning, 6 rejected. The 8 good rows become 14 products and 45 sellable items.

## Mapping: legacy field to Fynd attribute

| Legacy field (any of these names) | Cleaned field | Fynd attribute |
|---|---|---|
| SKU, Item Code | style SKU | Used to build `item_code` and the size-level identifier |
| Item Name, name, title | name | `name` (colour added if the style has several colours) |
| Brand, Brand Name | brand | Brand is set once from `FYND_BRAND_UID` for the demo company |
| Size, sizes (split on `/ , ; \|`) | one entry per size | `sizes[].size` |
| Colour, Color, colours (split on `, ; \|`) | one product per colour | One Fynd product per style and colour |
| MRP | mrp | `sizes[].price` |
| Selling Price, Sale Price, price | price_effective | `sizes[].price_effective` |
| HSN, HSN Code | hsn_code | `tax_identifier.hsn_code` |
| GST, GST % | gst_rate | Checked, but tax rules are set on the platform |
| Image, image_url | image | `media[]` |
| Description, desc | description | `description` |

Item codes are built as `STYLE-COLOUR` (for example `MER-TS-001-RED`) and the size-level code adds the size (`MER-TS-001-RED-M`). All catalog items go under the **Others** category with the **Supplementary** template.

## Rules I applied

- Mandatory: SKU, name, MRP, selling price, HSN and GST rate. A missing one rejects the row.
- SKU: letters, numbers and hyphens only (Fynd asks for no spaces or special characters).
- HSN: digits only, 4, 6 or 8 long.
- GST: one of 0, 3, 5, 12, 18, 28.
- Selling price must not be higher than MRP.
- A bad or missing image link does not stop the product. It loads without a picture and the report says so.
- If the same SKU appears twice, the first good one wins. The later row is rejected with a note.

## Assumptions

- Each size and colour is one sellable item. I group them into one Fynd product per style and colour, with the sizes inside it.
- A slash splits sizes but not colours, because "Black/White" is usually one two-tone colour.
- Brand, department and category are set once for the demo company, not read from each row.
- A missing size or colour is loaded as "Free Size" or "Default" with a warning.
- Rows are checked in file order. A row only counts as a duplicate if the same SKU was already loaded.

## Known limitations

- I do not check whether an image link actually opens, only that it is well formed. Hosting is not covered.
- GST is checked against the standard slabs, not against the price band or the HSN code.
- HSN codes are checked for shape only. Fynd may also require the HSN to exist on the platform first.
- The upload creates one product per request, which is fine for this demo. For 80,000 items I would use a bulk route and the batch design described in Part B.
- The sign-in and renewal logic is tested against a local stand-in server (`tests/test_ingest_auth.py`), not against the real Fynd platform, because I could not reach it when building this.
- The top-level fields in `build_payload()` follow Fynd's createProduct (V3) required list. The inner fields of each size (price, price_effective, identifiers) are my best reading and are not confirmed, so check the first live response.
- Stock quantities are not part of the catalog load.

