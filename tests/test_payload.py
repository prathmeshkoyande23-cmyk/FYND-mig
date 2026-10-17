"""Checks the create-product request has every top-level field Fynd's createProduct (V3) requires."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ingest_fynd as ing

REQUIRED = ["brand_uid", "category_slug", "company_id", "country_of_origin", "currency", "departments",
            "item_code", "item_type", "name", "return_config", "sizes", "slug", "tax_identifier",
            "template_tag", "trader", "hs_code"]

CFG = {"company_id": "1234", "brand_uid": "77", "departments": [21, 22], "tax_rule_id": "rule-1",
       "category_slug": "others", "template_tag": "supplementary", "country_of_origin": "India",
       "returnable": True, "return_days": 7, "trader_name": "", "trader_type": "Manufacturer",
       "trader_address": "India", "attributes": {}}

PRODUCT = {"item_code": "MER-TS-001-RED", "name": "Classic Crew Neck T-Shirt - Red", "slug": "classic-red",
           "brand": "Meridian", "description": "Tee", "hsn_code": "61091000", "image": "https://x.in/a.jpg",
           "sizes": [{"size": "S", "sku": "MER-TS-001-RED-S", "mrp": 1299.0, "price_effective": 999.0},
                     {"size": "M", "sku": "MER-TS-001-RED-M", "mrp": 1299.0, "price_effective": 999.0}]}


class PayloadTests(unittest.TestCase):
    def test_create_path_is_the_v3_create_endpoint(self):
        self.assertIn("/catalog/v3.0/", ing.DEFAULT_CREATE_PATH)

    def test_all_required_fields_present(self):
        payload = ing.build_payload(PRODUCT, CFG)
        for field in REQUIRED:
            self.assertIn(field, payload, field)

    def test_exactly_one_department_and_numeric_ids(self):
        payload = ing.build_payload(PRODUCT, CFG)
        self.assertEqual(payload["departments"], [21])
        self.assertEqual(payload["brand_uid"], 77)
        self.assertEqual(payload["company_id"], 1234)

    def test_tax_rule_and_hs_code(self):
        payload = ing.build_payload(PRODUCT, CFG)
        self.assertEqual(payload["tax_identifier"], {"tax_rule_id": "rule-1"})
        self.assertEqual(payload["hs_code"], "61091000")

    def test_missing_image_marks_product_image_less(self):
        payload = ing.build_payload(dict(PRODUCT, image=""), CFG)
        self.assertNotIn("media", payload)
        self.assertTrue(payload["is_image_less_product"])

    def test_item_code_and_slug_follow_fynd_patterns(self):
        import re
        payload = ing.build_payload(PRODUCT, CFG)
        self.assertRegex(payload["item_code"], r"^[A-Za-z0-9-_@,#\(\)\. ]+$")
        self.assertRegex(payload["slug"], r"^[a-z0-9-_]*$")

    def test_real_catalog_builds(self):
        path = Path(__file__).resolve().parent.parent / "out" / "catalog_products.json"
        for product in json.loads(path.read_text(encoding="utf-8")):
            payload = ing.build_payload(product, CFG)
            for field in REQUIRED:
                self.assertIn(field, payload, f"{product['item_code']}: {field}")


if __name__ == "__main__":
    unittest.main()
