"""HTTP calls to the Hive REST API.

Every aiohttp error is translated here into one of the library's exceptions,
so the rest of the package only ever sees :class:`~hiveheating.exceptions.HiveError`
subclasses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from functools import partial
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

from aiohttp import ClientError, ClientSession, ClientTimeout

from hiveheating.exceptions import (
    HiveApiError,
    HiveConnectionError,
    HiveError,
    HiveReauthRequired,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from aiohttp import ClientResponse

_LOGGER = logging.getLogger(__name__)

# Per-request budget. The total has to sit above Hive's slow days, which can
# reach several seconds for the all-nodes response, and below the scan
# interval; the connect limit makes an unreachable Hive fail fast.
_TIMEOUT = ClientTimeout(total=20, connect=10)
# Retries of requests that are safe to repeat: at most this many attempts, all
# started within the deadline, with a random pause between them. The deadline
# leaves room for a retry after Hive's 10-second stall ending in a 404 while
# keeping a failing command to about 30 seconds.
_MAX_ATTEMPTS = 3
_RETRY_DEADLINE = 30.0
_RETRY_DELAY = (0.5, 1.5)
# A retry is not started with less than this left before the deadline.
_MIN_ATTEMPT_TIME = 2.0
_RANDOM = secrets.SystemRandom()
_BASE_URL = "https://beekeeper.hivehome.com/1.0"
_HEADERS = {
    "content-type": "application/json",
    "Accept": "*/*",
    "User-Agent": "Hive/12.04.0 iOS/18.3.1 Apple",
}


def _prepare(
    websession: ClientSession,
    method: str,
    url: str,
    token: str | None,
    data: str | None,
) -> Callable[..., AbstractAsyncContextManager[ClientResponse]]:
    """Return the request, to be called with its timeout."""
    headers = {**_HEADERS, "Authorization": token} if token else None
    return partial(websession.request, method, url, headers=headers, data=data)


async def _send(
    send: Callable[[], AbstractAsyncContextManager[ClientResponse]],
    method: str,
    url: str,
) -> tuple[int, str, str | None]:
    """Make one HTTP request and return its status, body and gateway error type.

    Raises:
        HiveConnectionError: Network error or timeout.
    """
    start = time.monotonic()
    try:
        async with send() as resp:
            body = await resp.text()
            status = resp.status
            error_type = resp.headers.get("x-amzn-ErrorType")
    except TimeoutError as err:
        raise HiveConnectionError(f"Timed out calling {url}") from err
    except (ClientError, OSError) as err:
        raise HiveConnectionError(f"Could not reach {url}: {err}") from err
    _LOGGER.debug(
        "%s %s answered HTTP %s in %.2fs",
        method.upper(),
        url,
        status,
        time.monotonic() - start,
    )
    return status, body, error_type


def _status_error(
    url: str, token: str | None, status: int, body: str, error_type: str | None
) -> HiveError | None:
    """Return the error for a non-2xx response, or None for a 2xx one."""
    if HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
        return None
    # API Gateway names the kind of refusal, e.g. UnauthorizedException or
    # ForbiddenException, sometimes followed by ":<documentation URL>".
    error_name = error_type.split(":")[0] if error_type else None
    kind = f" ({error_name})" if error_name else ""
    detail = f"HTTP {status}{kind} calling {url}: {body[:200]}"
    if token and status == HTTPStatus.UNAUTHORIZED:
        return HiveReauthRequired(f"Hive rejected the token: {detail}")
    if status == HTTPStatus.FORBIDDEN and error_name == "ForbiddenException":
        return HiveConnectionError(
            f"Hive is temporarily blocking requests, try again in a few minutes: "
            f"{detail}"
        )
    if status >= HTTPStatus.INTERNAL_SERVER_ERROR:
        return HiveConnectionError(f"Hive server error: {detail}")
    return HiveApiError(f"Hive refused the request: {detail}")


async def _fetch(
    websession: ClientSession,
    method: str,
    url: str,
    *,
    token: str | None = None,
    data: str | None = None,
) -> str:
    """Make one HTTP request and return the body of a 2xx response.

    Args:
        websession: The aiohttp session to use.
        method: HTTP method.
        url: URL to call.
        token: API token to send. When one is sent, an HTTP 401 means Hive
            rejected it.
        data: Request body.

    Raises:
        HiveReauthRequired: Hive rejected the token (HTTP 401 on a request
            that sent one).
        HiveConnectionError: Network error, timeout, HTTP 5xx, or HTTP 403
            ``ForbiddenException``: Hive's API gateway blocks every request
            from a connection for a minute or two after a burst of about 25.
        HiveApiError: Any other non-2xx status, including any other HTTP
            403: Hive's API gateway answers a bad token with 401, and a 403
            means it refused the request itself (for example an unknown path).
    """
    send = _prepare(websession, method, url, token, data)
    status, body, error_type = await _send(partial(send, timeout=_TIMEOUT), method, url)
    if error := _status_error(url, token, status, body, error_type):
        raise error
    return body


def _timeout_until(deadline: float) -> ClientTimeout:
    """Return the request timeout, cut short so it ends by *deadline*."""
    remaining = deadline - time.monotonic()
    return ClientTimeout(
        total=min(_TIMEOUT.total or remaining, remaining), connect=_TIMEOUT.connect
    )


async def _fetch_with_retry(
    websession: ClientSession,
    method: str,
    url: str,
    *,
    token: str | None,
    data: str | None,
) -> str:
    """Make a request that is safe to repeat, retrying transient failures.

    Retries a network error, a timeout or an HTTP 5xx, and an HTTP 404 once:
    Hive has answered a write to a node that exists with a 404 after stalling
    for about 10 seconds. Hive's gateway block (HTTP 403
    ``ForbiddenException``) is not retried, because it lasts a minute or two
    and every retry adds to the burst that caused it. Gives up after
    ``_MAX_ATTEMPTS`` attempts or once ``_RETRY_DEADLINE`` would pass, and
    raises the last failure.

    Raises:
        The same exceptions as :func:`_fetch`.
    """
    send = _prepare(websession, method, url, token, data)
    deadline = time.monotonic() + _RETRY_DEADLINE
    retried_not_found = False
    attempt = 1
    while True:
        error: HiveError | None
        try:
            status, body, error_type = await _send(
                partial(send, timeout=_timeout_until(deadline)), method, url
            )
        except HiveConnectionError as err:
            error = err
        else:
            error = _status_error(url, token, status, body, error_type)
            if error is None:
                return body
            if not (
                status >= HTTPStatus.INTERNAL_SERVER_ERROR
                or (status == HTTPStatus.NOT_FOUND and not retried_not_found)
            ):
                raise error
            retried_not_found = retried_not_found or status == HTTPStatus.NOT_FOUND
        delay = _RANDOM.uniform(*_RETRY_DELAY)
        if (
            attempt >= _MAX_ATTEMPTS
            or time.monotonic() + delay + _MIN_ATTEMPT_TIME > deadline
        ):
            raise error
        attempt += 1
        _LOGGER.debug(
            "%s %s failed (%s), retrying in %.1fs: attempt %d of %d",
            method.upper(),
            url,
            error,
            delay,
            attempt,
            _MAX_ATTEMPTS,
        )
        await asyncio.sleep(delay)


async def request(
    websession: ClientSession,
    method: str,
    path: str,
    *,
    token: str | None,
    data: str | None = None,
) -> dict[str, Any]:
    """Make an authenticated Hive API request and return its JSON body.

    A POST is retried after a transient failure (see :func:`_fetch_with_retry`):
    every POST sets a node's state to absolute values, so it is safe to send
    twice. A GET is sent once; the caller polls again on its own interval.

    Args:
        websession: The aiohttp session to use.
        method: HTTP method.
        path: Path under the API's base URL, e.g. ``/nodes/all``.
        token: The session's API token.
        data: Request body.

    Returns:
        The decoded body, or ``{}`` when the response has none.

    Raises:
        HiveReauthRequired: There is no token, or Hive rejected it.
        HiveConnectionError: Hive could not be reached, failed, or sent an
            unreadable body.
        HiveApiError: Hive refused the request, or the body is not a JSON
            object.
    """
    if not token:
        raise HiveReauthRequired("The session has no API token")
    url = f"{_BASE_URL}{path}"
    fetch = _fetch_with_retry if method == "post" else _fetch
    body = await fetch(websession, method, url, token=token, data=data)
    if not body.strip():
        return {}
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as err:
        raise HiveConnectionError(
            f"Hive returned an unreadable body calling {url}"
        ) from err
    if not isinstance(parsed, dict):
        raise HiveApiError(
            f"Hive returned a JSON {type(parsed).__name__} calling {url}"
        )
    return parsed
