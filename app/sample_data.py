"""Sample order messages for demoing the pipeline without Gmail configured."""

SAMPLE_ORDERS = [
    {
        "sender": "Riverside Clinic",
        "subject": "Urgent supply request",
        "body": "Hi team, we need 2x insulin pens by Friday morning for a patient. Thanks, Riverside Clinic.",
    },
    {
        "sender": "ABC Medical Center",
        "subject": "Weekly order",
        "body": "Please send 1 box of surgical gloves and 3 vials of vaccine to ABC Medical Center by 2025-09-20.",
    },
    {
        "sender": "Northgate Pharmacy",
        "subject": "Restock request",
        "body": "Requesting 3 dozen N95 masks and 200 tablets of paracetamol before month-end. Please confirm delivery date.",
    },
    {
        "sender": "unknown sender",
        "subject": "quick q",
        "body": "hey can we get some more thermometers soon? maybe 5 of them",
    },
]
