"""
run_eval.py — Measures actual pipeline accuracy against a labeled test set.

Usage:
    cd app
    python eval/run_eval.py             # regex-only (no API key needed)
    python eval/run_eval.py --use-llm    # includes Gemini customer/date extraction

Produces real precision/recall numbers to cite instead of an unverified
"near-perfect accuracy" claim. Re-run after any pipeline change to check
for regressions.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from eval.test_orders import TEST_CASES
from pipeline import parse_order
import storage

storage.init_db()


def evaluate(use_llm: bool = False) -> None:
    total_expected_items = 0
    total_predicted_items = 0
    total_matched_items = 0
    date_correct = 0
    date_total = 0

    print(f"Running evaluation ({'with' if use_llm else 'without'} LLM extraction)...\n")

    for i, case in enumerate(TEST_CASES, 1):
        order = parse_order(case["text"], use_llm=use_llm)

        expected_products = [(it["product"], it["quantity"]) for it in case["expected_items"]]
        predicted_products = [(it.product, it.quantity) for it in order.items]

        matched = 0
        remaining = list(predicted_products)
        for exp in expected_products:
            if exp in remaining:
                matched += 1
                remaining.remove(exp)

        total_expected_items += len(expected_products)
        total_predicted_items += len(predicted_products)
        total_matched_items += matched

        date_total += 1
        expected_date = case["expected_delivery_date"]
        if expected_date is None:
            date_ok = order.delivery_date is None
        else:
            date_ok = (order.delivery_date or "").lower() == expected_date.lower()
        if date_ok:
            date_correct += 1

        status = "OK" if matched == len(expected_products) and date_ok else "MISS"
        print(f"[{status}] #{i}: {case['text'][:60]}")
        if matched != len(expected_products):
            print(f"       expected items: {expected_products}")
            print(f"       predicted items: {predicted_products}")
        if not date_ok:
            print(f"       expected date: {expected_date!r}, got: {order.delivery_date!r}")

    precision = total_matched_items / total_predicted_items if total_predicted_items else 0
    recall = total_matched_items / total_expected_items if total_expected_items else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0
    date_accuracy = date_correct / date_total if date_total else 0

    print("\n--- Results ---")
    print(f"Item extraction — Precision: {precision:.1%}  Recall: {recall:.1%}  F1: {f1:.1%}")
    print(f"Delivery date accuracy: {date_accuracy:.1%} ({date_correct}/{date_total})")
    print(f"\nTest set size: {len(TEST_CASES)} messages")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--use-llm", action="store_true", help="Also run Gemini customer/date extraction")
    args = parser.parse_args()
    evaluate(use_llm=args.use_llm)
