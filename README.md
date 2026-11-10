# hiveheating

![CI](https://github.com/tomwilkie/hiveheating/actions/workflows/ci.yml/badge.svg) ![PyPI](https://img.shields.io/pypi/v/hiveheating) ![License](https://img.shields.io/github/license/tomwilkie/hiveheating)

An async Python library for the [Hive](https://www.hivehome.com/) smart home platform. It exists to power [tomwilkie/ha-hive](https://github.com/tomwilkie/ha-hive), a fork of the Hive integration for [Home Assistant](https://www.home-assistant.io/), and exposes exactly what that integration needs: Hive heating and hot water.

> This library is a hard fork of [Pyhass/Pyhive](https://github.com/Pyhass/Pyhive), cut down to what the ha-hive integration needs and restarted at version 0.0.1. It was called `pyhive-integration` until v0.1.5 and `apyhiveapi` until v0.2.1; since v0.3.0 it is `hiveheating`, on PyPI and as the import name. Upstream's `pyhive-integration`, which Home Assistant ships for its built-in Hive integration, installs its own `apyhiveapi` and `pyhiveapi` packages, and sharing a folder with them broke imports.

---

## Features

- Async only, built on `aiohttp`
- AWS Cognito SRP authentication with SMS two-factor authentication support
- Automatic token refresh at 90% of token lifetime, with a silent re-login when the refresh token is rejected
- Mirrors Hive's own API: `get_nodes()` is `GET /nodes/all`, and each command (`set_mode`, `set_target_temperature`, `set_heating_boost`, `set_hot_water_boost`) is one `POST /nodes/{type}/{id}`. There is no polling timer and no cached state; the caller decides when to fetch
- Typed, frozen snapshots of heating and hot water products and devices, using Hive's own field names and values
- File-based offline mode for development and testing without live credentials

## Supported Devices

| Device Type | Capabilities |
| --- | --- |
| **Heating** (thermostat) | Current / target temperature, mode (schedule / manual / off), boost on/off |
| **Hot Water** | Mode (schedule / on / off), boost on/off |

Lights, smart plugs, sensors and the hub are not supported: the ha-hive integration does not use them.

---

## Installation

From PyPI, or the current `main`:

```bash
pip install hiveheating
pip install "hiveheating @ git+https://github.com/tomwilkie/hiveheating"
```

Every release is also attached to its [GitHub Release](https://github.com/tomwilkie/hiveheating/releases) as a wheel and sdist.

Requires Python 3.13+.

---

## Quick Start

```python
import asyncio
import getpass

from aiohttp import ClientSession
from hiveheating import Auth, Hive


async def main():
    username = input("Hive email: ")
    password = getpass.getpass("Hive password: ")

    async with ClientSession() as websession:
        auth = Auth(username=username, password=password, websession=websession)
        result = await auth.login()
        if result.sms_required:
            result = await auth.sms_2fa(input("SMS code: "), result)
            await auth.device_registration()

        hive = Hive(websession)
        hive.new_session(
            {
                "username": username,
                "password": password,
                "tokens": result.response,
                "device_data": auth.get_device_data(),
            }
        )
        nodes = await hive.get_nodes()
        for product in nodes.products.values():
            print(product.type, product.name, product.mode)


asyncio.run(main())
```

---

## Authentication

Authentication uses the AWS Cognito SRP flow. `login()`, `sms_2fa()` and
`device_login()` return a `LoginResult`. On the first login its `sms_required` is true
when the account has SMS two-factor authentication enabled; pass the code and the
result to `sms_2fa(code, result)`. Then call `device_registration()` to register the
machine as a trusted device and save the credentials `get_device_data()` returns, so
later sessions can log in without an SMS. Save `result.response` as the session's
`tokens`.

> **Note:** Only the Hive account owner is supported. Guest accounts cannot be used.

### Standalone login example

[`examples/example_login.py`](examples/example_login.py) is a complete runnable
script. The first run asks for your email, password and SMS code, registers the
machine as a trusted device and saves the device credentials to
`hive_device.json`. Later runs log in with that file and skip the SMS. Both runs
list the account's heating and hot water products and devices.

```bash
pip install hiveheating
python examples/example_login.py
```

---

## Reading and Controlling Devices

`new_session()` only loads the saved credentials; nothing is sent until the
first call. `get_nodes()` fetches `GET /nodes/all` and returns a `Nodes`
snapshot: `user`, `products` and `devices`, each keyed by Hive ID. Products are
`HeatingProduct` (`heating`) or `HotWaterProduct` (`hotwater`);
devices are `Device` (`hub`, `thermostatui`, `boilermodule`). Fields keep
Hive's names and values: a heating `mode` can be `BOOST`, a `previous_mode` can be
`False`. A value Hive left out or sent with an unexpected type is `None`.

Each command takes a product from the snapshot and sends one
`POST /nodes/{type}/{id}`:

| Command | Products | Body |
|---|---|---|
| `set_mode(product, mode)` | any | `{"mode": mode}`; `SCHEDULE`, `MANUAL` or `OFF` (`MANUAL` is "on" for hot water) |
| `set_target_temperature(product, target)` | `HeatingProduct` | `{"target": target}` |
| `set_heating_boost(product, minutes, target)` | `HeatingProduct` | `{"mode": "BOOST", "boost": minutes, "target": target}` |
| `set_hot_water_boost(product, minutes)` | `HotWaterProduct` | `{"mode": "BOOST", "boost": minutes}` |

There is no boost-off call: end a boost with `set_mode` and the mode it returns
to (the product's `previous_mode`, where `False` means `OFF`). The library does
not check argument values; Hive does.

```python
nodes = await hive.get_nodes()
heating = nodes.products["<heating id>"]
print(heating.temperature, heating.target, heating.mode, heating.online)

await hive.set_target_temperature(heating, 21.0)
await hive.set_heating_boost(heating, 30, 22.0)
await hive.set_mode(heating, "SCHEDULE")
```

---

## Errors

Every exception the library raises is a subclass of `HiveError`, grouped by what the
caller should do about it. aiohttp and botocore exceptions never escape the library.

| Exception | Meaning | What to do |
|---|---|---|
| `HiveConnectionError` | Hive or Cognito could not be reached, timed out, failed with a 5xx, sent an unreadable response, or returned no products or no devices | Retry later |
| `HiveApiError` | Hive or Cognito refused the request or answered unexpectedly | Report it; retrying will not help |
| `HiveReauthRequired` | The tokens, refresh token or saved device credentials were rejected, there are no tokens, or Hive wants an SMS code. The session then keeps raising it without contacting Hive until it is given new tokens | Log in again |
| `HiveInvalidUsername`, `HiveInvalidPassword`, `HiveInvalid2FACode` | Subclasses of `HiveReauthRequired` raised by the login flow | Show the user a form error |

`get_nodes()` and the commands raise on every failure and keep nothing between
calls. A command is retried inside the library after a timeout, a network
error, an HTTP 5xx or a single HTTP 404, for up to 30 seconds, because each one
sets absolute values and is safe to send twice. Hive's temporary block of a
burst of requests is not retried. Retrying a failed poll is up to the caller.

---

## Offline / File-Based Testing

Pass `username="use@file.com"` in the `new_session` config to load device state from the bundled JSON fixture in `src/hiveheating/data/` instead of making live API calls. Useful for development without real Hive credentials.

```python
async with ClientSession() as websession:
    hive = Hive(websession)
    hive.new_session({"username": "use@file.com"})
    nodes = await hive.get_nodes()
```

---

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run linters
pre-commit run --all-files

# Run tests
pytest tests/
```

---

## Links

- [ha-hive](https://github.com/tomwilkie/ha-hive), the Home Assistant integration this library powers
- [Releases](https://github.com/tomwilkie/hiveheating/releases)
- [Source](https://github.com/tomwilkie/hiveheating)
- [Issue Tracker](https://github.com/tomwilkie/hiveheating/issues)

---

## License

MIT License — see [LICENSE](https://github.com/tomwilkie/hiveheating/blob/main/LICENSE) for details. The original work is copyright Pyhive; the changes in this fork are copyright Tom Wilkie.
