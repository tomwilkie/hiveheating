"""End-to-end tests for timeouts and transient Hive server failures.

A real aiohttp server on localhost stands in for beekeeper.hivehome.com and
answers each request with a scripted outcome: the fixture data, a hang past
the request timeout, a dropped connection, an HTTP 5xx, a truncated body or
a refusal from Hive's API gateway. The session is driven the way the Home
Assistant fork's coordinator drives it: ``get_nodes()`` on every refresh and
a ``set_*`` call for a command, each raising when the request fails.
"""

# pylint: disable=redefined-outer-name

import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestServer

from hiveheating import Hive, LoginResult
from hiveheating.exceptions import HiveApiError, HiveConnectionError
from hiveheating.nodes import parse_nodes

_DATA = json.loads(
    (Path(__file__).parents[2] / "src/hiveheating/data/data.json").read_text()
)
_HEATING_ID = "heating-0000-0000-0000-000000000001"
_HOTWATER_ID = "hotwater-0000-0000-0000-000000000001"
# The products commands are sent to, parsed without a request.
_PRODUCTS = parse_nodes(_DATA).products
# Short enough to keep the suite fast, long enough not to trip on a slow CI box.
_TEST_TIMEOUT = ClientTimeout(total=0.5, connect=0.5)
_COGNITO_RESULT = {
    "AuthenticationResult": {
        "IdToken": "id-token",
        "RefreshToken": "refresh-token",
        "AccessToken": "access-token",
        "ExpiresIn": 3600,
    }
}
# The outcomes for the requests of one failed poll or command. aiohttp
# resends a GET once by itself when a kept-alive connection is dropped, so a
# dropped poll drops that resend too (see TestDroppedConnection).
_FAULTS = [
    pytest.param(["timeout"], id="timeout"),
    pytest.param(["disconnect", "disconnect"], id="dropped-connection"),
    pytest.param([500], id="http-500"),
    pytest.param([502], id="http-502"),
    pytest.param([503], id="http-503"),
    pytest.param([504], id="http-504"),
    pytest.param(["truncated"], id="truncated-body"),
    pytest.param(["forbidden"], id="gateway-block"),
]
# What Hive's API gateway answers, as observed on the live API: a bad token
# gets a 401, and after a burst of about 25 requests every request gets a bare
# 403 "Forbidden" for a minute or two.
_GATEWAY_REFUSALS = {
    "unauthorized": (401, "UnauthorizedException", '{"message":"Unauthorized"}'),
    "forbidden": (403, "ForbiddenException", '{"message":"Forbidden"}'),
}


class _FakeHive:
    """Scripted stand-in for the Hive API.

    Each request takes the next outcome from its queue and answers with the
    current ``nodes`` once the queue is empty.
    """

    def __init__(self):
        self.nodes = copy.deepcopy(_DATA)
        self.poll_outcomes = []
        self.command_outcomes = []
        self.polls = 0
        self.commands = []
        self._release = asyncio.Event()
        app = web.Application()
        app.router.add_get("/1.0/nodes/all", self._get_all)
        app.router.add_post("/1.0/nodes/{type}/{id}", self._set_state)
        self.server = TestServer(app)

    def set_temperature(self, product_id, value):
        product = next(p for p in self.nodes["products"] if p["id"] == product_id)
        product["props"]["temperature"] = value

    async def _get_all(self, request):
        self.polls += 1
        outcome = self.poll_outcomes.pop(0) if self.poll_outcomes else "ok"
        return await self._respond(request, outcome, json.dumps(self.nodes))

    async def _set_state(self, request):
        self.commands.append(await request.json())
        outcome = self.command_outcomes.pop(0) if self.command_outcomes else "ok"
        return await self._respond(request, outcome, "{}")

    async def _respond(self, request, outcome, body):
        if outcome == "timeout":
            # Hang until the test ends; the client gives up first.
            await self._release.wait()
        if outcome == "disconnect":
            request.transport.close()
        if outcome in _GATEWAY_REFUSALS:
            status, error_type, text = _GATEWAY_REFUSALS[outcome]
            return web.Response(
                status=status,
                text=text,
                content_type="application/json",
                headers={"x-amzn-ErrorType": error_type},
            )
        if isinstance(outcome, int):
            return web.Response(status=outcome, text="Service Unavailable")
        if outcome == "truncated":
            return web.Response(text=body[: len(body) // 2])
        return web.Response(text=body)

    async def close(self):
        self._release.set()
        await self.server.close()


@pytest.fixture
async def fake_hive(monkeypatch):
    """A running fake Hive API with the library pointed at it."""
    server = _FakeHive()
    await server.server.start_server()
    base = str(server.server.make_url("/1.0"))
    monkeypatch.setattr("hiveheating.api._BASE_URL", base)
    monkeypatch.setattr("hiveheating.api._TIMEOUT", _TEST_TIMEOUT)
    monkeypatch.setattr("hiveheating.api._RETRY_DELAY", (0, 0))
    yield server
    await server.close()


@pytest.fixture
async def websession():
    """The shared aiohttp session Home Assistant passes to the library."""
    async with ClientSession() as session:
        yield session


@pytest.fixture
def session(websession):
    """A started live-mode session; tests take ``fake_hive`` to point it there.

    Cognito is mocked: the refresh before the first request succeeds and a
    login does nothing.
    """
    hive = Hive(websession)
    hive.auth.refresh_token = AsyncMock(return_value=LoginResult(_COGNITO_RESULT))
    hive._login = AsyncMock()
    hive.new_session(
        {
            "username": "user@example.com",
            "password": "password",  # pragma: allowlist secret
            "tokens": {
                "AuthenticationResult": {
                    "IdToken": "id-token",
                    "RefreshToken": "refresh-token",
                }
            },
        }
    )
    return hive


def _temperature(nodes):
    return nodes.products[_HEATING_ID].temperature


class TestPollFailures:
    """A refresh fails; the caller gets the error and the next one succeeds."""

    @pytest.mark.parametrize("fault", _FAULTS)
    async def test_a_failed_poll_raises_a_connection_error_without_logging_in(
        self, session, fake_hive, fault
    ):
        # The fork's coordinator turns this into UpdateFailed and tries again
        # at the next interval; nothing is kept or retried in the library.
        fake_hive.poll_outcomes = list(fault)
        with pytest.raises(HiveConnectionError):
            await session.get_nodes()
        session._login.assert_not_awaited()
        fake_hive.set_temperature(_HEATING_ID, 23.5)
        assert _temperature(await session.get_nodes()) == 23.5
        session.auth.refresh_token.assert_awaited_once()

    async def test_a_blocked_poll_names_the_gateway_refusal(self, session, fake_hive):
        fake_hive.poll_outcomes = ["forbidden"]
        with pytest.raises(HiveConnectionError, match=r"\(ForbiddenException\)"):
            await session.get_nodes()
        session._login.assert_not_awaited()

    async def test_a_hung_request_gives_up_at_the_timeout(self, session, fake_hive):
        fake_hive.poll_outcomes = ["timeout"]
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(HiveConnectionError):
            await session.get_nodes()
        assert loop.time() - started < _TEST_TIMEOUT.total + 0.5

    async def test_cognito_unreachable_fails_the_poll_before_it_reaches_hive(
        self, session, fake_hive
    ):
        session.auth.refresh_token.side_effect = [
            HiveConnectionError("Cognito timed out"),
            LoginResult(_COGNITO_RESULT),
        ]
        with pytest.raises(HiveConnectionError, match="Cognito timed out"):
            await session.get_nodes()
        assert fake_hive.polls == 0
        await session.get_nodes()
        assert fake_hive.polls == 1
        session._login.assert_not_awaited()


class TestDroppedConnection:
    """Hive drops a kept-alive connection once."""

    async def test_aiohttp_resends_the_poll_and_it_succeeds(self, session, fake_hive):
        fake_hive.poll_outcomes = ["disconnect"]
        fake_hive.set_temperature(_HEATING_ID, 21.5)
        assert _temperature(await session.get_nodes()) == 21.5
        assert fake_hive.polls == 2

    async def test_the_library_resends_the_command_and_it_succeeds(
        self, session, fake_hive
    ):
        # aiohttp does not resend a POST; the library's retry does.
        fake_hive.command_outcomes = ["disconnect"]
        await session.set_target_temperature(_PRODUCTS[_HEATING_ID], 21.0)
        assert fake_hive.commands == [{"target": 21.0}, {"target": 21.0}]


class TestCommandFailures:
    """Commands sent while Hive is failing or refusing them."""

    @pytest.mark.parametrize("fault", _FAULTS)
    async def test_a_failed_command_raises_a_connection_error(
        self, session, fake_hive, fault
    ):
        # The fork turns any HiveError from a command into HomeAssistantError.
        # The fault repeats for every attempt the library makes.
        fake_hive.command_outcomes = list(fault) * 3
        with pytest.raises(HiveConnectionError):
            await session.set_target_temperature(_PRODUCTS[_HEATING_ID], 21.0)
        assert fake_hive.polls == 0
        session._login.assert_not_awaited()

    async def test_a_blocked_command_raises_without_logging_in(
        self, session, fake_hive
    ):
        # ha-hive#4: hot water commands got a 403 while the token worked.
        fake_hive.command_outcomes = ["forbidden"]
        with pytest.raises(
            HiveConnectionError, match=r"HTTP 403 \(ForbiddenException\)"
        ):
            await session.set_mode(_PRODUCTS[_HOTWATER_ID], "MANUAL")
        assert fake_hive.commands == [{"mode": "MANUAL"}]
        session._login.assert_not_awaited()

    async def test_a_stalled_404_is_resent_once(self, session, fake_hive):
        # hiveheating#28: Hive answered a write to a node that exists with a
        # 404 after a 10-second stall, and the same write worked later.
        fake_hive.command_outcomes = [404]
        await session.set_mode(_PRODUCTS[_HEATING_ID], "MANUAL")
        assert fake_hive.commands == [{"mode": "MANUAL"}, {"mode": "MANUAL"}]

        fake_hive.commands.clear()
        fake_hive.command_outcomes = [404, 404]
        with pytest.raises(HiveApiError, match="HTTP 404"):
            await session.set_mode(_PRODUCTS[_HEATING_ID], "MANUAL")
        assert fake_hive.commands == [{"mode": "MANUAL"}, {"mode": "MANUAL"}]

    async def test_a_rejected_token_logs_in_once_and_resends(self, session, fake_hive):
        fake_hive.command_outcomes = ["unauthorized"]
        await session.set_mode(_PRODUCTS[_HOTWATER_ID], "MANUAL")
        session._login.assert_awaited_once()
        assert fake_hive.commands == [{"mode": "MANUAL"}, {"mode": "MANUAL"}]
