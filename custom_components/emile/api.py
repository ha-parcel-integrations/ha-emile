"""eMile public tracking API client.

The endpoint takes a small XML request and answers HTTP 200 for found and
unknown codes alike, so the outcome is read from the body. The client's
contract with the coordinator:

* ``async_get_parcel`` returns the parsed per-parcel dict on success,
* returns ``None`` only when the carrier says the code is unknown, using its
  exact not-found sentence (a normal, expected state — never an error),
* raises :class:`EmileApiError` for anything else — HTTP errors, challenge
  pages, an unexpected envelope, a reply naming another parcel, an empty
  history — with ``status_code`` set
  on a non-2xx response and ``retry_after`` set when a 429 carried a
  ``Retry-After`` in seconds,
* lets ``aiohttp.ClientError`` propagate untouched — ``DataUpdateCoordinator``
  already wraps those into ``UpdateFailed``.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from xml.etree import ElementTree as ET

import aiohttp

from .const import MAX_RESPONSE_BYTES, TRACKING_API_URL
from .parcels import warn_unexpected_shape

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

_REQUEST_HEADERS = {
    "Content-Type": "application/xml",
    "Accept": "application/xml",
}

# The service only ever answers with data, never a DTD. Refusing any
# declaration keeps entity-expansion payloads away from the parser.
_DECLARATION = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)

# The declaration check reads raw bytes, so a wide encoding would slip past it.
_WIDE_ENCODING_BOMS = (b"\xff\xfe", b"\xfe\xff")


class EmileApiError(Exception):
    """Raised when an eMile API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"eMile API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


def not_found_message(tracking_code: str) -> str:
    """Return the exact sentence the carrier uses for an unknown code."""
    return f"Order [{tracking_code}] not found trackingevent."


def _element_value(element: ET.Element) -> Any:
    """Return an element's text, or a dict of its children when it has any."""
    if len(element) == 0:
        return (element.text or "").strip()
    children: dict[str, Any] = {}
    for child in element:
        value = _element_value(child)
        if child.tag in children:
            existing = children[child.tag]
            children[child.tag] = (
                [*existing, value] if isinstance(existing, list) else [existing, value]
            )
        else:
            children[child.tag] = value
    return children


async def _read_capped(response: aiohttp.ClientResponse) -> bytes:
    """Read the body, refusing anything above the size cap."""
    body = bytearray()
    async for chunk in response.content.iter_chunked(65536):
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise EmileApiError("response too large")
    return bytes(body)


def _parse(tracking_code: str, body: bytes) -> dict[str, Any] | None:
    """Turn the XML body into the parcel dict, ``None`` for an unknown code."""
    if b"\x00" in body or body.startswith(_WIDE_ENCODING_BOMS):
        raise EmileApiError("unexpected encoding")
    if _DECLARATION.search(body):
        raise EmileApiError("unexpected XML declaration")
    try:
        root = ET.fromstring(body)  # noqa: S314 - declarations are refused above
    except (ET.ParseError, ValueError, LookupError, RecursionError) as err:
        raise EmileApiError("unparseable body") from err

    if root.tag != "root":
        raise EmileApiError("unexpected envelope")
    status = (root.findtext("status") or "").strip()
    if status != "0":
        raise EmileApiError(f"service status {status or 'missing'}")
    tracks_list = root.findall("tracks")
    if len(tracks_list) != 1:
        raise EmileApiError("unexpected envelope")
    tracks = tracks_list[0]
    rows = tracks.findall("track")

    error_message = tracks.get("error_message")
    if error_message is not None:
        barcode = tracks.get("barcode")
        if (
            error_message == not_found_message(tracking_code)
            and not rows
            and barcode in (None, tracking_code)
        ):
            return None
        raise EmileApiError("unexpected error message")

    barcode = tracks.get("barcode")
    if barcode is None:
        warn_unexpected_shape("found reply without a barcode")
    elif barcode != tracking_code:
        raise EmileApiError("reply names a different parcel")
    if not rows:
        raise EmileApiError("empty history")
    try:
        events = [_element_value(row) for row in rows]
    except RecursionError as err:
        raise EmileApiError("unparseable body") from err

    return {
        "response_status": 0,
        "timestamp": (root.findtext("timestamp") or "").strip() or None,
        **tracks.attrib,
        "events": events,
    }


class EmileApiClient:
    """Client for the public eMile tracking endpoint (keyless, XML)."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with an aiohttp session."""
        self._session = session

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any] | None:
        """Fetch one parcel's tracking details.

        ``tracking_code`` must already be validated: it is interpolated into
        the XML request and relies on being plain ``EM<digits>CA``.
        """
        request = (
            "<tracks><language>en</language><track>"
            f"<barcode>{tracking_code}</barcode></track></tracks>"
        )
        try:
            async with self._session.post(
                TRACKING_API_URL,
                data=request.encode(),
                headers=_REQUEST_HEADERS,
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status == 429:
                    retry_after_header = response.headers.get("Retry-After")
                    try:
                        retry_after = (
                            float(retry_after_header) if retry_after_header else None
                        )
                    except ValueError:
                        retry_after = None  # an HTTP-date, not seconds
                    raise EmileApiError(
                        "HTTP 429", status_code=429, retry_after=retry_after
                    )
                if response.status != 200:
                    raise EmileApiError(
                        f"HTTP {response.status}", status_code=response.status
                    )
                body = await _read_capped(response)
        except TimeoutError as err:
            # The coordinator only absorbs EmileApiError/ClientError; a bare
            # timeout would otherwise take the whole update down and drop the
            # last good data.
            raise EmileApiError("timeout") from err

        return _parse(tracking_code, body)
