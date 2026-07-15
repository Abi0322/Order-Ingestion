"""
storage.py — SQLite persistence for parsed orders.

Keeps the app behaving like a real running tool (orders persist across
restarts, statuses can be updated) rather than a stateless "paste text,
see JSON" script.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).parent / "orders.db"

STATUS_NEW = "new"
STATUS_CONFIRMED = "confirmed"
STATUS_FLAGGED = "flagged"


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer TEXT,
                delivery_date TEXT,
                source TEXT,
                raw_text TEXT,
                items_json TEXT,
                total_cost REAL,
                needs_review INTEGER,
                status TEXT DEFAULT 'new',
                created_at TEXT,
                ner_cross_check_json TEXT
            )
            """
        )
        # Corrections a human makes to a flagged item's product mapping.
        # The pipeline checks this table before falling back to the static
        # catalog + fuzzy match, so the system gets more accurate the more
        # it's used — a lightweight feedback loop rather than a fixed map.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS learned_products (
                raw_text_key TEXT PRIMARY KEY,
                canonical_product TEXT NOT NULL,
                corrected_at TEXT,
                correction_count INTEGER DEFAULT 1
            )
            """
        )
        # Migration: gmail_message_id wasn't in the original schema. Added so
        # re-running "Fetch from Gmail" can skip emails already imported as
        # orders, instead of creating a duplicate every time the same message
        # happens to still be in the latest-N window.
        existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(orders)")}
        if "gmail_message_id" not in existing_columns:
            conn.execute("ALTER TABLE orders ADD COLUMN gmail_message_id TEXT")


def add_product_correction(raw_text: str, canonical_product: str) -> None:
    """Record that a human corrected raw_text -> canonical_product.

    Future parses of the same (normalized) raw text will use this mapping
    directly instead of falling back to fuzzy matching.
    """
    key = raw_text.strip().lower()
    with _connect() as conn:
        existing = conn.execute(
            "SELECT correction_count FROM learned_products WHERE raw_text_key = ?", (key,)
        ).fetchone()
        count = (existing["correction_count"] + 1) if existing else 1
        conn.execute(
            """
            INSERT INTO learned_products (raw_text_key, canonical_product, corrected_at, correction_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(raw_text_key) DO UPDATE SET
                canonical_product = excluded.canonical_product,
                corrected_at = excluded.corrected_at,
                correction_count = excluded.correction_count
            """,
            (key, canonical_product, datetime.now(timezone.utc).isoformat(), count),
        )


def get_learned_product(raw_text: str) -> Optional[str]:
    key = raw_text.strip().lower()
    with _connect() as conn:
        try:
            row = conn.execute(
                "SELECT canonical_product FROM learned_products WHERE raw_text_key = ?", (key,)
            ).fetchone()
        except sqlite3.OperationalError:
            return None  # table doesn't exist yet — init_db() hasn't run
        return row["canonical_product"] if row else None


def list_learned_products() -> List[Dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM learned_products ORDER BY corrected_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def update_order_item(order_id: int, item_index: int, new_product: str) -> None:
    """Correct a single item's product on a saved order, and recompute
    review status / total cost to match."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            return
        items = json.loads(row["items_json"] or "[]")
        if item_index >= len(items):
            return

        raw_text = items[item_index]["product_raw"]
        old_product = items[item_index]["product"]
        items[item_index]["product"] = new_product
        items[item_index]["match_score"] = 100.0
        items[item_index]["needs_review"] = False

        add_product_correction(raw_text, new_product)

        from pipeline import PRICE_LIST  # local import avoids a circular import at module load

        total_cost = sum(
            PRICE_LIST.get(it["product"], 0.0) * it["quantity"] for it in items
        )
        still_needs_review = any(it["needs_review"] for it in items) or not row["customer"] or not row["delivery_date"]

        conn.execute(
            """
            UPDATE orders
            SET items_json = ?, total_cost = ?, needs_review = ?
            WHERE id = ?
            """,
            (json.dumps(items), total_cost, 1 if still_needs_review else 0, order_id),
        )
        return old_product


def remove_order_item(order_id: int, item_index: int) -> Optional[Dict[str, Any]]:
    """Drop a single item from a saved order — a manual override for lines
    that shouldn't be on the order at all (a mistyped/garbled extraction, an
    irrelevant aside caught by the regex), as opposed to update_order_item's
    re-mapping for items that ARE real but matched the wrong catalog product.
    """
    with _connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            return None
        items = json.loads(row["items_json"] or "[]")
        if item_index >= len(items):
            return None

        removed = items.pop(item_index)

        from pipeline import PRICE_LIST  # local import avoids a circular import at module load

        total_cost = sum(PRICE_LIST.get(it["product"], 0.0) * it["quantity"] for it in items)
        still_needs_review = (
            not items
            or any(it["needs_review"] for it in items)
            or not row["customer"]
            or not row["delivery_date"]
        )

        conn.execute(
            """
            UPDATE orders
            SET items_json = ?, total_cost = ?, needs_review = ?
            WHERE id = ?
            """,
            (json.dumps(items), total_cost, 1 if still_needs_review else 0, order_id),
        )
        return removed


def update_order_item_quantity(order_id: int, item_index: int, new_quantity: int) -> None:
    """Manually correct an item's quantity — the parser can misread a
    number, or the real order changes after the fact. Only the quantity and
    the recomputed total change; the product match/confidence is untouched,
    since this isn't about what the item is, just how many."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            return
        items = json.loads(row["items_json"] or "[]")
        if item_index >= len(items):
            return

        items[item_index]["quantity"] = new_quantity

        from pipeline import PRICE_LIST  # local import avoids a circular import at module load

        total_cost = sum(PRICE_LIST.get(it["product"], 0.0) * it["quantity"] for it in items)

        conn.execute(
            "UPDATE orders SET items_json = ?, total_cost = ? WHERE id = ?",
            (json.dumps(items), total_cost, order_id),
        )


def update_order_delivery_date(order_id: int, new_date_iso: str) -> None:
    """Manually set an order's delivery date to a concrete ISO date,
    overriding whatever the parser originally extracted — a relative phrase
    like "Friday" that turned out wrong, or nothing at all. A human-supplied
    date is treated as fully resolved, so it also clears the "missing date"
    reason for needs_review if that was the only thing flagging it."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not row:
            return
        items = json.loads(row["items_json"] or "[]")
        still_needs_review = (
            not items
            or any(it["needs_review"] for it in items)
            or not row["customer"]
        )
        conn.execute(
            "UPDATE orders SET delivery_date = ?, needs_review = ? WHERE id = ?",
            (new_date_iso, 1 if still_needs_review else 0, order_id),
        )


def gmail_message_already_imported(gmail_message_id: str) -> bool:
    """Has this Gmail message already become an order? Fetching "latest N
    emails" re-checks the same recent messages every time — without this,
    every re-fetch that still has the same message in its window creates a
    second, duplicate order for it."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM orders WHERE gmail_message_id = ?", (gmail_message_id,)
        ).fetchone()
        return row is not None


def save_order(parsed: Dict[str, Any]) -> int:
    status = STATUS_FLAGGED if parsed["needs_review"] else STATUS_NEW
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO orders (customer, delivery_date, source, raw_text,
                                 items_json, total_cost, needs_review, status,
                                 created_at, ner_cross_check_json, gmail_message_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                parsed.get("customer"),
                parsed.get("delivery_date"),
                parsed.get("source"),
                parsed.get("raw_text", ""),
                json.dumps(parsed.get("items", [])),
                parsed.get("total_cost", 0.0),
                1 if parsed.get("needs_review") else 0,
                status,
                datetime.now(timezone.utc).isoformat(),
                json.dumps(parsed.get("ner_cross_check", {})),
                parsed.get("gmail_message_id"),
            ),
        )
        return cur.lastrowid


def list_orders(status: Optional[str] = None) -> List[Dict[str, Any]]:
    with _connect() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status = ? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
        return [_row_to_dict(r) for r in rows]


def update_status(order_id: int, status: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))


def delete_order(order_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    d["items"] = json.loads(d.pop("items_json") or "[]")
    d["needs_review"] = bool(d["needs_review"])
    d["ner_cross_check"] = json.loads(d.pop("ner_cross_check_json", None) or "{}")
    return d
