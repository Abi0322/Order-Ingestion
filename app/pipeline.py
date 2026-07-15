"""
pipeline.py — Core order-parsing logic.

Approach (deliberately hybrid, not a single "trained NER model"):
  1. Regex extracts quantity / unit / product spans from free-text order messages.
     Fast, deterministic, and free — handles the bulk of well-formed orders.
  2. Product names are normalized against a small catalog using fuzzy string
     matching (rapidfuzz), so "insulin pens" and "Insulin Pen x2" resolve to the
     same canonical product.
  3. Gemini (an LLM) is used only for the fields regex can't reliably get:
     customer/sender name and delivery date, since these appear in too many
     free-form shapes for a fixed pattern to catch.
  4. Every parsed order gets a confidence assessment: items with a weak fuzzy
     match, or orders missing a customer/date, are flagged for human review
     instead of being silently guessed.

This is intentionally not framed as "entity recognition" — there's no NER model
here, just pattern matching + a normalization step + one LLM call for the fields
that need it. That's a more accurate description of what's actually happening.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

try:
    from rapidfuzz import process as rf_process
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False
    from difflib import SequenceMatcher

try:
    import google.generativeai as genai
    HAS_GEMINI = True
except ImportError:
    HAS_GEMINI = False


# ---------------------------------------------------------------------------
# Product catalog & pricing (mock data standing in for a real ERP/catalog API)
# ---------------------------------------------------------------------------

PRODUCT_CATALOG: Dict[str, str] = {
    "insulin pen": "Insulin Pen",
    "insulin pens": "Insulin Pen",
    "surgical glove": "Surgical Gloves",
    "surgical gloves": "Surgical Gloves",
    "n95 mask": "N95 Mask",
    "n95 masks": "N95 Mask",
    "ecg electrode": "ECG Electrode",
    "ecg electrodes": "ECG Electrode",
    "sterile syringe": "Sterile Syringe",
    "sterile syringes": "Sterile Syringe",
    "oxygen cylinder": "Oxygen Cylinder",
    "oxygen cylinders": "Oxygen Cylinder",
    "oxygen tank": "Oxygen Cylinder",
    "oxygen tanks": "Oxygen Cylinder",
    "bandage": "Bandage",
    "bandages": "Bandage",
    "paracetamol tablet": "Paracetamol Tablet",
    "paracetamol tablets": "Paracetamol Tablet",
    "digital thermometer": "Digital Thermometer",
    "digital thermometers": "Digital Thermometer",
    "iv drip": "IV Drip",
    "iv drips": "IV Drip",
    "ppe kit": "PPE Kit",
    "ppe kits": "PPE Kit",
    "face shield": "Face Shield",
    "face shields": "Face Shield",
}
PRODUCT_CATALOG = {k.lower(): v for k, v in PRODUCT_CATALOG.items()}

PRICE_LIST: Dict[str, float] = {
    "Insulin Pen": 25.0,
    "Surgical Gloves": 500.0,
    "N95 Mask": 1.5,
    "ECG Electrode": 0.75,
    "Sterile Syringe": 5.0,
    "Oxygen Cylinder": 120.0,
    "Bandage": 0.5,
    "Paracetamol Tablet": 0.1,
    "Digital Thermometer": 20.0,
    "IV Drip": 80.0,
    "PPE Kit": 25.0,
    "Face Shield": 4.0,
}

FUZZY_MATCH_THRESHOLD = 75  # below this, we don't trust the normalization

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

# Products are captured up to a "stop" boundary (delivery/time phrasing, a
# conjunction, or punctuation) rather than a bare \b, so multi-word product
# names ("insulin pens", "N95 masks") aren't truncated to their first word.
_STOP = r"(?=\s*(?:,|\.|$|\band\b|\balso\b|\bby\b|\bbefore\b|\bon\b|\bto\b|\bin\b|\bnext\b|\bfor\b|\brequired\b|\bneeded\b|\bASAP\b|\burgently\b|\bsoon\b))"
_UNIT_WORDS = (
    r"boxes|box|packs|pack|cartons|carton|pallets|pallet|pieces|piece|"
    r"vials|vial|cylinders|cylinder|tablets|tablet|bottles|bottle"
)
# A negative lookbehind for a preceding letter, so the quantity number can't
# match a digit embedded inside a product code like "O2" or "N95" — without
# it, "3 O2 bottles" spuriously matches qty=2 (from the "2" in "O2") instead
# of qty=3.
_QTY = r"(?<![A-Za-z])\d+"

QUANTITY_PATTERNS = [
    # "10 boxes of surgical gloves"
    rf"(?P<qty>{_QTY})\s*(?P<unit>{_UNIT_WORDS})\b(?:\s*of)?\s*(?P<product>[A-Za-z0-9\-\./]+(?:\s+[A-Za-z0-9\-\./]+)*?){_STOP}",
    # "2x insulin pens", "3x oxygen cylinders"
    rf"(?P<qty>{_QTY})\s*[xX]\s*(?P<product>[A-Za-z0-9\-\./]+(?:\s+[A-Za-z0-9\-\./]+)*?)"
    rf"(?:\s+(?P<unit>{_UNIT_WORDS}))?{_STOP}",
    # "3 dozen N95 masks"
    rf"(?P<qty>{_QTY})\s*dozen\s*(?P<product>[A-Za-z0-9\-\./]+(?:\s+[A-Za-z0-9\-\./]+)*?){_STOP}",
    # "half a dozen bandages"
    rf"half\s*a\s*dozen\s*(?P<product>[A-Za-z0-9\-\./]+(?:\s+[A-Za-z0-9\-\./]+)*?){_STOP}",
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ParsedItem:
    product_raw: str
    product: str
    quantity: int
    unit: str = ""
    match_score: Optional[float] = None  # fuzzy match confidence, 0-100

    @property
    def needs_review(self) -> bool:
        # No score means an exact catalog hit (fine) or a title-cased fallback
        # with no catalog entry at all (not fine).
        if self.match_score is None:
            return self.product_raw.strip().lower() not in PRODUCT_CATALOG
        return self.match_score < FUZZY_MATCH_THRESHOLD


@dataclass
class ParsedOrder:
    customer: Optional[str]
    delivery_date: Optional[str]
    items: List[ParsedItem] = field(default_factory=list)
    source: str = "manual"  # "manual" | "gmail"
    raw_text: str = ""
    ner_cross_check: Dict[str, Any] = field(default_factory=dict)

    @property
    def needs_review(self) -> bool:
        if not self.customer or not self.delivery_date:
            return True
        if not self.items:
            return True
        if any(it.needs_review for it in self.items):
            return True
        # NER disagreement is a real signal: an independent extraction
        # method spotted something our primary extraction missed or got wrong.
        if self.ner_cross_check.get("customer_agrees") is False:
            return True
        if self.ner_cross_check.get("date_agrees") is False:
            return True
        return False

    @property
    def total_cost(self) -> float:
        total = 0.0
        for it in self.items:
            price = PRICE_LIST.get(it.product)
            if price is not None:
                total += price * it.quantity
        return total

    def to_dict(self) -> Dict[str, Any]:
        return {
            "customer": self.customer,
            "delivery_date": self.delivery_date,
            "items": [
                {
                    "product_raw": it.product_raw,
                    "product": it.product,
                    "quantity": it.quantity,
                    "unit": it.unit,
                    "match_score": it.match_score,
                    "needs_review": it.needs_review,
                }
                for it in self.items
            ],
            "source": self.source,
            "needs_review": self.needs_review,
            "total_cost": self.total_cost,
            "ner_cross_check": self.ner_cross_check,
        }


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _singular(unit: str) -> str:
    unit = unit.lower().strip()
    return unit[:-1] if unit.endswith("s") else unit


def normalize_product_name(raw_name: str) -> tuple[str, Optional[float]]:
    """Return (canonical_name, fuzzy_match_score_or_None).

    Checks human corrections first (learned_products), then the static
    catalog, then fuzzy matching. This is the self-improving part of the
    pipeline: once a person corrects a flagged item, that exact raw text
    resolves with full confidence from then on.
    """
    key = raw_name.lower().strip()

    try:
        import storage
        learned = storage.get_learned_product(raw_name)
        if learned:
            return learned, 100.0
    except ImportError:
        pass  # storage not available (e.g. running pipeline standalone/in tests)

    if key in PRODUCT_CATALOG:
        return PRODUCT_CATALOG[key], None  # exact match, fully trusted

    choices = list(PRODUCT_CATALOG.keys())

    if HAS_RAPIDFUZZ:
        match, score, _ = rf_process.extractOne(key, choices)
    else:
        # Fallback: stdlib difflib, scaled to look like a 0-100 rapidfuzz score.
        best_match, best_score = None, 0.0
        for choice in choices:
            ratio = SequenceMatcher(None, key, choice).ratio() * 100
            if ratio > best_score:
                best_match, best_score = choice, ratio
        match, score = best_match, best_score

    if match is not None and score >= FUZZY_MATCH_THRESHOLD:
        return PRODUCT_CATALOG[match], score
    return raw_name.strip().title(), score


# ---------------------------------------------------------------------------
# Regex extraction
# ---------------------------------------------------------------------------

def regex_parse_items(text: str) -> List[ParsedItem]:
    items: List[ParsedItem] = []
    for pat in QUANTITY_PATTERNS:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            qty_str = m.groupdict().get("qty")
            qty = int(qty_str) if qty_str else 1
            if "dozen" in m.group(0).lower() and "half" not in m.group(0).lower():
                qty *= 12
            if "half a dozen" in m.group(0).lower():
                qty = 6
            unit = m.groupdict().get("unit", "") or ""
            prod_raw = (m.group("product") or "").strip(" .,+")
            if not prod_raw:
                continue
            canonical, score = normalize_product_name(prod_raw)
            items.append(
                ParsedItem(
                    product_raw=prod_raw,
                    product=canonical,
                    quantity=qty,
                    unit=_singular(unit) if unit else "",
                    match_score=score,
                )
            )

    if not items:
        # Same bounded shape as the patterns above ("<qty> <product>", no unit
        # word or "x" needed) — still stops at a _STOP boundary so one match
        # doesn't swallow the rest of a multi-item message.
        # Negative lookahead excludes "10 days"/"2 weeks" — a delivery-date
        # duration, not a product line — from being swallowed as an item.
        generic_pattern = (
            rf"(?P<qty>{_QTY})\s+(?!(?:days?|weeks?)\b)"
            rf"(?P<product>[A-Za-z0-9\-\./]+(?:\s+[A-Za-z0-9\-\./]+)*?){_STOP}"
        )
        for m in re.finditer(generic_pattern, text, flags=re.IGNORECASE):
            prod_raw = m.group("product").strip()
            canonical, score = normalize_product_name(prod_raw)
            items.append(
                ParsedItem(
                    product_raw=prod_raw,
                    product=canonical,
                    quantity=int(m.group("qty")),
                    unit="",
                    match_score=score,
                )
            )
    return items


# Order-intent language. Deliberately generic-but-scoped verbs/nouns, not
# medical-supply-specific — the point is filtering inbox noise (newsletters,
# social notifications, receipts), not narrowing to a product domain.
_ORDER_KEYWORDS = re.compile(
    r"\b(orders?|ordering|send|sending|deliver(?:y|ies)?|supply|supplies|"
    r"restock(?:ing)?|requir(?:e|es|ed|ing|ement)|request(?:ing|ed)?|"
    r"need(?:s|ed|ing)?|purchas(?:e|ing)|arrange|provide|providing|"
    r"quantity|units?|invoice|shipment|ship|shipping|\bpo\b)\b",
    re.IGNORECASE,
)


def gemini_confirms_order(text: str) -> bool:
    """Ask Gemini whether a *borderline* email is really a supply order.

    Only called for the ambiguous middle case looks_like_order can't resolve
    for free (order-intent language present, but no confidently-matched
    catalog item) — never for every fetched email, since that would burn
    through the free-tier daily quota fast. Fails closed: any error, missing
    key, or unparseable response returns False, since silently admitting a
    non-order into the inbox is worse than occasionally skipping a real one.
    """
    if not _ensure_gemini():
        return False
    try:
        model = genai.GenerativeModel("gemini-flash-latest")
        prompt = (
            "Is the following email a request/order for physical supplies, "
            "equipment, or goods — as opposed to a receipt, newsletter, "
            "social/account notification, marketing email, or unrelated "
            "correspondence that just happens to mention words like "
            '"order" or "delivery"?\n\n'
            f"Email:\n{text}\n\n"
            'Reply with strict JSON only: {"is_order": true} or {"is_order": false}.'
        )
        resp = model.generate_content(prompt)
        raw = (resp.text or "").strip() if resp else ""
        match = re.search(r"\{.*?\}", raw, flags=re.DOTALL)
        if match:
            return json.loads(match.group(0)).get("is_order") is True
    except Exception:
        pass
    return False


def looks_like_order(text: str, use_llm: bool = True) -> bool:
    """Gate for Gmail fetch: only treat an email as an order candidate if it
    uses order-intent language AND has a confidently-recognizable item —
    checked cheaply first, with a single LLM call as a last resort for the
    genuinely ambiguous case.

    Three tiers, cheapest first:
      1. No order-intent keyword at all -> reject for free.
      2. A regex-extracted item is an exact catalog hit or clears
         FUZZY_MATCH_THRESHOLD -> accept for free. (Catching only "some
         number+words pattern matched" here is what let boilerplate through
         before — "1600 Amphitheatre Parkway" parses as cleanly as "5
         insulin pens" without this check.)
      3. Keyword present but nothing confidently matches the catalog (e.g. a
         real order phrased with a product the catalog doesn't know, like
         "O2 bottles") -> spend one Gemini call to ask directly, rather than
         silently reject every non-catalog product term.
    """
    if not _ORDER_KEYWORDS.search(text):
        return False
    items = regex_parse_items(text)
    if any(it.match_score is None or it.match_score >= FUZZY_MATCH_THRESHOLD for it in items):
        return True
    if use_llm:
        return gemini_confirms_order(text)
    return False


def extract_delivery_date_regex(text: str) -> Optional[str]:
    txt = text.lower()
    for wd in WEEKDAYS:
        if wd in txt:
            return wd.title()
    m = re.search(r"(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}|\d{1,2}(?:st|nd|rd|th)\s+[A-Za-z]+)", text)
    if m:
        return m.group(0)
    # "in 10 days", "in 2 weeks" — a relative offset with no fixed calendar
    # date in the text at all, so the "by <word>" fallback below would never
    # catch it (there's no "by").
    m_rel = re.search(r"\bin\s+(\d+)\s+(day|week)s?\b", text, flags=re.IGNORECASE)
    if m_rel:
        n, unit = m_rel.group(1), m_rel.group(2).lower()
        plural = "s" if n != "1" else ""
        return f"In {n} {unit}{plural}"
    m2 = re.search(r"by\s+([A-Za-z]+)", text, flags=re.IGNORECASE)
    if m2:
        return m2.group(1).title()
    return None


def resolve_delivery_date(delivery_date: Optional[str], reference: date) -> Optional[date]:
    """Resolve a delivery_date string — which may be relative ("Tomorrow", a
    weekday name) or absolute (various formats, including free-form dates
    from Gemini) — into a concrete calendar date.

    `reference` is the date the order was actually parsed/received: relative
    terms like "tomorrow" are meaningless without it, since a message that
    says "need it tomorrow" means a fixed calendar day, not whatever day the
    inbox happens to be viewed on later.

    Returns None for phrasing that isn't a date at all ("ASAP", "month-end"),
    so callers can sort/display those separately from real dates.
    """
    if not delivery_date:
        return None
    s = delivery_date.strip().lower()

    if s == "today":
        return reference
    if s == "tomorrow":
        return reference + timedelta(days=1)
    if s in WEEKDAYS:
        delta = (WEEKDAYS.index(s) - reference.weekday()) % 7
        return reference + timedelta(days=delta)

    m_rel = re.match(r"^in\s+(\d+)\s+(day|week)s?$", s)
    if m_rel:
        n, unit = int(m_rel.group(1)), m_rel.group(2)
        return reference + timedelta(days=n * 7 if unit == "week" else n)

    try:
        from dateutil import parser as dateutil_parser
        parsed = dateutil_parser.parse(
            delivery_date,
            default=datetime.combine(reference, datetime.min.time()),
            dayfirst=True,
        )
        return parsed.date()
    except (ValueError, OverflowError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Gemini extraction (customer + delivery date only — regex handles items)
# ---------------------------------------------------------------------------

_gemini_configured = False


def _ensure_gemini():
    global _gemini_configured
    if not HAS_GEMINI:
        return False
    if not _gemini_configured:
        api_key = os.environ.get("GEMINI_API_KEY")
        if not api_key:
            return False
        genai.configure(api_key=api_key)
        _gemini_configured = True
    return True


def gemini_match_product(raw_phrase: str) -> Optional[str]:
    """Ask Gemini to map an unrecognized product phrase (e.g. "oxygen tanks")
    onto the closest existing catalog product (e.g. "Oxygen Cylinder").

    Only called for items regex/fuzzy-matching couldn't confidently resolve —
    it never invents a new product, only picks among the fixed catalog or
    returns None if nothing reasonably fits.
    """
    if not _ensure_gemini():
        return None
    catalog_names = sorted(set(PRODUCT_CATALOG.values()))
    try:
        model = genai.GenerativeModel("gemini-flash-latest")
        prompt = (
            "A medical-supply order mentions a product using non-standard wording.\n"
            f"Fixed catalog of valid products: {catalog_names}\n"
            f'Raw phrase from the order: "{raw_phrase}"\n\n'
            'Return strict JSON only: {"match": "<exact catalog product name>"} '
            'if one of the catalog products is clearly what\'s meant, otherwise '
            '{"match": null}. Do not pick a catalog product unless it\'s a '
            "confident, sensible match."
        )
        resp = model.generate_content(prompt)
        raw = (resp.text or "").strip() if resp else ""
        match = re.search(r"\{.*?\}", raw, flags=re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            candidate = parsed.get("match")
            if candidate in catalog_names:
                return candidate
    except Exception:
        pass
    return None


def clarify_unmatched_items(items: List[ParsedItem]) -> List[ParsedItem]:
    """For items the regex + fuzzy-matching stage couldn't confidently map
    to the catalog, ask Gemini to pick the closest catalog product.

    A confirmed match is saved as a learned correction (the same mechanism
    used when a person corrects a flagged item in the UI), so the exact same
    raw phrasing resolves instantly next time without another LLM call.
    """
    for item in items:
        if not item.needs_review:
            continue
        matched = gemini_match_product(item.product_raw)
        if not matched:
            continue
        item.product = matched
        item.match_score = 100.0
        try:
            import storage
            storage.add_product_correction(item.product_raw, matched)
        except ImportError:
            pass  # storage not available (e.g. running pipeline standalone/in tests)
    return items


def gemini_extract_customer_and_date(text: str) -> Dict[str, Optional[str]]:
    if not _ensure_gemini():
        return {"customer": None, "delivery_date": None}
    try:
        model = genai.GenerativeModel("gemini-flash-latest")
        prompt = (
            "Extract structured order info from this healthcare order message.\n"
            "Return strict JSON only, with keys \"Customer\" and \"Delivery_Date\".\n"
            "Use null if a field isn't present.\n\n"
            f"Message:\n{text}"
        )
        resp = model.generate_content(prompt)
        raw = (resp.text or "").strip() if resp else ""
        match = re.search(r"\{.*?\}", raw, flags=re.DOTALL)
        if match:
            parsed = json.loads(match.group(0))
            return {
                "customer": parsed.get("Customer"),
                "delivery_date": parsed.get("Delivery_Date"),
            }
    except Exception:
        pass
    return {"customer": None, "delivery_date": None}


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------

def parse_order(text: str, source: str = "manual", use_llm: bool = True) -> ParsedOrder:
    items = regex_parse_items(text)

    customer = None
    delivery_date = None

    if use_llm:
        items = clarify_unmatched_items(items)
        llm_result = gemini_extract_customer_and_date(text)
        customer = llm_result.get("customer")
        delivery_date = llm_result.get("delivery_date")

    if not delivery_date:
        delivery_date = extract_delivery_date_regex(text)

    import ner_utils
    cross_check = ner_utils.cross_check(text, customer, delivery_date)

    return ParsedOrder(
        customer=customer,
        delivery_date=delivery_date,
        items=items,
        source=source,
        raw_text=text,
        ner_cross_check=cross_check,
    )
