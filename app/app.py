"""
app.py — AI Order Ingestion: Orders Inbox

An ops-style inbox for incoming order messages: parse free-text orders
(pasted, or fetched from Gmail), review anything the pipeline is unsure
about, and confirm orders into a running order book.
"""

import html
from datetime import date, datetime

import streamlit as st

import erp_export
import gmail_client
import ner_utils
import storage
from pipeline import PRODUCT_CATALOG, looks_like_order, parse_order, resolve_delivery_date
from sample_data import SAMPLE_ORDERS

st.set_page_config(page_title="AI Order Ingestion", page_icon="📦", layout="wide")
storage.init_db()

if "gemini_key_set" not in st.session_state:
    st.session_state.gemini_key_set = bool(st.secrets.get("GEMINI_API_KEY", None)) if hasattr(st, "secrets") else False

# Reload periodically so shipping urgency (which tier an order falls into,
# and its position in the sort order) stays correct for a tab left open —
# otherwise "ships in 3 days" silently goes stale until the next click.
st.markdown('<meta http-equiv="refresh" content="1800">', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Urgency pills — status color (never plain red/amber/green dots): a small
# color-coded dot is paired with a distinct text label per tier, so the
# signal never rides on color alone. Colors are a reserved status palette,
# not the app's categorical/brand colors, and are theme-aware.
# ---------------------------------------------------------------------------

st.markdown(
    """
    <style>
    .urgency-pill {
        display: inline-flex; align-items: center; gap: 6px;
        padding: 3px 11px 4px; border-radius: 999px;
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
        font-size: 12.5px; letter-spacing: -0.01em; line-height: 1.5;
        margin-bottom: 0.6rem;
    }
    .urgency-pill .dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; }
    .urgency-pill[data-tier="critical"] { background: rgba(208,59,59,0.16); border: 1px solid rgba(208,59,59,0.40); color: #e66767; font-weight: 700; }
    .urgency-pill[data-tier="critical"] .dot { background: #e66767; }
    .urgency-pill[data-tier="urgent"]   { background: rgba(236,131,90,0.16); border: 1px solid rgba(236,131,90,0.38); color: #ffffff; font-weight: 600; }
    .urgency-pill[data-tier="urgent"]   .dot { background: #ec835a; }
    .urgency-pill[data-tier="good"]     { background: rgba(12,163,12,0.14); border: 1px solid rgba(12,163,12,0.32); color: #c3c2b7; font-weight: 500; }
    .urgency-pill[data-tier="good"]     .dot { background: #0ca30c; }
    .urgency-pill[data-tier="neutral"]  { background: rgba(137,135,129,0.16); border: 1px solid rgba(137,135,129,0.34); color: #898781; font-weight: 500; }
    .urgency-pill[data-tier="neutral"]  .dot { background: #898781; }

    /* Stat tiles — the inbox summary row */
    .stat-tile {
        display: flex; flex-direction: column; gap: 10px;
        padding: 18px 18px 16px; border-radius: 16px;
        border: 1px solid rgba(255,255,255,0.10);
        background:
            linear-gradient(165deg, rgba(255,255,255,0.05), rgba(255,255,255,0) 55%),
            #1f1f1e;
        box-shadow: 0 1px 3px rgba(0,0,0,0.35);
    }
    .stat-tile .icon-badge {
        width: 34px; height: 34px; border-radius: 10px;
        display: flex; align-items: center; justify-content: center;
        overflow: hidden;
    }
    .stat-tile .icon-glyph {
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
        font-size: 24px; font-weight: 800; line-height: 1;
        opacity: 0.5;
    }
    .stat-tile .stat-value {
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
        font-size: 28px; font-weight: 650; letter-spacing: -0.02em; line-height: 1.05;
        color: #ffffff;
    }
    .stat-tile .stat-label {
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
        font-size: 13px; font-weight: 500; color: #c3c2b7;
    }
    .stat-tile[data-accent="blue"]    .icon-badge { background: rgba(57,135,229,0.18); color: #3987e5; }
    .stat-tile[data-accent="warning"] .icon-badge { background: rgba(250,178,25,0.20); color: #fab219; }
    .stat-tile[data-accent="good"]    .icon-badge { background: rgba(12,163,12,0.20); color: #0ca30c; }

    /* Feedback banners — replaces default st.success/error/warning/info */
    .feedback-banner {
        display: flex; align-items: flex-start; gap: 10px;
        padding: 11px 14px; border-radius: 12px;
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
        font-size: 14px; line-height: 1.45; color: #ffffff;
        margin-bottom: 0.75rem;
    }
    .feedback-banner .fb-icon {
        width: 20px; height: 20px; border-radius: 50%; margin-top: 1px;
        display: flex; align-items: center; justify-content: center;
        font-size: 11px; font-weight: 800; flex-shrink: 0;
    }
    .feedback-banner[data-kind="success"] { background: rgba(12,163,12,0.15); border: 1px solid rgba(12,163,12,0.38); }
    .feedback-banner[data-kind="success"] .fb-icon { background: rgba(12,163,12,0.24); color: #0ca30c; }
    .feedback-banner[data-kind="error"]   { background: rgba(208,59,59,0.15); border: 1px solid rgba(208,59,59,0.38); }
    .feedback-banner[data-kind="error"]   .fb-icon { background: rgba(208,59,59,0.24); color: #e66767; }
    .feedback-banner[data-kind="warning"] { background: rgba(250,178,25,0.15); border: 1px solid rgba(250,178,25,0.38); }
    .feedback-banner[data-kind="warning"] .fb-icon { background: rgba(250,178,25,0.26); color: #fab219; }
    .feedback-banner[data-kind="info"]    { background: rgba(57,135,229,0.15); border: 1px solid rgba(57,135,229,0.38); }
    .feedback-banner[data-kind="info"]    .fb-icon { background: rgba(57,135,229,0.24); color: #3987e5; }

    /* Empty states — zero orders, zero confirmed, zero learned corrections */
    .empty-state {
        display: flex; flex-direction: column; align-items: center; text-align: center;
        gap: 4px; padding: 36px 24px; border-radius: 16px;
        border: 1px dashed rgba(255,255,255,0.18);
        background: rgba(255,255,255,0.02);
    }
    .empty-state .es-icon {
        width: 42px; height: 42px; border-radius: 12px; margin-bottom: 8px;
        display: flex; align-items: center; justify-content: center;
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
        font-size: 19px; font-weight: 800; opacity: 0.6;
        background: rgba(57,135,229,0.16); color: #3987e5;
    }
    .empty-state .es-title {
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
        font-size: 15.5px; font-weight: 650; color: #ffffff;
    }
    .empty-state .es-desc {
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
        font-size: 13.5px; color: #898781; max-width: 380px; line-height: 1.5; margin-top: 2px;
    }

    /* Correction "decision" label — sits above the picker inside its own container */
    .correction-label {
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
        font-size: 12.5px; font-weight: 650; color: #fab219;
        display: flex; align-items: center; gap: 6px; margin-bottom: 8px;
    }

    /* Section headers — ERP export, Learned corrections, sidebar title */
    .section-heading {
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
        font-size: 20px; font-weight: 700; letter-spacing: -0.01em; color: #ffffff;
    }
    .section-subheading {
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
        font-size: 13px; color: #898781; margin-top: 1px;
    }

    /* Structural refinement for native widgets — shape/type only; color
       comes from .streamlit/config.toml's [theme] (base = "dark"), so
       Streamlit's own chrome (sidebar, page, widget backgrounds) always
       matches these components instead of following a separate signal. */
    [data-testid="stTextArea"] textarea,
    [data-testid="stTextInput"] input,
    [data-testid="stSelectbox"] div[data-baseweb="select"] > div {
        border-radius: 10px !important;
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif !important;
    }
    [data-testid="stButton"] button,
    [data-testid="stDownloadButton"] button {
        border-radius: 10px !important;
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif !important;
        font-weight: 600 !important;
    }
    [data-testid="stExpander"] {
        border-radius: 14px !important;
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
    }
    [data-testid="stTabs"] button[role="tab"] p {
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif !important;
        font-weight: 600 !important;
    }

    /* Order card header — avatar + name + metadata pills, replacing one
       dense text string as the expander's own label. */
    .order-header-row { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
    .order-avatar {
        width: 36px; height: 36px; border-radius: 50%; flex-shrink: 0;
        display: flex; align-items: center; justify-content: center;
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
        font-size: 14px; font-weight: 700; color: #ffffff;
    }
    .order-header-info { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
    .order-customer-name {
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
        font-size: 16px; font-weight: 650; letter-spacing: -0.01em; color: #ffffff;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }
    .order-meta-row { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }

    .status-pill {
        display: inline-flex; align-items: center; gap: 5px;
        padding: 2px 9px 3px; border-radius: 999px;
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
        font-size: 11.5px; font-weight: 600; letter-spacing: -0.01em;
    }
    .status-pill .dot { width: 5px; height: 5px; border-radius: 50%; flex-shrink: 0; }
    .status-pill[data-status="new"]       { background: rgba(57,135,229,0.16); color: #3987e5; }
    .status-pill[data-status="new"]       .dot { background: #3987e5; }
    .status-pill[data-status="flagged"]   { background: rgba(250,178,25,0.18); color: #fab219; }
    .status-pill[data-status="flagged"]   .dot { background: #fab219; }
    .status-pill[data-status="confirmed"] { background: rgba(12,163,12,0.16); color: #0ca30c; }
    .status-pill[data-status="confirmed"] .dot { background: #0ca30c; }

    .meta-pill {
        display: inline-flex; align-items: center; gap: 5px;
        padding: 2px 9px 3px; border-radius: 999px;
        font-family: -apple-system, system-ui, "Segoe UI", sans-serif;
        font-size: 11.5px; font-weight: 500; color: #898781;
        background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.08);
    }

    /* Micro-interactions — subtle, not flashy: a small lift + shadow on
       hover so the interface feels alive without adding motion clutter. */
    .stat-tile { transition: transform 0.15s ease, box-shadow 0.15s ease; }
    .stat-tile:hover { transform: translateY(-2px); box-shadow: 0 6px 16px rgba(0,0,0,0.32); }
    [data-testid="stExpander"] { transition: border-color 0.15s ease; }
    [data-testid="stButton"] button,
    [data-testid="stDownloadButton"] button {
        transition: transform 0.1s ease, filter 0.1s ease !important;
    }
    [data-testid="stButton"] button:hover,
    [data-testid="stDownloadButton"] button:hover {
        transform: translateY(-1px);
        filter: brightness(1.08);
    }

    /* Thin hairline dividers instead of the default heavier rule. */
    [data-testid="stMarkdownContainer"] hr { border-color: rgba(255,255,255,0.09) !important; margin: 1.4rem 0 !important; }
    </style>
    """,
    unsafe_allow_html=True,
)


# Glyph fills the badge large and soft (opacity, not a small crisp icon) —
# a faded watermark-style character rather than a bold/loud symbol.
_STAT_ICONS = {
    "list": "#",
    "alert": "!",
    "check": "✓",
    "banknote": "$",
}


def _render_stat_tile(icon_key: str, accent: str, label: str, value: str) -> None:
    st.markdown(
        f'<div class="stat-tile" data-accent="{accent}">'
        f'<div class="icon-badge"><span class="icon-glyph">{_STAT_ICONS[icon_key]}</span></div>'
        f'<div class="stat-value">{value}</div>'
        f'<div class="stat-label">{label}</div>'
        f"</div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Feedback banners — on-brand replacement for st.success/error/warning/info.
#
# Streamlit's built-in alert colors are applied via computed CSS-in-JS with
# no stable per-type selector to hook (no "kind" attribute exposed in this
# Streamlit version) — restyling them with plain CSS isn't reliable, so this
# renders the same status-palette language used everywhere else instead.
# ---------------------------------------------------------------------------

_FEEDBACK_ICONS = {"success": "✓", "error": "✕", "warning": "!", "info": "i"}


def _render_feedback(kind: str, message: str) -> None:
    # message can carry text lifted straight from user/email content (spaCy
    # NER-detected org/date spans, Gmail error text) with no character
    # restriction — unlike regex-extracted product names, which are limited
    # to a safe character class by construction. Escape before embedding in
    # unsafe_allow_html markdown so that can never become HTML injection.
    st.markdown(
        f'<div class="feedback-banner" data-kind="{kind}">'
        f'<div class="fb-icon">{_FEEDBACK_ICONS[kind]}</div>'
        f"<div>{html.escape(message)}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )


def _flash(kind: str, message: str) -> None:
    """Queue a styled message to render at the top of the page after the
    upcoming rerun. Calling st.success() right before st.rerun() shows the
    message for a single frame that gets wiped before anyone can read it —
    this survives the rerun by riding in session_state."""
    st.session_state["_flash"] = (kind, message)


def _render_pending_flash() -> None:
    flash = st.session_state.pop("_flash", None)
    if flash:
        _render_feedback(*flash)


def _render_empty_state(icon: str, title: str, description: str) -> None:
    st.markdown(
        f'<div class="empty-state">'
        f'<div class="es-icon">{icon}</div>'
        f'<div class="es-title">{title}</div>'
        f'<div class="es-desc">{description}</div>'
        f"</div>",
        unsafe_allow_html=True,
    )


def _render_section_heading(title: str, subtitle: str = "") -> None:
    sub = f'<div class="section-subheading">{subtitle}</div>' if subtitle else ""
    st.markdown(f'<div class="section-heading">{title}</div>{sub}', unsafe_allow_html=True)


def _urgency_tier(days_until: "int | None") -> tuple[str, str]:
    """Map days-until-delivery to a (tier_key, label) pair — exactly three
    date tiers (overdue / within 3 days / 5+ days), plus a separate neutral
    case for no resolvable date at all, which is a needs-review problem, not
    a point on the urgency scale."""
    if days_until is None:
        return "neutral", "Needs review — no date present"
    if days_until < 0:
        n = abs(days_until)
        return "critical", "Overdue by 1 day" if n == 1 else f"Overdue by {n} days"
    if days_until <= 3:
        if days_until == 0:
            return "urgent", "Due today"
        if days_until == 1:
            return "urgent", "Due tomorrow"
        return "urgent", f"Ships in {days_until} days"
    return "good", f"Ships in {days_until} days"


# Categorical identity colors (dark-surface steps), for avatars only — kept
# separate from the reserved status palette (good/warning/serious/critical),
# which never doubles as an identity/series color and vice versa.
_AVATAR_COLORS = ["#3987e5", "#199e70", "#c98500", "#008300", "#9085e9", "#e66767", "#d55181", "#d95926"]

_STATUS_PILL = {
    storage.STATUS_NEW: ("new", "New"),
    storage.STATUS_FLAGGED: ("flagged", "Needs review"),
    storage.STATUS_CONFIRMED: ("confirmed", "Confirmed"),
}


def _avatar_color(name: str) -> str:
    # A stable (not Python's randomized str hash) index, so the same
    # customer always lands on the same color across restarts — that
    # consistency is what makes avatars useful for at-a-glance recognition.
    return _AVATAR_COLORS[sum(name.encode("utf-8")) % len(_AVATAR_COLORS)]


def _render_order_header(customer: "str | None", status: str, tier: str, tier_label: str, item_count: int) -> None:
    display_name = customer or "Unknown customer"
    initial = html.escape(display_name.strip()[:1].upper() or "?")
    color = _avatar_color(display_name)
    status_key, status_label = _STATUS_PILL[status]
    items_label = "1 item" if item_count == 1 else f"{item_count} items"

    st.markdown(
        '<div class="order-header-row">'
        f'<div class="order-avatar" style="background:{color};">{initial}</div>'
        '<div class="order-header-info">'
        f'<div class="order-customer-name">{html.escape(display_name)}</div>'
        '<div class="order-meta-row">'
        f'<span class="status-pill" data-status="{status_key}"><span class="dot"></span>{status_label}</span>'
        f'<span class="urgency-pill" data-tier="{tier}"><span class="dot"></span>{html.escape(tier_label)}</span>'
        f'<span class="meta-pill">{items_label}</span>'
        "</div></div></div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Sidebar — intake
# ---------------------------------------------------------------------------

st.sidebar.markdown(
    '<div style="font-family: -apple-system, system-ui, \'Segoe UI\', sans-serif; '
    'font-size: 21px; font-weight: 700; letter-spacing: -0.01em;">📥 Add orders</div>'
    '<div style="font-family: -apple-system, system-ui, \'Segoe UI\', sans-serif; '
    'font-size: 12.5px; color: #898781; margin-top: 1px; margin-bottom: 0.5rem;">'
    "Paste a message, load a demo, or pull from Gmail</div>",
    unsafe_allow_html=True,
)

with st.sidebar.expander("⚙️ Settings"):
    enable_erp_export = st.checkbox(
        "Enable ERP export",
        value=False,
        help="Show a section for exporting confirmed orders as a PO-shaped JSON/CSV.",
    )

tab_manual, tab_sample, tab_gmail = st.sidebar.tabs(["📝 Paste", "🧪 Sample", "📧 Gmail"])

with tab_manual:
    text = st.text_area("Order message", height=140, placeholder="e.g. Need 2x insulin pens by Friday morning.")
    use_llm = st.checkbox("Use LLM for customer/date extraction + unmatched items", value=True,
                           help="Requires GEMINI_API_KEY. Also used to resolve product phrases the regex/fuzzy "
                                "matching stage flags for review. Falls back to regex-only extraction if unset.")
    if st.button("Parse & add to inbox", type="primary", use_container_width=True):
        if text.strip():
            parsed = parse_order(text, source="manual", use_llm=use_llm)
            storage.save_order(parsed.to_dict() | {"raw_text": text})
            _flash("success", "Order added to inbox.")
            st.rerun()
        else:
            _render_feedback("warning", "Paste an order message first.")

with tab_sample:
    st.caption("No Gmail setup needed — load a few example order messages.")
    if st.button("Load sample orders", use_container_width=True):
        for sample in SAMPLE_ORDERS:
            parsed = parse_order(sample["body"], source="manual", use_llm=True)
            if not parsed.customer:
                parsed.customer = sample["sender"]
            d = parsed.to_dict()
            d["raw_text"] = sample["body"]
            d["customer"] = parsed.customer
            storage.save_order(d)
        _flash("success", f"Loaded {len(SAMPLE_ORDERS)} sample orders.")
        st.rerun()

with tab_gmail:
    if gmail_client.gmail_available():
        from_email = st.text_input("Filter by sender (optional)")
        n = st.slider("Number of emails", 1, 10, 3)
        if st.button("Fetch from Gmail", use_container_width=True):
            with st.spinner("Fetching and parsing emails..."):
                try:
                    emails = gmail_client.fetch_latest_emails(n=n, from_email=from_email or None)
                    added, skipped, duplicates = 0, 0, 0
                    for e in emails:
                        # Dedup: "fetch latest N" re-lists the same recent
                        # messages every time you click the button, so
                        # without this, re-fetching creates a second order
                        # for an email that's already been imported.
                        if storage.gmail_message_already_imported(e["message_id"]):
                            duplicates += 1
                            continue
                        # Gate: only emails that read like an actual order
                        # (order-intent language + at least one extractable
                        # quantity/product) become orders — otherwise every
                        # newsletter/notification in the inbox turns into a
                        # garbage order card.
                        if not looks_like_order(e["body"]):
                            skipped += 1
                            continue
                        parsed = parse_order(e["body"], source="gmail", use_llm=True)
                        if not parsed.customer:
                            parsed.customer = e["sender"]
                        d = parsed.to_dict()
                        d["raw_text"] = e["body"]
                        d["customer"] = parsed.customer
                        d["gmail_message_id"] = e["message_id"]
                        storage.save_order(d)
                        added += 1
                    _flash(
                        "success",
                        f"Fetched {len(emails)} email(s) — added {added} order(s), "
                        f"skipped {skipped} that didn't look order-related, "
                        f"{duplicates} already imported.",
                    )
                    st.rerun()
                except Exception as e:
                    _render_feedback("error", f"Gmail fetch failed: {e}")
    else:
        st.caption(
            "Gmail isn't configured in this environment. Add credentials.json "
            "(see gmail_client.py docstring) to enable this tab — it's optional, "
            "the app works fully without it."
        )


# ---------------------------------------------------------------------------
# Main — Orders Inbox
# ---------------------------------------------------------------------------

st.markdown(
    '<div style="font-family: -apple-system, system-ui, \'Segoe UI\', sans-serif;">'
    '<div style="font-size: 30px; font-weight: 700; letter-spacing: -0.02em;">📦 Order Ingestion Inbox</div>'
    '<div style="font-size: 14px; color: #898781; margin-top: 2px;">Parsed orders, sorted by shipping urgency</div>'
    "</div>",
    unsafe_allow_html=True,
)
st.write("")
_render_pending_flash()

all_orders = storage.list_orders()
needs_review_count = sum(1 for o in all_orders if o["status"] == storage.STATUS_FLAGGED)
confirmed_count = sum(1 for o in all_orders if o["status"] == storage.STATUS_CONFIRMED)
confirmed_value = sum(o["total_cost"] for o in all_orders if o["status"] == storage.STATUS_CONFIRMED)

col1, col2, col3, col4 = st.columns(4)
with col1:
    _render_stat_tile("list", "blue", "Total orders", str(len(all_orders)))
with col2:
    _render_stat_tile("alert", "warning", "Needs review", str(needs_review_count))
with col3:
    _render_stat_tile("check", "good", "Confirmed", str(confirmed_count))
with col4:
    _render_stat_tile("banknote", "blue", "Est. value (confirmed)", f"${confirmed_value:,.2f}")

st.divider()

_render_section_heading("Orders", "Sorted by shipping urgency — closest first.")
st.write("")

filter_choice = st.radio(
    "Filter", ["All", "Needs review", "New", "Confirmed"], horizontal=True, label_visibility="collapsed"
)

search_col, from_col, to_col = st.columns([2, 1, 1])
customer_search = search_col.text_input(
    "Search by customer", placeholder="🔍 Search by customer…", label_visibility="collapsed"
)
date_from = from_col.date_input("Ships from", value=None, label_visibility="collapsed")
date_to = to_col.date_input("Ships to", value=None, label_visibility="collapsed")

status_map = {"Needs review": storage.STATUS_FLAGGED, "New": storage.STATUS_NEW, "Confirmed": storage.STATUS_CONFIRMED}
orders = all_orders if filter_choice == "All" else [o for o in all_orders if o["status"] == status_map[filter_choice]]

# Resolve each order's delivery_date (which may be relative — "Tomorrow", a
# weekday name — or absolute) into a real calendar date, using the order's
# own created_at as the reference point ("tomorrow" means a fixed day, not
# whatever day the inbox happens to be viewed on). Needed before the date
# range filter can apply, and reused afterward for sorting/urgency display.
today = date.today()


def _resolve(order: dict) -> "date | None":
    # created_at is stored in UTC; convert to local time before taking the
    # date, or "tomorrow" resolves a day early/late for anyone not on UTC.
    reference = datetime.fromisoformat(order["created_at"]).astimezone().date()
    return resolve_delivery_date(order["delivery_date"], reference)


orders_with_dates = [(o, _resolve(o)) for o in orders]

if customer_search.strip():
    q = customer_search.strip().lower()
    orders_with_dates = [(o, d) for o, d in orders_with_dates if q in (o["customer"] or "").lower()]

if date_from or date_to:
    # A date range only makes sense against orders that actually resolved
    # to a real date — one with no date at all can't be "in range".
    def _in_range(resolved: "date | None") -> bool:
        if resolved is None:
            return False
        if date_from and resolved < date_from:
            return False
        if date_to and resolved > date_to:
            return False
        return True

    orders_with_dates = [(o, d) for o, d in orders_with_dates if _in_range(d)]

# Sort soonest-first regardless of which filters are active — the most
# urgent shipments should always surface at the top; no-date orders sort last.
orders_with_dates.sort(key=lambda pair: (pair[1] is None, pair[1] or date.max))

any_filter_active = filter_choice != "All" or bool(customer_search.strip()) or date_from or date_to

if not orders_with_dates:
    if not all_orders:
        _render_empty_state(
            "+",
            "No orders yet",
            "Paste an order message, load sample data, or fetch from Gmail using the sidebar to get started.",
        )
    elif any_filter_active:
        _render_empty_state(
            "⦿",
            "No orders match your filters",
            "Nothing matches this combination of status, customer, and date range — try loosening one of them.",
        )
    else:
        _render_empty_state(
            "⦿",
            "No orders here",
            "Nothing to show right now.",
        )

for order, resolved_date in orders_with_dates:
    days_until = (resolved_date - today).days if resolved_date else None
    tier, tier_label = _urgency_tier(days_until)

    date_display = (
        f"{order['delivery_date']} ({resolved_date.isoformat()})"
        if resolved_date
        else (order["delivery_date"] or "no date")
    )
    expander_label = f"Order #{order['id']} · {date_display}"

    container = st.container(border=True)
    with container:
        _render_order_header(order["customer"], order["status"], tier, tier_label, len(order["items"]))

    with container.expander(expander_label, expanded=(order["status"] == storage.STATUS_FLAGGED or tier == "critical")):
        left, right = st.columns([3, 1])

        with left:
            st.caption(f"Source: {order['source']} · Order #{order['id']}")

            ner = order.get("ner_cross_check") or {}
            if ner.get("ner_ran"):
                notes = []
                if ner.get("customer_agrees") is False:
                    orgs = ", ".join(ner.get("ner_orgs", [])) or "an organization"
                    notes.append(f"NER detected {orgs} in the text, which doesn't match the extracted customer.")
                if ner.get("date_agrees") is False:
                    dates = ", ".join(ner.get("ner_dates", [])) or "a date"
                    notes.append(f"NER detected {dates} in the text, which doesn't match the extracted delivery date.")
                if notes:
                    _render_feedback("warning", " ".join(notes))

            if order["items"]:
                for idx, it in enumerate(order["items"]):
                    item_cols = st.columns([2, 1.6, 1, 0.6, 0.8, 1.6, 0.7])
                    item_cols[0].write(("⚠️ " if it["needs_review"] else "✅ ") + it["product"])
                    item_cols[1].caption(f"raw: {it['product_raw']}")
                    # Editable quantity — the parser can misread a number
                    # ("in 10 days" vs. an actual count) or the real order
                    # can just change after the fact. Apply is disabled
                    # until the value actually differs from what's stored,
                    # so this doesn't silently write on every rerender.
                    new_qty = item_cols[2].number_input(
                        f"Quantity for {it['product_raw']}",
                        min_value=0, value=int(it["quantity"]), step=1,
                        key=f"qty_{order['id']}_{idx}", label_visibility="collapsed",
                    )
                    if item_cols[3].button(
                        "✓", key=f"apply_qty_{order['id']}_{idx}",
                        help="Apply quantity change",
                        disabled=(int(new_qty) == it["quantity"]),
                    ):
                        storage.update_order_item_quantity(order["id"], idx, int(new_qty))
                        _flash("success", f"Updated quantity for '{it['product_raw']}' to {int(new_qty)}.")
                        st.rerun()
                    item_cols[4].write(it["unit"] or "—")
                    confidence = f"{it['match_score']:.0f}%" if it["match_score"] is not None else "exact"
                    item_cols[5].caption(f"confidence: {confidence}")
                    # Manual override: drop a line entirely — for a mistyped
                    # or irrelevant extraction ("22 sticks of bulla ice
                    # cream") that isn't a real order item at all, as
                    # opposed to the correction card below, which is for
                    # items that ARE real but matched the wrong product.
                    if item_cols[6].button("🗑️", key=f"remove_{order['id']}_{idx}", help="Remove this item from the order"):
                        storage.remove_order_item(order["id"], idx)
                        _flash("success", f"Removed '{it['product_raw']}' from this order.")
                        st.rerun()

                    if it["needs_review"]:
                        # The correction moment: this is the AI surfacing its
                        # own uncertainty and handing the decision to a human
                        # — a deliberate step, not a bare dropdown.
                        with st.container(border=True):
                            # product_raw is regex-extracted (restricted to a
                            # safe character class) so this is already safe
                            # by construction, but escape anyway rather than
                            # depend on that invariant holding forever.
                            st.markdown(
                                '<div class="correction-label">✎ Needs a decision — '
                                f'what is &ldquo;{html.escape(it["product_raw"])}&rdquo; actually?</div>',
                                unsafe_allow_html=True,
                            )
                            catalog_options = sorted(set(PRODUCT_CATALOG.values()))
                            pick_col, act_col = st.columns([3, 1])
                            correction = pick_col.selectbox(
                                f"Correct product for '{it['product_raw']}'",
                                options=["— choose correct product —"] + catalog_options,
                                key=f"correct_{order['id']}_{idx}",
                                label_visibility="collapsed",
                            )
                            apply_disabled = correction == "— choose correct product —"
                            if act_col.button(
                                "Apply",
                                key=f"save_correct_{order['id']}_{idx}",
                                type="primary",
                                use_container_width=True,
                                disabled=apply_disabled,
                            ):
                                storage.update_order_item(order["id"], idx, correction)
                                _flash(
                                    "success",
                                    f"Learned: '{it['product_raw']}' → {correction}. "
                                    "Future orders with this phrasing will match automatically.",
                                )
                                st.rerun()
            else:
                _render_feedback("warning", "No items could be extracted from this message.")
            st.write(f"**Estimated total:** ${order['total_cost']:,.2f}")
            with st.popover("View original message"):
                st.text(order["raw_text"])

        with right:
            st.write("**Delivery date**")
            # A human-supplied date overrides whatever the parser extracted
            # (a relative phrase that turned out wrong, or nothing at all)
            # with a concrete one — no more guessing "Friday" vs. "next
            # Friday". Update is disabled until it actually differs.
            new_date = st.date_input(
                f"Set delivery date for order {order['id']}",
                value=(resolved_date or today),
                key=f"date_{order['id']}", label_visibility="collapsed",
            )
            if st.button(
                "Update date", key=f"update_date_{order['id']}", use_container_width=True,
                disabled=(new_date == resolved_date),
            ):
                storage.update_order_delivery_date(order["id"], new_date.isoformat())
                _flash("success", f"Delivery date updated to {new_date.isoformat()}.")
                st.rerun()

            st.write("**Actions**")
            if order["status"] != storage.STATUS_CONFIRMED:
                if st.button("Confirm order", key=f"confirm_{order['id']}", use_container_width=True):
                    storage.update_status(order["id"], storage.STATUS_CONFIRMED)
                    st.rerun()
            if order["status"] != storage.STATUS_FLAGGED:
                if st.button("Flag for review", key=f"flag_{order['id']}", use_container_width=True):
                    storage.update_status(order["id"], storage.STATUS_FLAGGED)
                    st.rerun()
            if st.button("Delete", key=f"delete_{order['id']}", use_container_width=True):
                storage.delete_order(order["id"])
                st.rerun()


# ---------------------------------------------------------------------------
# ERP export
# ---------------------------------------------------------------------------

confirmed_orders = [o for o in all_orders if o["status"] == storage.STATUS_CONFIRMED]
if enable_erp_export:
    st.divider()
    _render_section_heading(
        "📤 Export confirmed orders",
        "Mapped to a purchase-order schema (PO number, SKU, line items) ready for an ERP import.",
    )
    st.write("")
    if confirmed_orders:
        st.caption(f"{len(confirmed_orders)} confirmed order(s) ready to export.")
        exp_col1, exp_col2 = st.columns(2)
        exp_col1.download_button(
            "Download as JSON",
            data=erp_export.export_json(confirmed_orders),
            file_name="orders_erp_export.json",
            mime="application/json",
            use_container_width=True,
        )
        exp_col2.download_button(
            "Download as CSV",
            data=erp_export.export_csv(confirmed_orders),
            file_name="orders_erp_export.csv",
            mime="text/csv",
            use_container_width=True,
        )
    else:
        _render_empty_state(
            "$",
            "Nothing to export yet",
            "Confirm an order (from the Actions panel on any order card) to enable a JSON/CSV export here.",
        )

st.write("")
learned = storage.list_learned_products()
with st.expander(f"🧠 Learned corrections ({len(learned)})"):
    if learned:
        st.caption("Product mappings the pipeline learned from your corrections.")
        st.table(
            [
                {"Raw text": l["raw_text_key"], "Maps to": l["canonical_product"], "Times corrected": l["correction_count"]}
                for l in learned
            ]
        )
    else:
        _render_empty_state(
            "✎",
            "No corrections learned yet",
            "When you fix a flagged item's product match, that mapping is remembered here — the same "
            "phrasing resolves automatically next time, without needing a human or an LLM call again.",
        )
