"""Tests for the eMile API client."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.emile.api import EmileApiClient, EmileApiError
from custom_components.emile.const import MAX_RESPONSE_BYTES, TRACKING_API_URL

from .payloads import DELIVERED_CODE, found_xml, not_found_xml, row

CODE = DELIVERED_CODE


def _session(status: int = 200, body: bytes = b"", headers: dict | None = None):
    async def chunks(_size):
        for start in range(0, len(body), 65536):
            yield body[start : start + 65536]

    response = MagicMock()
    response.status = status
    response.headers = headers or {}
    response.content.iter_chunked = chunks
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=response)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    return session


async def _get(body: bytes, code: str = CODE, **kwargs):
    return await EmileApiClient(_session(body=body, **kwargs)).async_get_parcel(code)


async def test_found_reply_is_parsed():
    parcel = await _get(found_xml())
    assert parcel["barcode"] == CODE
    assert parcel["recipient_country"] == "CA"
    assert parcel["response_status"] == 0
    assert len(parcel["events"]) == 7
    assert parcel["events"][0]["status_desc"].startswith("Waybill Generated")
    assert parcel["events"][-1]["status_desc"] == "DELIVERED"


async def test_request_is_the_documented_xml_post():
    session = _session(body=found_xml())
    await EmileApiClient(session).async_get_parcel(CODE)
    args, kwargs = session.post.call_args
    assert args[0] == TRACKING_API_URL
    assert (
        kwargs["data"]
        == (
            f"<tracks><language>en</language><track><barcode>{CODE}</barcode></track></tracks>"
        ).encode()
    )
    assert kwargs["headers"]["Content-Type"] == "application/xml"


async def test_not_found_sentinel_is_none():
    assert await _get(not_found_xml()) is None


async def test_not_found_with_matching_barcode_attribute_is_none():
    body = not_found_xml().replace(b"<tracks ", f'<tracks barcode="{CODE}" '.encode())
    assert await _get(body) is None


async def test_not_found_for_another_code_is_an_error():
    with pytest.raises(EmileApiError):
        await _get(not_found_xml("EM299999999999CA"))


async def test_not_found_with_a_different_barcode_attribute_is_an_error():
    body = not_found_xml().replace(b"<tracks ", b'<tracks barcode="EM299999999999CA" ')
    with pytest.raises(EmileApiError):
        await _get(body)


async def test_not_found_sentinel_with_rows_is_an_error():
    body = not_found_xml().replace(
        b'next_carrier_name="eMile"/>',
        b'next_carrier_name="eMile"><track/></tracks>',
    )
    with pytest.raises(EmileApiError):
        await _get(body)


async def test_other_error_message_is_an_error_not_not_found():
    body = not_found_xml().replace(b"not found trackingevent.", b"service busy.")
    with pytest.raises(EmileApiError) as err:
        await _get(body)
    assert "busy" not in str(err.value)


async def test_wrong_barcode_in_a_found_reply_is_an_error():
    with pytest.raises(EmileApiError):
        await _get(found_xml("EM299999999999CA"))


@pytest.mark.parametrize("country", ["Canada", "US"])
async def test_recipient_country_is_not_a_gate(country):
    parcel = await _get(found_xml(recipient_country=country))
    assert parcel["recipient_country"] == country


async def test_missing_recipient_country_is_accepted():
    body = found_xml().replace(b'recipient_country="CA" ', b"")
    assert (await _get(body))["barcode"] == CODE


async def test_found_reply_without_barcode_warns_once_and_is_accepted(caplog):
    body = found_xml().replace(f'barcode="{CODE}" '.encode(), b"")
    with caplog.at_level(logging.WARNING):
        first = await _get(body)
        await _get(body)
    assert "barcode" not in first
    assert first["events"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "template=unrecognised_status.yml" in warnings[0].getMessage()


async def test_empty_history_is_an_error_not_not_found():
    with pytest.raises(EmileApiError):
        await _get(found_xml(events=[]))


async def test_nonzero_service_status_is_an_error():
    body = found_xml().replace(b"<status>0</status>", b"<status>1</status>")
    with pytest.raises(EmileApiError):
        await _get(body)


async def test_missing_service_status_is_an_error():
    with pytest.raises(EmileApiError):
        await _get(found_xml().replace(b"<status>0</status>", b""))


async def test_wrong_envelope_is_an_error():
    with pytest.raises(EmileApiError):
        await _get(b"<html><body>challenge</body></html>")


async def test_two_tracks_elements_are_an_error():
    body = found_xml().replace(b"</root>", b"<tracks/></root>")
    with pytest.raises(EmileApiError):
        await _get(body)


async def test_challenge_html_is_an_error():
    with pytest.raises(EmileApiError):
        await _get(b"<!DOCTYPE html><html><body>Just a moment...</body></html>")


async def test_truncated_xml_is_an_error():
    with pytest.raises(EmileApiError):
        await _get(found_xml()[:-30])


async def test_dtd_is_refused():
    body = b'<!DOCTYPE root [<!ENTITY a "x">]>' + found_xml()
    with pytest.raises(EmileApiError) as err:
        await _get(body)
    assert "declaration" in str(err.value)


@pytest.mark.parametrize(
    "body",
    [
        found_xml().decode().encode("utf-16"),
        found_xml().decode().encode("utf-16-le"),
        found_xml().decode().encode("utf-16-be"),
        b"\xff\xfe",
        (b'<!DOCTYPE root [<!ENTITY a "DELIVERED">]>' + found_xml())
        .decode()
        .encode("utf-16"),
    ],
)
async def test_wide_encodings_are_refused(body):
    with pytest.raises(EmileApiError) as err:
        await _get(body)
    assert "encoding" in str(err.value)


@pytest.mark.parametrize(
    "body",
    [
        b'<?xml version="1.0" encoding="nope"?><root/>',
        b'<?xml version="1.0" encoding="shift_jis"?><root/>',
        b"<a>" * 20000 + b"</a>" * 20000,
    ],
)
async def test_bodies_the_parser_chokes_on_are_api_errors(body):
    with pytest.raises(EmileApiError):
        await _get(body)


async def test_too_deeply_nested_row_is_an_api_error():
    with (
        patch("custom_components.emile.api._element_value", side_effect=RecursionError),
        pytest.raises(EmileApiError),
    ):
        await _get(found_xml())


async def test_oversized_body_is_refused():
    with pytest.raises(EmileApiError):
        await _get(b"<root>" + b"x" * MAX_RESPONSE_BYTES + b"</root>")


async def test_nested_columns_are_kept_in_raw():
    rows = [row("DELIVERED", "2026-04-07 14:30:00")]
    body = found_xml(events=rows).replace(
        b"<remark></remark>",
        b"<proof_images><image>a</image><image>b</image></proof_images>",
    )
    parcel = await _get(body)
    assert parcel["events"][0]["proof_images"] == {"image": ["a", "b"]}


async def test_three_repeated_children_become_one_list():
    body = found_xml(events=[row("DELIVERED", "2026-04-07 14:30:00")]).replace(
        b"<remark></remark>",
        b"<proof_images><i>a</i><i>b</i><i>c</i></proof_images>",
    )
    assert (await _get(body))["events"][0]["proof_images"] == {"i": ["a", "b", "c"]}


@pytest.mark.parametrize("status", [403, 500, 503])
async def test_http_errors_raise_with_status(status):
    with pytest.raises(EmileApiError) as err:
        await _get(b"", status=status)
    assert err.value.status_code == status


async def test_429_carries_retry_after_seconds():
    with pytest.raises(EmileApiError) as err:
        await _get(b"", status=429, headers={"Retry-After": "120"})
    assert err.value.status_code == 429
    assert err.value.retry_after == 120.0


async def test_429_without_or_with_date_retry_after():
    with pytest.raises(EmileApiError) as err:
        await _get(b"", status=429)
    assert err.value.retry_after is None
    with pytest.raises(EmileApiError) as err:
        await _get(
            b"", status=429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
        )
    assert err.value.retry_after is None


async def test_timeout_becomes_an_api_error():
    session = MagicMock()
    session.post = MagicMock(side_effect=TimeoutError())
    with pytest.raises(EmileApiError) as err:
        await EmileApiClient(session).async_get_parcel(CODE)
    assert err.value.detail == "timeout"


async def test_network_error_propagates():
    session = MagicMock()
    session.post = MagicMock(side_effect=aiohttp.ClientError("boom"))
    with pytest.raises(aiohttp.ClientError):
        await EmileApiClient(session).async_get_parcel(CODE)
