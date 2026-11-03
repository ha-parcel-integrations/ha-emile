"""Synthetic eMile payloads shared by the test modules.

Nothing here is a captured reply: the codes are made up and every person,
address and phone column is blank. ``*_sample`` return the *parsed* shape the
API client hands to ``normalize_parcel``; ``*_xml`` return the wire XML.
"""

from __future__ import annotations

ACTIVE_CODE = "EM299999999992CA"
DELIVERED_CODE = "EM299999999991CA"
OTHER_CODE = "EM299999999993CA"


def row(
    description: str,
    date: str,
    zone: str = "GMT-04:00",
    number: int = 1,
    code: str = "21",
) -> dict:
    """One parsed ``track`` row."""
    return {
        "track_point_code": code,
        "date": date,
        "time_zone": zone,
        "status_number": str(number),
        "status_desc": description,
        "remark": "",
        "event_info": "",
        "country_code": "CA",
        "province": "",
        "city": "",
        "postcode": "",
        "phone_number": "",
    }


def sample(code: str, events: list[dict]) -> dict:
    """A parsed reply for ``code`` carrying ``events`` (oldest first)."""
    return {
        "response_status": 0,
        "timestamp": "11/3/2026 9:22:56 PM",
        "barcode": code,
        "trande_no": "REF000000000000",
        "recipient_country": "CA",
        "next_carrier_name": "eMile",
        "next_carrier_no": code,
        "events": events,
    }


def _in_flight() -> list[dict]:
    return [
        row(
            "Waybill Generated for REF000000000000",
            "2026-03-30 16:18:23",
            "GMT+00:00",
            1,
            "10",
        ),
        row("ARRIVE TRANSITHUB", "2026-04-04 16:34:04", number=2, code="20"),
        row("TASK ASSIGNED", "2026-04-06 19:19:54", number=3),
        row("ORDER SORTED", "2026-04-07 08:00:00", number=4, code="22"),
        row("OUTBOUND SCANNED", "2026-04-07 09:00:00", number=5, code="30"),
    ]


def in_transit_sample(code: str = ACTIVE_CODE) -> dict:
    """A parcel that has been scanned outbound but is not out for delivery yet."""
    return sample(code, _in_flight())


def active_sample(code: str = ACTIVE_CODE) -> dict:
    """An out-for-delivery parcel."""
    return sample(
        code,
        [
            *_in_flight(),
            row("OUT FOR DELIVERY", "2026-04-07 10:00:00", number=6, code="31"),
        ],
    )


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    """A delivered parcel."""
    return sample(
        code,
        [
            *_in_flight(),
            row("OUT FOR DELIVERY", "2026-04-07 10:00:00", number=6, code="31"),
            row("DELIVERED", "2026-04-07 14:30:00", number=7, code="40"),
        ],
    )


def _xml_row(r: dict) -> str:
    return "<track>" + "".join(f"<{k}>{v}</{k}>" for k, v in r.items()) + "</track>"


def found_xml(
    code: str = DELIVERED_CODE, events: list[dict] | None = None, **attrs: str
) -> bytes:
    """A found reply as the service sends it."""
    events = events if events is not None else delivered_sample(code)["events"]
    attributes = {
        "barcode": code,
        "trande_no": "REF000000000000",
        "recipient_country": "CA",
        "next_carrier_name": "eMile",
        "next_carrier_no": code,
        **attrs,
    }
    attr_text = " ".join(f'{k}="{v}"' for k, v in attributes.items())
    return (
        "<root><timestamp>11/3/2026 9:22:56 PM</timestamp><status>0</status>"
        f"<tracks {attr_text}>{''.join(_xml_row(r) for r in events)}</tracks></root>"
    ).encode()


def not_found_xml(code: str = DELIVERED_CODE) -> bytes:
    """The service's unknown-code reply."""
    return (
        "<root><timestamp>11/3/2026 9:22:56 PM</timestamp><status>0</status>"
        f'<tracks error_message="Order [{code}] not found trackingevent." '
        'recipient_country="Canada" next_carrier_name="eMile"/></root>'
    ).encode()
