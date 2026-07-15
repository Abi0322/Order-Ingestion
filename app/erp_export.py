"""
erp_export.py — Maps confirmed orders onto an ERP-style purchase order schema.

This doesn't integrate with a live ERP system (that would need a specific
vendor's API and credentials) — it demonstrates the schema-mapping step that
would sit in front of one: normalized, structured data with the fields a
real ERP import (e.g. NetSuite, SAP, Dynamics purchase-order imports) would
expect, generated from the same confirmed order data the Inbox already has.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Any, Dict, List

# Mock SKU codes standing in for a real product-master lookup.
SKU_MAP = {
    "Insulin Pen": "SKU-1001",
    "Surgical Gloves": "SKU-1002",
    "N95 Mask": "SKU-1003",
    "ECG Electrode": "SKU-1004",
    "Sterile Syringe": "SKU-1005",
    "Oxygen Cylinder": "SKU-1006",
    "Bandage": "SKU-1007",
    "Paracetamol Tablet": "SKU-1008",
    "Digital Thermometer": "SKU-1009",
    "IV Drip": "SKU-1010",
    "PPE Kit": "SKU-1011",
    "Face Shield": "SKU-1012",
}


def _po_number(order_id: int) -> str:
    return f"PO-{order_id:06d}"


def order_to_erp_record(order: Dict[str, Any]) -> Dict[str, Any]:
    """Map one order (as returned by storage.list_orders) to an ERP-shaped record."""
    from pipeline import PRICE_LIST

    line_items = []
    for idx, item in enumerate(order["items"], start=1):
        unit_price = PRICE_LIST.get(item["product"], 0.0)
        line_items.append(
            {
                "line_number": idx,
                "sku": SKU_MAP.get(item["product"], "SKU-UNKNOWN"),
                "description": item["product"],
                "quantity": item["quantity"],
                "unit_of_measure": item["unit"] or "each",
                "unit_price": unit_price,
                "line_total": round(unit_price * item["quantity"], 2),
            }
        )

    return {
        "po_number": _po_number(order["id"]),
        "vendor_reference": order["source"],
        "customer_name": order["customer"],
        "requested_delivery_date": order["delivery_date"],
        "currency": "USD",
        "line_items": line_items,
        "order_total": round(order["total_cost"], 2),
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }


def export_json(orders: List[Dict[str, Any]]) -> str:
    records = [order_to_erp_record(o) for o in orders]
    return json.dumps(records, indent=2)


def export_csv(orders: List[Dict[str, Any]]) -> str:
    """Flat CSV — one row per line item, PO-level fields repeated per line."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "po_number", "customer_name", "requested_delivery_date",
            "line_number", "sku", "description", "quantity",
            "unit_of_measure", "unit_price", "line_total",
        ]
    )
    for order in orders:
        record = order_to_erp_record(order)
        for line in record["line_items"]:
            writer.writerow(
                [
                    record["po_number"],
                    record["customer_name"],
                    record["requested_delivery_date"],
                    line["line_number"],
                    line["sku"],
                    line["description"],
                    line["quantity"],
                    line["unit_of_measure"],
                    line["unit_price"],
                    line["line_total"],
                ]
            )
    return buf.getvalue()
