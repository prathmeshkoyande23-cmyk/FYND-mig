import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import transformer as t


def run(row, seen=None):
    return t.process_row(row, seen if seen is not None else set())


def errors(summary):
    return [i.message for i in summary["issues"] if i.severity == "error"]


GOOD = {"SKU": "A-1", "Item Name": "Tee", "Size": "S/M", "Colour": "Red, Blue", "MRP": "₹1,000",
        "Selling Price": "₹900", "HSN": "6109", "GST %": "5", "Image": "https://x.in/a.jpg"}


class TransformerTests(unittest.TestCase):
    def test_prices(self):
        self.assertEqual(t.parse_price("Rs. 1,499/-"), 1499.0)
        self.assertEqual(t.parse_price("INR 2,499.00"), 2499.0)
        self.assertEqual(t.parse_price("₹ 699"), 699.0)
        self.assertIsNone(t.parse_price(""))
        with self.assertRaises(ValueError):
            t.parse_price("free")

    def test_variants_split(self):
        summary, products = run(GOOD)
        self.assertEqual(errors(summary), [])
        self.assertEqual(len(products), 2)                           # two colours
        self.assertEqual(sum(len(p["sizes"]) for p in products), 4)  # 2 sizes each

    def test_field_names_and_casing(self):
        row = {"item_code": " a-2 ", "TITLE": "Cap", "sizes": "Free Size", "colours": "black", "mrp": "500",
               "price": "400", "hsn code": "6505", "gst": "12%"}
        summary, products = run(row)
        self.assertEqual(errors(summary), [])
        self.assertEqual(products[0]["style_sku"], "A-2")

    def test_duplicate_sku(self):
        seen = set()
        run(GOOD, seen)
        summary, products = run(GOOD, seen)
        self.assertTrue(any("already appears" in e for e in errors(summary)))
        self.assertEqual(products, [])

    def test_missing_fields_all_reported(self):
        summary, _ = run({"SKU": "A-3", "Item Name": "Tee", "HSN": "6109", "GST": "5"})
        text = " ".join(errors(summary))
        self.assertIn("MRP", text)
        self.assertIn("selling price", text)

    def test_malformed_row(self):
        summary, products = run("this is not a record")
        self.assertEqual(products, [])
        self.assertTrue(errors(summary))

    def test_bad_image_is_warning_only(self):
        summary, products = run(dict(GOOD, Image="htp:/bad link.jpg"))
        self.assertEqual(errors(summary), [])
        self.assertTrue(any(i.severity == "warning" for i in summary["issues"]))
        self.assertEqual(products[0]["image"], "")

    def test_bad_hsn_and_gst(self):
        self.assertTrue(errors(run(dict(GOOD, HSN="61A9"))[0]))
        self.assertTrue(errors(run(dict(GOOD, **{"GST %": "118%"}))[0]))

    def test_price_above_mrp(self):
        self.assertTrue(errors(run(dict(GOOD, **{"Selling Price": "₹1,200"}))[0]))


if __name__ == "__main__":
    unittest.main()
