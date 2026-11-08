"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

Carrier-specific: :data:`_STATUS_MAP`, the event clock and
:func:`normalize_parcel`. The sort contract, the delivered filter and the
one-shot warning for unmapped statuses are suite-wide machinery.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
import logging
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-emile/issues/new"
    "?template=unrecognised_status.yml"
)

# Keyed on the upper-cased ``status_desc``. Rows are matched exactly; the
# waybill row is the one exception, handled in :func:`_collapse`, because it
# embeds an upstream reference after a fixed prefix.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "WAYBILL GENERATED": ParcelStatus.REGISTERED,
    "PREALERT": ParcelStatus.REGISTERED,
    "CUSTOMS CLEARANCE FORECAST": ParcelStatus.IN_TRANSIT,
    "NOTIFICATION OF ARRIVAL": ParcelStatus.IN_TRANSIT,
    "CBSA HAS PUT THE FOLLOWING SHIPMENTS ON HOLD": ParcelStatus.IN_TRANSIT,
    "CUSTOMS DETENTION": ParcelStatus.IN_TRANSIT,
    "CUSTOMS RELEASED": ParcelStatus.IN_TRANSIT,
    "CUSTOMS CLEARANCE COMPLETED": ParcelStatus.IN_TRANSIT,
    "HANDED OVER": ParcelStatus.IN_TRANSIT,
    "PACKAGE ON THE WAY TO PARTNER SORTING FACILITY": ParcelStatus.IN_TRANSIT,
    "IN TRANSIT TO EMIL. TRACKING WILL UPDATE ON ARRIVAL.": ParcelStatus.IN_TRANSIT,
    "WITH PARTNER CARRIER, TRANSFERRING TO EMIL SOON.": ParcelStatus.IN_TRANSIT,
    "PICKED UP": ParcelStatus.IN_TRANSIT,
    "IN TRANSIT": ParcelStatus.IN_TRANSIT,
    "PRE_INBOUND": ParcelStatus.IN_TRANSIT,
    "PRE ASSIGNED": ParcelStatus.IN_TRANSIT,
    "ARRIVE TRANSITHUB": ParcelStatus.IN_TRANSIT,
    "ARRIVE AT THE WAREHOUSE": ParcelStatus.IN_TRANSIT,
    "ORDER SORTED": ParcelStatus.IN_TRANSIT,
    "TASK ASSIGNED": ParcelStatus.IN_TRANSIT,
    "ASSIGN DRIVER": ParcelStatus.IN_TRANSIT,
    "TRANSFERRED TO DRIVER": ParcelStatus.IN_TRANSIT,
    # The driver collects it from the warehouse, not the recipient.
    "WAITING FOR PICKUP": ParcelStatus.IN_TRANSIT,
    "OUTBOUND SCANNED": ParcelStatus.IN_TRANSIT,
    "DELIVERY MAY BE DELAYED DUE TO": ParcelStatus.IN_TRANSIT,
    "ORDER EXCEPTION RELEASED": ParcelStatus.IN_TRANSIT,
    "OUT FOR DELIVERY": ParcelStatus.OUT_FOR_DELIVERY,
    "RE-DELIVERY ATTEMPT": ParcelStatus.OUT_FOR_DELIVERY,
    "DELIVERED": ParcelStatus.DELIVERED,
    "RETURNED TO SENDER": ParcelStatus.RETURNING,
    "LOST CONFIRMED": ParcelStatus.PROBLEM,
    "ORDER DESTROYED": ParcelStatus.PROBLEM,
    "DELIVERY EXCEPTION": ParcelStatus.PROBLEM,
    "EXCEPTION NOTIFIED TO SENDER": ParcelStatus.PROBLEM,
}

_WAYBILL_PREFIX = "WAYBILL GENERATED"
_WAYBILL_TEXT = "Waybill Generated"

_OFFSET = re.compile(r"^GMT([+-])(\d{2}):(\d{2})$")

# A later row of any other status is noise on a finished parcel; these are the
# ones that can genuinely overturn a delivery.
_OVERTURNS_DELIVERED = frozenset(
    {ParcelStatus.OUT_FOR_DELIVERY, ParcelStatus.RETURNING, ParcelStatus.PROBLEM}
)

# Some wordings read like the head of a longer sentence; a reference in the
# tail must not reach a log line that gets pasted into a public issue.
_MAX_LOGGED_WORDING = 60
_DIGITS = re.compile(r"\d+")


def _clean_text(value: Any) -> str:
    """Return a status wording trimmed and with runs of whitespace collapsed."""
    return " ".join(str(value or "").split())


def _wording(event: dict) -> str:
    """Return the event's wording as shown to users.

    The waybill row carries an upstream reference after the milestone; it is
    dropped so it never reaches ``raw_status`` or ``history``.
    """
    return _collapse(_clean_text(event.get("status_desc")))


def _collapse(text: str) -> str:
    """Drop the upstream reference the waybill row appends to its milestone."""
    if text.upper().startswith(_WAYBILL_PREFIX):
        return _WAYBILL_TEXT
    return text


# Both word orders occur for the same notification row ("EMAIL REMINDER SENT",
# "REMINDER EMAIL SENT FAILED").
_REMINDER_PREFIXES = ("EMAIL REMINDER", "REMINDER EMAIL")


def _is_statusless(wording: str) -> bool:
    """Reminder mails and billing rows say nothing about the parcel's state."""
    upper = wording.upper()
    return upper.startswith(_REMINDER_PREFIXES) or upper.endswith("_FEE")


# Status codes we have already warned about, so each unmapped one is logged
# only once per HA session instead of on every poll.
_unmapped_statuses_logged: set[str] = set()


def warn_unexpected_shape(detail: str) -> None:
    """Log an unexpected reply shape once, with a copy-paste issue link.

    ``detail`` names keys and structure, never values from the reply.
    """
    if detail in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(detail)
    _LOGGER.warning(
        "Unexpected eMile reply — help us handle it. Open an issue "
        "and paste this line: %s\n  %s",
        NEW_ISSUE_URL,
        detail,
    )


def _warn_unmapped_status(wording: str) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    code = _DIGITS.sub("#", wording)[:_MAX_LOGGED_WORDING]
    if code in _unmapped_statuses_logged:
        return
    _unmapped_statuses_logged.add(code)
    _LOGGER.warning(
        "Unrecognised eMile status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → not used for the parcel status",
        NEW_ISSUE_URL,
        code,
    )


def map_event_status(wording: str | None) -> ParcelStatus | None:
    """Map one row's wording to a canonical status, or ``None``.

    Rows that carry no status stay ``None`` silently. Wording we do not map
    also stays ``None`` (so a consumer can tell "no mapping" from "mapped to
    unknown") and warns once.
    """
    wording = _collapse(_clean_text(wording))
    if not wording or _is_statusless(wording):
        return None
    mapped = _STATUS_MAP.get(wording.upper())
    if mapped is not None:
        return mapped
    _warn_unmapped_status(wording)
    return None


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def event_instant(event: dict) -> datetime | None:
    """Return the row's aware instant from ``date`` plus its ``GMT+HH:MM`` zone."""
    match = _OFFSET.match(_clean_text(event.get("time_zone")))
    if match is None:
        return None
    try:
        naive = datetime.strptime(_clean_text(event.get("date")), "%Y-%m-%d %H:%M:%S")
        offset = timedelta(hours=int(match[2]), minutes=int(match[3]))
        if match[1] == "-":
            offset = -offset
        return naive.replace(tzinfo=timezone(offset))
    except ValueError:
        return None


def _ordered_events(events: Any) -> list[tuple[datetime | None, dict]]:
    """Return ``(instant, event)`` oldest first.

    The clocks mix GMT+00:00 (waybill) with local offsets, so rows are ordered
    by the aware instant; equal instants keep the wire order, which is oldest
    first. A row whose clock cannot be read takes the previous row's instant, so
    it keeps its place and a final delivery scan is not mistaken for the oldest
    row. With no previous row the instant stays ``None`` and it sorts first.
    """
    rows: list[tuple[datetime | None, dict]] = []
    previous: datetime | None = None
    for event in events or []:
        if not isinstance(event, dict):
            continue
        instant = event_instant(event)
        if instant is None:
            zone = _clean_text(event.get("time_zone"))[:20]
            warn_unexpected_shape(f"unreadable clock, time_zone={zone}")
            instant = previous
        previous = instant
        rows.append((instant, event))
    rows.sort(key=lambda row: row[0] or datetime.min.replace(tzinfo=UTC))
    return rows


def build_history(events: Any, *, max_events: int = HISTORY_MAX_EVENTS) -> list[dict]:
    """Build the canonical ``history`` list from the carrier's rows.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. Oldest to newest, capped to the most recent
    ``max_events``. A row with no instant at all is left out.
    """
    history = [
        {
            "timestamp": instant.isoformat(),
            "status": map_event_status(_wording(event)),
            "raw_status": _wording(event) or None,
        }
        for instant, event in _ordered_events(events)
        if instant is not None
    ]
    return history[-max_events:]


def _current_event(
    events: Any,
) -> tuple[ParcelStatus, dict | None, datetime | None]:
    """Return the parcel status, the row that decided it and that row's instant.

    The newest row that maps wins, unless a delivery came before it and that
    row cannot overturn one. Rows without a status are skipped quietly; unmapped
    wording is skipped with a one-shot warning.
    """
    mapped = [
        (status, event, instant)
        for instant, event in reversed(_ordered_events(events))
        if (status := map_event_status(_wording(event))) is not None
    ]
    if not mapped:
        return ParcelStatus.UNKNOWN, None, None
    if mapped[0][0] in _OVERTURNS_DELIVERED:
        return mapped[0]
    return next((row for row in mapped if row[0] is ParcelStatus.DELIVERED), mapped[0])


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The **keys of the returned dict are the contract**: every carrier in the
    suite returns exactly these, in this order. The payload has no sender,
    receiver, ETA, pickup point, weight or dimensions, so those stay ``None``,
    and the carrier has no pickup-point state, so ``pickup`` is always ``False``.
    ``history`` is ``None`` when the option is off.
    """
    status, current, instant = _current_event(raw.get("events"))
    delivered = status is ParcelStatus.DELIVERED
    delivered_at = instant if delivered else None
    shown = current or next(
        (event for _, event in reversed(_ordered_events(raw.get("events")))), None
    )
    raw_status = (_wording(shown) or None) if shown else None

    return {
        "carrier": "eMile",
        "barcode": raw.get("barcode"),
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": raw_status,
        "delivered": delivered,
        "delivered_at": delivered_at.isoformat() if delivered_at else None,
        "planned_from": None,
        "planned_to": None,
        "pickup": False,
        "pickup_point": None,
        "url": tracking_url(raw.get("barcode")),
        "weight": None,
        "dimensions": None,
        "history": build_history(raw.get("events")) if include_history else None,
        "raw": raw,
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE)
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(UTC) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
