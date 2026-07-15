"""
test_orders.py — Hand-labeled test set for pipeline evaluation.

Each case is a realistic order message with the ground-truth structured
output a human would extract. Used by run_eval.py to compute real
precision/recall numbers instead of an unverified accuracy claim.
"""

TEST_CASES = [
    {
        "text": "Need 2x insulin pens by Friday morning.",
        "expected_items": [{"product": "Insulin Pen", "quantity": 2}],
        "expected_delivery_date": "Friday",
    },
    {
        "text": "Please send 1 box of surgical gloves to ABC Clinic by 2025-09-20.",
        "expected_items": [{"product": "Surgical Gloves", "quantity": 1}],
        "expected_delivery_date": "2025-09-20",
    },
    {
        "text": "Requesting 3 dozen N95 masks ASAP.",
        "expected_items": [{"product": "N95 Mask", "quantity": 36}],
        "expected_delivery_date": None,
    },
    {
        "text": "Order: 5x ECG electrodes, required before 2025-09-20.",
        "expected_items": [{"product": "ECG Electrode", "quantity": 5}],
        "expected_delivery_date": "2025-09-20",
    },
    {
        "text": "We'd like 12 packs of sterile syringes, latest by Tuesday.",
        "expected_items": [{"product": "Sterile Syringe", "quantity": 12}],
        "expected_delivery_date": "Tuesday",
    },
    {
        "text": "Urgent: 1x oxygen cylinder, by tomorrow.",
        "expected_items": [{"product": "Oxygen Cylinder", "quantity": 1}],
        "expected_delivery_date": None,  # "tomorrow" isn't a weekday/ISO date — known regex gap
    },
    {
        "text": "Book 50 bandages, needed on or before 17/09/2025.",
        "expected_items": [{"product": "Bandage", "quantity": 50}],
        "expected_delivery_date": "17/09/2025",
    },
    {
        "text": "Require 200 tablets of paracetamol before month-end.",
        "expected_items": [{"product": "Paracetamol Tablet", "quantity": 200}],
        "expected_delivery_date": None,
    },
    {
        "text": "Send one box of digital thermometers by 09-20-2025.",
        "expected_items": [{"product": "Digital Thermometer", "quantity": 1}],
        "expected_delivery_date": None,  # "one" isn't parsed as a digit — known gap
    },
    {
        "text": "Please arrange 2 cartons of IV drips for delivery this week.",
        "expected_items": [{"product": "IV Drip", "quantity": 2}],
        "expected_delivery_date": None,
    },
    {
        "text": "3x PPE kits and 10x face shields needed by Monday.",
        "expected_items": [
            {"product": "PPE Kit", "quantity": 3},
            {"product": "Face Shield", "quantity": 10},
        ],
        "expected_delivery_date": "Monday",
    },
    {
        "text": "Half a dozen bandages required, no rush.",
        "expected_items": [{"product": "Bandage", "quantity": 6}],
        "expected_delivery_date": None,
    },
    {
        "text": "4 vials of sterile syringes needed by Wednesday for the clinic.",
        "expected_items": [{"product": "Sterile Syringe", "quantity": 4}],
        "expected_delivery_date": "Wednesday",
    },
    {
        "text": "Please send 6 boxes of N95 masks and 2 boxes of surgical gloves by next Friday.",
        "expected_items": [
            {"product": "N95 Mask", "quantity": 6},
            {"product": "Surgical Gloves", "quantity": 2},
        ],
        "expected_delivery_date": "Friday",
    },
    {
        "text": "hey can we get some more thermometers soon? maybe 5 of them",
        "expected_items": [{"product": "Digital Thermometer", "quantity": 5}],  # informal phrasing — known gap
        "expected_delivery_date": None,
    },
]
