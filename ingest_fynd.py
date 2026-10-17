#!/usr/bin/env python3
"""
Send the cleaned catalog (out/catalog_products.json) to a Fynd Commerce
Platform company through the Platform Catalog API.

Default is a DRY RUN: it builds the payloads, writes them to
out/payload_preview.json, and sends nothing. Add --live to really send.

Every response is saved under out/api_responses/ so you have evidence of what
the platform said. Progress is saved after each product (out/ingest_state.json),
so if the run stops, run it again and it carries on where it left off.

Settings come from environment variables (see config.example.env):
  FYND_COMPANY_ID, FYND_BRAND_UID, FYND_DEPARTMENTS (one id), FYND_TAX_RULE_ID,
  FYND_CATEGORY_SLUG (default: others), FYND_TEMPLATE_TAG (default: supplementary)
Sign-in, either of:
  FYND_CLIENT_ID + FYND_CLIENT_SECRET   the script fetches a token itself and
                                        fetches a new one if it expires mid-run
  FYND_ACCESS_TOKEN a token you generated yourself (lasts ~1 hour)
Optional:
  FYND_HOST (default https://api.fynd.com), FYND_CREATE_PATH, FYND_TOKEN_SCOPE,
  FYND_COUNTRY_OF_ORIGIN, FYND_RETURNABLE, FYND_RETURN_DAYS,
  FYND_TRADER_NAME, FYND_TRADER_TYPE, FYND_TRADER_ADDRESS, FYND_ATTRIBUTES_JSON

Useful commands:
  --check-auth     test the sign-in without touching the catalog
  --get PATH       call any Platform API GET path with your token and print the answer,
                   e.g. to look up department, category, brand and tax rule ids.
                   {company_id} in the path is filled in for you.

The create-product call is Fynd's createProduct: POST
/service/platform/catalog/v3.0/company/{company_id}/products/ (ProductCreateSchemaV3).
Note that v2.0 on the same address is the LIST endpoint, so posting there fails.
The top-level required fields in build_payload() follow Fynd's catalog reference.
The inner fields of each size are my best reading and are not confirmed: if Fynd
rejects them, the message is saved in out/api_responses/ and build_payload() is the
only place that needs to change.
"""
import argparse
import base64
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_HOST = "https://console.fynd.com/"
# createProduct in Fynd's Platform catalog reference. (v2.0 on this address is "List products".)
DEFAULT_CREATE_PATH = "commerce/asia-south1/accounts/17411/products/list"

MAX_RETRIES = 5


def load_config():
    cfg = {
        "host": os.getenv("FYND_HOST", DEFAULT_HOST).rstrip("/"),
        "create_path": os.getenv("FYND_CREATE_PATH", DEFAULT_CREATE_PATH),
        "company_id": os.getenv("FYND_COMPANY_ID", ""),
        "token": os.getenv("FYND_ACCESS_TOKEN", ""),
        "brand_uid": os.getenv("FYND_BRAND_UID", ""),
        "departments": [int(x) for x in os.getenv("FYND_DEPARTMENTS", "").split(",") if x.strip()],
        "category_slug": os.getenv("FYND_CATEGORY_SLUG", "others"),
        "template_tag": os.getenv("FYND_TEMPLATE_TAG", "supplementary"),
        "client_id": os.getenv("FYND_CLIENT_ID", ""),
        "client_secret": os.getenv("FYND_CLIENT_SECRET", ""),
        "token_scope": [x.strip() for x in os.getenv("FYND_TOKEN_SCOPE", "").split(",") if x.strip()],
        "tax_rule_id": os.getenv("FYND_TAX_RULE_ID", ""),
        "country_of_origin": os.getenv("FYND_COUNTRY_OF_ORIGIN", "India"),
        "returnable": os.getenv("FYND_RETURNABLE", "true").strip().lower() in ("1", "true", "yes"),
        "return_days": int(os.getenv("FYND_RETURN_DAYS", "7")),
        "trader_name": os.getenv("FYND_TRADER_NAME", ""),
        "trader_type": os.getenv("FYND_TRADER_TYPE", "Manufacturer"),
        "trader_address": os.getenv("FYND_TRADER_ADDRESS", "India"),
        "attributes": json.loads(os.getenv("FYND_ATTRIBUTES_JSON", "") or "{}"),
    }
    return cfg


def can_fetch_token(cfg):
    return bool(cfg["client_id"] and cfg["client_secret"] and cfg["company_id"])


def fetch_token(cfg):
    """
    Swap the client ID and secret for a short-lived access token (about an hour).
    Returns (token, seconds_until_expiry). The token itself is never printed.

    The request follows Fynd's "Platform REST API" page (client credentials).
    If Fynd rejects it, the error is printed in full: compare it with that page.
    """
    if not can_fetch_token(cfg):
        sys.exit("To fetch a token I need FYND_COMPANY_ID, FYND_CLIENT_ID and FYND_CLIENT_SECRET.")
    url = f"{cfg['host']}/service/panel/authentication/v1.0/company/{cfg['company_id']}/oauth/token"
    basic = base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
    payload = {"grant_type": "client_credentials"}      # exactly as in Fynd's docs
    if cfg["token_scope"]:
        payload["scope"] = cfg["token_scope"]            # only if you set FYND_TOKEN_SCOPE
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Basic {basic}"})
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as err:
        sys.exit(f"Could not get a token (HTTP {err.code}): {err.read().decode('utf-8', 'replace')[:400]}\n"
                 "Check the client ID, secret, company ID and scope, and compare the request with Fynd's "
                 "Platform REST API page for client credentials.")
    except urllib.error.URLError as err:
        sys.exit(f"Could not reach Fynd to get a token: {err.reason}")
    token = data.get("access_token")
    if not token:
        sys.exit("Fynd answered but did not return an access_token. Check the scope and the request format.")
    return token, data.get("expires_in")


def build_payload(product, cfg):
    """
    Turn one cleaned product into a createProduct (V3) request body.
    Required by Fynd at the top level: brand_uid, category_slug, company_id,
    country_of_origin, currency, departments (exactly one), item_code, item_type,
    name, return_config, sizes, slug, tax_identifier (tax_rule_id), template_tag,
    trader, and hs_code for Indian companies.
    """
    sizes = [{
        "size": s["size"],
        "price": s["mrp"],                       # MRP
        "price_effective": s["price_effective"],  # selling price
        "currency": "INR",
        "identifiers": [{
            "gtin_type": "sku_code",
            "gtin_value": s["sku"],
            "primary": True,
        }],
    } for s in product["sizes"]]
    payload = {
        "name": product["name"],
        "slug": product["slug"],
        "item_code": product["item_code"],
        "company_id": int(cfg["company_id"]) if cfg["company_id"] else None,
        "brand_uid": int(cfg["brand_uid"]) if cfg["brand_uid"] else None,
        "category_slug": cfg["category_slug"],
        "departments": cfg["departments"][:1],
        "template_tag": cfg["template_tag"],
        "item_type": "standard",
        "currency": "INR",
        "country_of_origin": cfg["country_of_origin"],
        "description": product["description"],
        "hs_code": product["hsn_code"],
        "tax_identifier": {"tax_rule_id": cfg["tax_rule_id"]},
        "return_config": {"returnable": cfg["returnable"], "time": cfg["return_days"], "unit": "days"},
        "trader": [{
            "name": cfg["trader_name"] or product.get("brand") or "Meridian Apparel",
            "type": cfg["trader_type"],
            "address": [cfg["trader_address"]],
        }],
        "multi_size": len(sizes) > 1,
        "sizes": sizes,
    }
    if product.get("image"):
        payload["media"] = [{"type": "image", "url": product["image"]}]
    else:
        payload["is_image_less_product"] = True      # a bad or missing image link still loads the product
    if cfg["attributes"]:
        payload["attributes"] = cfg["attributes"]
    return {k: v for k, v in payload.items() if v not in (None, "")}


def request_json(method, url, auth, body=None, refresh=None):
    """
    Call the API with retries on rate limits (429) and server errors (5xx).
    If the token has expired (401) and a refresh function is given, get a new one and try again.
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    refreshed = False
    for attempt in range(1, MAX_RETRIES + 1):
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {auth['token']}"})
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            text = err.read().decode("utf-8", "replace")
            if err.code == 401 and refresh and not refreshed:
                print("    token rejected, fetching a new one")
                auth["token"], _ = refresh()
                refreshed = True
                continue
            if err.code == 429 or err.code >= 500:
                wait = int(err.headers.get("Retry-After", 2 ** attempt))
                print(f"    {err.code} from platform, waiting {wait}s (attempt {attempt}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            return err.code, text
        except urllib.error.URLError as err:
            wait = 2 ** attempt
            print(f"    network problem ({err.reason}), waiting {wait}s (attempt {attempt}/{MAX_RETRIES})")
            time.sleep(wait)
    return 0, "gave up after retries"


def post_json(url, auth, body, refresh=None):
    return request_json("POST", url, auth, body, refresh)


def get_auth(cfg):
    """Returns (auth dict holding the token, refresh function or None)."""
    refresh = (lambda: fetch_token(cfg)) if can_fetch_token(cfg) else None
    if refresh:
        token, expires = refresh()
        print(f"Got a token for company {cfg['company_id']}"
              + (f", valid for about {int(expires) // 60} minutes." if expires else "."))
    elif cfg["token"]:
        token = cfg["token"]
        print("Using FYND_ACCESS_TOKEN. It lasts about an hour, so generate a fresh one if you see 401 errors.")
    else:
        sys.exit("No sign-in settings. Set FYND_CLIENT_ID and FYND_CLIENT_SECRET (or FYND_ACCESS_TOKEN).")
    return {"token": token}, refresh


def short_message(text):
    """Pull the platform's message out of an error reply so it can be shown in plain view."""
    try:
        data = json.loads(text)
        for key in ("message", "error", "errors", "detail"):
            if data.get(key):
                return str(data[key])[:400]
    except (ValueError, AttributeError):
        pass
    return text[:400].replace("\n", " ")


def main():
    parser = argparse.ArgumentParser(description="Send cleaned products to the Fynd Commerce Platform.")
    parser.add_argument("--input", default="out/catalog_products.json")
    parser.add_argument("--out", default="out")
    parser.add_argument("--live", action="store_true", help="really send to Fynd (default is a dry run)")
    parser.add_argument("--delay", type=float, default=0.7, help="seconds between requests")
    parser.add_argument("--limit", type=int, default=0, help="only send the first N products (for a trial)")
    parser.add_argument("--check-auth", action="store_true", help="only test the sign-in, send nothing")
    parser.add_argument("--get", metavar="PATH", help="call a Platform API GET path and print the answer")
    args = parser.parse_args()

    cfg = load_config()

    if args.check_auth:
        if can_fetch_token(cfg):
            _, expires = fetch_token(cfg)
            print(f"Sign-in works. Got a token for company {cfg['company_id']}"
                  + (f", valid for about {int(expires) // 60} minutes." if expires else "."))
        elif cfg["token"]:
            print("FYND_ACCESS_TOKEN is set, but I can only test a token by using it. "
                  "Set FYND_CLIENT_ID and FYND_CLIENT_SECRET to test the sign-in, or run with --live --limit 1.")
        else:
            sys.exit("Nothing to test. Set FYND_CLIENT_ID and FYND_CLIENT_SECRET (or FYND_ACCESS_TOKEN).")
        return

    if args.get:
        auth, refresh = get_auth(cfg)
        path = args.get.replace("{company_id}", cfg["company_id"])
        status, text = request_json("GET", cfg["host"] + path, auth, None, refresh)
        print(f"HTTP {status}")
        try:
            print(json.dumps(json.loads(text), indent=2, ensure_ascii=False)[:6000])
        except ValueError:
            print(text[:3000])
        return

    products = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if args.limit:
        products = products[:args.limit]
    out_dir = Path(args.out)
    responses_dir = out_dir / "api_responses"
    responses_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / "ingest_state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    payloads = [(p["item_code"], build_payload(p, cfg)) for p in products]

    if not args.live:
        preview = out_dir / "payload_preview.json"
        preview.write_text(json.dumps([p for _, p in payloads], indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"DRY RUN. Built {len(payloads)} payloads, nothing sent. Preview: {preview}")
        print("Add --live to send them to Fynd.")
        return

    missing = [k for k in ("company_id", "brand_uid", "tax_rule_id") if not cfg[k]]
    if not cfg["departments"]:
        missing.append("departments")
    if not cfg["token"] and not can_fetch_token(cfg):
        missing.append("FYND_ACCESS_TOKEN (or FYND_CLIENT_ID and FYND_CLIENT_SECRET)")
    if missing:
        sys.exit("Missing settings: " + ", ".join(missing) + ". See config.example.env.")
    if len(cfg["departments"]) > 1:
        print("Note: Fynd takes exactly one department per product, so only the first one is used.")

    auth, refresh = get_auth(cfg)

    url = cfg["host"] + cfg["create_path"].format(company_id=cfg["company_id"])
    log_rows, created, failed, skipped = [], 0, 0, 0
    for index, (item_code, payload) in enumerate(payloads, start=1):
        if state.get(item_code) == "created":
            skipped += 1
            continue
        print(f"[{index}/{len(payloads)}] {item_code}")
        status, text = post_json(url, auth, payload, refresh)
        (responses_dir / f"{item_code}.json").write_text(
            json.dumps({"http_status": status, "request": payload, "response": text}, indent=2, ensure_ascii=False),
            encoding="utf-8")
        ok = 200 <= status < 300
        state[item_code] = "created" if ok else f"failed ({status})"
        state_path.write_text(json.dumps(state, indent=2))
        created += ok
        failed += not ok
        if not ok:
            print(f"    FAILED (HTTP {status}): {short_message(text)}")
        log_rows.append([item_code, status, "created" if ok else "failed", text[:300].replace("\n", " ")])
        if failed >= 3 and created == 0:
            print("Stopping: the first 3 products all failed, so something in the settings or request is wrong. "
                  "Read the message above and out/api_responses/, fix it, and run the same command again.")
            break
        time.sleep(args.delay)

    with open(out_dir / "ingest_log.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["item_code", "http_status", "result", "response_start"])
        writer.writerows(log_rows)
    print(f"Done. Created {created}, failed {failed}, skipped (already done) {skipped}.")
    print("Responses are in out/api_responses/ and out/ingest_log.csv. Screenshot the products on the platform too.")


if __name__ == "__main__":
    main()
