"""The Hive session: login, token refresh and the Hive API calls."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hiveheating.api import request
from hiveheating.auth import HiveAuth
from hiveheating.exceptions import (
    HiveApiError,
    HiveConnectionError,
    HiveReauthRequired,
)
from hiveheating.helpers import sanitize_payload
from hiveheating.nodes import Nodes, parse_nodes

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from aiohttp import ClientSession

    from hiveheating.nodes import HeatingProduct, HotWaterProduct, Mode, Product

_LOGGER = logging.getLogger(__name__)

_FIXTURE = Path(__file__).parent / "data" / "data.json"
_ALL_NODES_PATH = "/nodes/all?products=true&devices=true"

# The username that switches to the bundled fixture instead of Hive.
FILE_USERNAME = "use@file.com"

# The Cognito tokens the session keeps, and the names it keeps them under.
_TOKEN_KEYS = {"IdToken": "token", "RefreshToken": "refreshToken"}

type JsonValue = str | int | float | bool | None

# Tokens are refreshed once this share of their lifetime has passed.
_REFRESH_THRESHOLD = 0.90

# device_data holds device_group_key, device_key and device_password. Entries
# saved by older versions add the token issue time, which is ignored.
_DEVICE_DATA_FIELDS = 3


def _epoch() -> datetime:
    return datetime.min.replace(tzinfo=UTC)


@dataclass
class SessionTokens:
    """The session's API tokens and when they were issued."""

    token_data: dict[str, str] = field(default_factory=dict)
    token_created: datetime = field(default_factory=_epoch)
    token_expiry: timedelta = field(default_factory=lambda: timedelta(seconds=3600))


def _load_fixture() -> dict[str, Any]:
    """Load the bundled all-nodes fixture used in file mode."""
    result: dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    return result


class Hive:
    """A Hive account session.

    Create one per account and call :meth:`new_session` with the saved
    tokens and device credentials. Then :meth:`get_nodes` fetches Hive's
    ``GET /nodes/all``, and the ``set_*`` commands each send a
    ``POST /nodes/{type}/{id}``, repeated after a transient failure. The
    session keeps no device state and has no timer: the caller decides when
    to poll.

    Every error raised is a :class:`~hiveheating.exceptions.HiveError`:
    :class:`~hiveheating.exceptions.HiveReauthRequired` when the user has to
    log in again, :class:`~hiveheating.exceptions.HiveConnectionError` for
    transient failures and :class:`~hiveheating.exceptions.HiveApiError` when
    Hive refuses a request.
    """

    def __init__(self, websession: ClientSession) -> None:
        """Set up the session.

        Args:
            websession: aiohttp session for every Hive and Cognito call. Home
                Assistant passes its shared session; the caller closes it.
        """
        self.auth = HiveAuth("", "", websession=websession)
        self.websession = websession
        self.tokens = SessionTokens()
        # Set by new_session for the magic testing username.
        self._file = False
        self._refresh_lock = asyncio.Lock()
        # The login failure that needs the user, kept so later calls fail fast.
        self._reauth_error: HiveReauthRequired | None = None

    # --- Tokens and login --------------------------------------------------

    def update_tokens(
        self, tokens: dict[str, Any], *, update_expiry_time: bool = True
    ) -> None:
        """Store the tokens from a Cognito response.

        Args:
            tokens: A Cognito response, from a login or refresh or saved in
                the config entry. One without an ``AuthenticationResult``
                changes nothing.
            update_expiry_time: Whether the tokens count as freshly issued.
        """
        _LOGGER.debug("update_tokens - Input tokens: %s", sanitize_payload(tokens))
        data = tokens.get("AuthenticationResult") or {}
        stored = {
            target: data[source]
            for source, target in _TOKEN_KEYS.items()
            if source in data
        }
        self.tokens.token_data.update(stored)
        if stored:
            self._reauth_error = None
        if stored and update_expiry_time:
            self.tokens.token_created = datetime.now(UTC)
        if "ExpiresIn" in data:
            self.tokens.token_expiry = timedelta(seconds=data["ExpiresIn"])
        _LOGGER.debug(
            "Tokens updated, they expire at %s",
            self.tokens.token_created + self.tokens.token_expiry,
        )

    def _raise_if_reauth_required(self) -> None:
        """Fail fast once a login has shown that the user has to log in again.

        Logging in again would not help: Hive would send another SMS code, or
        count another wrong password towards locking the account. New tokens
        from :meth:`update_tokens` clear the failure.

        Raises:
            HiveReauthRequired: The earlier login failure.
        """
        if self._reauth_error is not None:
            raise self._reauth_error.with_traceback(None)

    async def _login(self) -> None:
        """Log in with the stored username, password and device credentials.

        After a login fails with :class:`HiveReauthRequired`, later calls raise
        it again without contacting Cognito until new tokens are stored.

        Raises:
            HiveReauthRequired: Hive wants an SMS code, no longer trusts the
                saved device, or rejected the username or password.
            HiveConnectionError: Cognito could not be reached.
            HiveApiError: Cognito asked for a challenge this library cannot answer.
        """
        self._raise_if_reauth_required()
        try:
            result = await self.auth.login()
            if result.device_challenge:
                result = await self.auth.device_login()
        except HiveReauthRequired as err:
            self._reauth_error = err
            raise
        if result.sms_required:
            self._reauth_error = HiveReauthRequired("Hive asked for an SMS code")
            raise self._reauth_error
        if not result.complete:
            raise HiveApiError(f"Login did not complete: {result.challenge}")
        self.update_tokens(result.response)

    def _refresh_due(self) -> bool:
        """Return True once the tokens have used most of their lifetime."""
        expiry = (
            self.tokens.token_created + self.tokens.token_expiry * _REFRESH_THRESHOLD
        )
        return datetime.now(UTC) >= expiry

    async def _refresh_tokens(self) -> None:
        """Refresh the tokens once most of their lifetime has passed.

        Logs in again with the saved device credentials when Cognito rejects
        the refresh token.

        Raises:
            HiveReauthRequired: The user has to log in again.
            HiveConnectionError: Cognito could not be reached.
            HiveApiError: Cognito refused the refresh for another reason.
        """
        if self._file or not self._refresh_due():
            return
        async with self._refresh_lock:
            # Another caller may have refreshed while this one waited.
            if not self._refresh_due():
                return
            try:
                result = await self.auth.refresh_token(
                    self.tokens.token_data.get("refreshToken")
                )
            except HiveReauthRequired as err:
                _LOGGER.debug("Token refresh rejected (%s), logging in again", err)
                await self._login()
                return
            self.update_tokens(result.response)

    async def _api_call[T](self, call: Callable[[], Awaitable[T]]) -> T:
        """Call the API with fresh tokens, logging in again if Hive rejects them.

        Callers whose tokens are rejected at the same time log in once: the
        login is serialised through the refresh lock and skipped when another
        caller has already replaced the tokens. Once a login has shown that the
        user has to log in again, every call fails with that error.
        """
        self._raise_if_reauth_required()
        await self._refresh_tokens()
        issued = self.tokens.token_created
        try:
            return await call()
        except HiveReauthRequired as err:
            async with self._refresh_lock:
                if self.tokens.token_created == issued:
                    _LOGGER.debug("Hive rejected the token (%s), logging in again", err)
                    await self._login()
            return await call()

    # --- Commands ----------------------------------------------------------

    async def set_mode(self, product: Product, mode: Mode) -> None:
        """Set a product's mode: ``{"mode": mode}``.

        Ending a boost is setting the mode it returns to, which Hive reports
        as the product's ``previous_mode``.

        Raises:
            HiveReauthRequired: The user has to log in again.
            HiveConnectionError: Hive could not be reached.
            HiveApiError: Hive refused the change.
        """
        await self._set_state(product, mode=mode)

    async def set_target_temperature(
        self, product: HeatingProduct, target: float
    ) -> None:
        """Set a heating target temperature: ``{"target": target}``.

        Raises:
            HiveReauthRequired: The user has to log in again.
            HiveConnectionError: Hive could not be reached.
            HiveApiError: Hive refused the change.
        """
        await self._set_state(product, target=target)

    async def set_heating_boost(
        self, product: HeatingProduct, minutes: int, target: float
    ) -> None:
        """Boost heating to *target* for *minutes*.

        Sends ``{"mode": "BOOST", "boost": minutes, "target": target}``.

        Raises:
            HiveReauthRequired: The user has to log in again.
            HiveConnectionError: Hive could not be reached.
            HiveApiError: Hive refused the change.
        """
        await self._set_state(product, mode="BOOST", boost=minutes, target=target)

    async def set_hot_water_boost(self, product: HotWaterProduct, minutes: int) -> None:
        """Boost the hot water for *minutes*: ``{"mode": "BOOST", "boost": minutes}``.

        Raises:
            HiveReauthRequired: The user has to log in again.
            HiveConnectionError: Hive could not be reached.
            HiveApiError: Hive refused the change.
        """
        await self._set_state(product, mode="BOOST", boost=minutes)

    async def _set_state(self, product: Product, **state: JsonValue) -> None:
        """Post a product's new state to Hive. Nothing is sent in file mode.

        The body sets absolute values, so sending it twice is safe and a
        transient failure is retried.
        """
        if self._file:
            return
        _LOGGER.debug("Setting %s %s: %s", product.type, product.id, state)
        path = f"/nodes/{product.type}/{product.id}"
        await self._api_call(lambda: self._request("post", path, json.dumps(state)))

    async def _request(
        self, method: str, path: str, data: str | None = None
    ) -> dict[str, Any]:
        """Make a Hive API request with the session's token."""
        return await request(
            self.websession,
            method,
            path,
            token=self.tokens.token_data.get("token"),
            data=data,
        )

    # --- Nodes -------------------------------------------------------------

    async def get_nodes(self) -> Nodes:
        """Fetch every product and device: Hive's ``GET /nodes/all``.

        In file mode the bundled fixture is read instead.

        Returns:
            The snapshot, with only the node types this library supports.

        Raises:
            HiveReauthRequired: The user has to log in again.
            HiveConnectionError: Hive could not be reached, or returned no
                products or no devices.
            HiveApiError: Hive refused the request.
        """
        if self._file:
            response = await asyncio.to_thread(_load_fixture)
        else:
            response = await self._api_call(
                lambda: self._request("get", _ALL_NODES_PATH)
            )
        products, devices = response.get("products"), response.get("devices")
        if not products or not devices:
            # Every account has at least its hub, so an empty answer is a
            # fault on Hive's side, not an account without devices.
            raise HiveConnectionError("Hive returned no products or no devices")
        nodes = parse_nodes(response)
        _LOGGER.debug(
            "Hive returned %d products and %d devices",
            len(nodes.products),
            len(nodes.devices),
        )
        return nodes

    # --- New session -------------------------------------------------------

    def _apply_device_data(self, device_data: Sequence[Any]) -> None:
        """Load saved device credentials into the session.

        Args:
            device_data: ``(device_group_key, device_key, device_password)``.
                Any further values, as older versions saved, are ignored.

        Raises:
            HiveReauthRequired: Fewer than three values were given.
        """
        if len(device_data) < _DEVICE_DATA_FIELDS:
            raise HiveReauthRequired(
                "device_data must hold device_group_key, device_key and device_password"
            )
        self.auth.device_group_key = device_data[0]
        self.auth.device_key = device_data[1]
        self.auth.device_password = device_data[2]

    def new_session(self, config: dict[str, Any] | None = None) -> None:
        """Load the credentials, tokens and device data from a config entry.

        Nothing is sent to Hive or Cognito; the first :meth:`get_nodes` is.

        Args:
            config: ``username``, ``password``, ``tokens`` and ``device_data``
                from the Home Assistant config entry.

        Raises:
            HiveReauthRequired: The config has no tokens, or its device data
                is incomplete.
        """
        config = config or {}
        _LOGGER.debug("new_session - Config: %s", sanitize_payload(config))
        if config.get("username") == FILE_USERNAME:
            self._file = True
        if self._file:
            return
        if "tokens" not in config:
            raise HiveReauthRequired("The config has no tokens")
        self.update_tokens(config["tokens"], update_expiry_time=False)
        if "username" in config:
            self.auth.username = config["username"]
        if "password" in config:
            self.auth.password = config["password"]
        if "device_data" in config:
            self._apply_device_data(config["device_data"])
