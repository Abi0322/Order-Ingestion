"""
ner_utils.py — spaCy-based entity recognition, used as a cross-check.

This is the genuine "entity recognition" component of the pipeline: spaCy's
pretrained NER model independently tags ORG (organization) and DATE entities
in the raw order text. We compare those against what the regex/Gemini stage
extracted for customer and delivery date:

  - If spaCy found an ORG/DATE that roughly matches what we already have,
    confidence goes up (two independent methods agree).
  - If spaCy found an ORG/DATE that *disagrees* with what we have, or found
    one when we have nothing, that's a signal worth flagging for review.
  - If spaCy found nothing either, that's neutral — plenty of valid orders
    don't name an organization explicitly.

This is intentionally scoped to customer/date, not product/quantity —
spaCy's general-purpose NER isn't trained on medical-supply nouns, so it
would just add noise there. Product extraction stays with the regex +
fuzzy-normalization pipeline, which is the right tool for that job.
"""

from __future__ import annotations

from typing import Dict, List, Optional

_nlp = None
_load_attempted = False


def _get_nlp():
    global _nlp, _load_attempted
    if _load_attempted:
        return _nlp
    _load_attempted = True
    try:
        import spacy
        _nlp = spacy.load("en_core_web_sm")
    except Exception:
        # Model not installed, or spaCy not installed at all. The pipeline
        # degrades gracefully — cross-checks just won't run.
        _nlp = None
    return _nlp


def ner_available() -> bool:
    return _get_nlp() is not None


def extract_entities(text: str) -> Dict[str, List[str]]:
    """Return {'orgs': [...], 'dates': [...]} found by spaCy NER."""
    nlp = _get_nlp()
    if nlp is None:
        return {"orgs": [], "dates": []}

    doc = nlp(text)
    orgs = [ent.text.strip() for ent in doc.ents if ent.label_ == "ORG"]
    dates = [ent.text.strip() for ent in doc.ents if ent.label_ == "DATE"]
    return {"orgs": orgs, "dates": dates}


def _fuzzy_contains(candidate: str, haystack_items: List[str]) -> bool:
    candidate_lower = candidate.lower()
    for item in haystack_items:
        item_lower = item.lower()
        if candidate_lower in item_lower or item_lower in candidate_lower:
            return True
    return False


def cross_check(
    text: str, customer: Optional[str], delivery_date: Optional[str]
) -> Dict[str, object]:
    """
    Cross-check the pipeline's customer/delivery_date against independent
    spaCy NER output. Returns a dict describing agreement/disagreement,
    which the pipeline uses to adjust its review-flagging decision.
    """
    if not ner_available():
        return {"ner_ran": False, "customer_agrees": None, "date_agrees": None, "ner_orgs": [], "ner_dates": []}

    entities = extract_entities(text)
    orgs, dates = entities["orgs"], entities["dates"]

    customer_agrees: Optional[bool] = None
    if customer:
        customer_agrees = _fuzzy_contains(customer, orgs) if orgs else None
    elif orgs:
        customer_agrees = False  # NER found an org, pipeline found nothing — worth a look

    date_agrees: Optional[bool] = None
    if delivery_date:
        date_agrees = _fuzzy_contains(delivery_date, dates) if dates else None
    elif dates:
        date_agrees = False

    return {
        "ner_ran": True,
        "customer_agrees": customer_agrees,
        "date_agrees": date_agrees,
        "ner_orgs": orgs,
        "ner_dates": dates,
    }
