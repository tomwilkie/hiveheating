# CLAUDE.md

Guidance for Claude Code when working in this repository, locally and in
GitHub Actions. Both Claude jobs in `.github/workflows/claude.yml` (the
automatic PR review and `@claude` mentions) load this file, so project
knowledge and rules belong here, not in the workflow prompt.

This is hiveheating, the async Python library behind the Hive integration for
Home Assistant. It is a hard fork of Pyhass/Pyhive, restarted at version
0.0.1. It is published on PyPI and imported as `hiveheating`. It was
`pyhive-integration` (installed from GitHub release wheels) until v0.1.5 and
`apyhiveapi` until v0.2.1. Both clash with upstream: Home Assistant ships
upstream's `pyhive-integration` for its built-in Hive integration, and that
installs `apyhiveapi/` and `pyhiveapi/` into site-packages, where they would
mix with this library's files. Never reuse those two import names;
`make verify-dist` checks the wheel imports alongside upstream. It covers Hive
heating and hot water only. The source is one flat package in
`src/hiveheating/`: no sync package, no mixins, no deprecation shims and no
camelCase compatibility aliases. The public API is snake_case only.

## The Home Assistant fork is the only consumer

This library exists to power one client: the owner's fork of the Hive
integration, a custom component at `~/Documents/repos/ha-hive`
(`custom_components/hive/`, GitHub `tomwilkie/ha-hive`). Code the fork does
not use is removed rather than kept, and when the library's API changes the
fork is updated alongside it. A change that looks fine here can still be a
bug on the Home Assistant side.

**Always check the fork's source.** Whenever you review, change or answer a
question about this library's code, read the fork's files that call the code
involved before you conclude. Do not rely on memory of the integration: it
changes. In what you post, name the files you checked and quote the lines
that matter. If nothing in the fork uses the code involved, say so
explicitly: that code is a removal candidate.

- The fork (the actual caller): `~/Documents/repos/ha-hive/custom_components/hive/`
  with `__init__.py`, `config_flow.py`, `entity.py`, `const.py`, `climate.py`,
  `water_heater.py` and `manifest.json`, which pins this library. Once the
  fork is ported (see below), the manifest pins a release of this library
  (`hiveheating==<version>` from PyPI; up to v0.1.5 it installed a GitHub
  release wheel as `<wheel URL>#pyhive-integration==<version>`) and its
  light and switch platforms are gone. The fork's own `CLAUDE.md` lists the
  API it calls and the error mapping it applies; its tests mock `Hive` and
  `Auth` at the boundary, so a renamed method or a changed dataclass field
  is not caught there. In GitHub Actions, where the local checkout is not
  available, fetch the raw files from
  `https://raw.githubusercontent.com/tomwilkie/ha-hive/<ref>/custom_components/hive/<file>`,
  where `<ref>` is `main`, or `ci-and-tests` while the port below is unmerged.
- Upstream integration, for reference only:
  <https://github.com/home-assistant/core/tree/dev/homeassistant/components/hive>
- Developer docs:
  - Entities overview: <https://developers.home-assistant.io/docs/core/entity/>
  - climate: <https://developers.home-assistant.io/docs/core/entity/climate/>
  - water_heater: <https://developers.home-assistant.io/docs/core/entity/water-heater/>
  - Config flow (login, SMS 2FA, reauth): <https://developers.home-assistant.io/docs/config_entries_config_flow_handler/>
  - Setup failures (`ConfigEntryNotReady` / `ConfigEntryAuthFailed` / `ConfigEntryError`): <https://contributors.home-assistant.io/core/integrations/setup-failures/>
  - Blocking calls in the event loop: <https://developers.home-assistant.io/docs/asyncio_blocking_operations/>

The fork breaks when: a method it calls is renamed or removed, a dataclass
field it reads is renamed or changes type, a new exception is raised that it
doesn't catch, or something blocks the event loop. The library mirrors Hive's
own API and knows nothing about Home Assistant; the public API is:

- `Hive(websession)` with `new_session` (sync: loads the config entry's
  tokens and device credentials, sends nothing), `get_nodes` (Hive's
  `GET /nodes/all`), the commands `set_mode`, `set_target_temperature`,
  `set_heating_boost` and `set_hot_water_boost` (each one
  `POST /nodes/{type}/{id}` for a product from the snapshot); plus the
  `Mode` type. The websession is required and the caller closes it.
- `Auth(username=, password=, websession=)` (the `HiveAuth` class) with `login`, `sms_2fa`,
  `is_device_registered`, `device_registration`, `get_device_data` (sync; the
  fork stores whatever it returns as `device_data`) and `forget_device`.
  `login`, `sms_2fa`, `device_login` and `refresh_token` return a frozen
  `LoginResult`: Cognito's `response` (the fork saves it as `tokens`) and the
  properties `complete`, `sms_required`, `device_challenge`, `new_device` and
  `challenge`. `device_login` is used by `Hive._login`, not by the fork.
- the frozen dataclasses `get_nodes` returns (`Nodes`, `User`,
  `HeatingProduct`, `HotWaterProduct`, `Device`) and their fields, read as
  attributes; see "Nodes" below
- the exceptions exported from `hiveheating` (see "Error handling" below)

Everything that turns Hive's model into Home Assistant's lives in the fork:
its `DataUpdateCoordinator` calls `get_nodes()` on its own interval, and the
fork attaches products to their hardware (heating and hot water to the
thermostat in their zone), decides
availability, reports a `BOOST` mode as the mode it returns to, ends a boost
by setting its `previous_mode`, validates command arguments and names Hive's
`Receiver` `Heating`.

The fork's `main` pins `apyhiveapi==0.1.5`, the last release with the old
entity-style API (`start_session` returning `device_list`, `update_data`,
`hive.heating` / `hive.hotwater` and the `Device` entity record). Moving to
this API means porting the fork to a coordinator in step. The mapping the
fork applies is: `HiveReauthRequired` → `ConfigEntryAuthFailed`,
`HiveConnectionError` from the first refresh → `ConfigEntryNotReady`, any
other `HiveError` there → `ConfigEntryError`, and `UpdateFailed` for a
failed refresh after setup; `HiveError` from a command → `HomeAssistantError`
and an invalid argument → `ServiceValidationError`; in the config flow
`HiveInvalidUsername` / `HiveInvalidPassword` / `HiveInvalid2FACode` → their
form errors, `HiveConnectionError` → `no_internet_available`, any other
`HiveError` → `unknown`. An API change here is made in the fork in step.

### Dependency changes

Home Assistant installs this library inside its own environment, so judge
version ranges in `pyproject.toml` `dependencies` against what Home Assistant
pins *today*, not against the reasons given in older issues or PRs — those
constraints change. Before calling a range right or wrong, fetch:

- <https://raw.githubusercontent.com/home-assistant/core/dev/homeassistant/package_constraints.txt>
  (hard pins applied to every integration install, e.g. boto3/botocore)
- <https://raw.githubusercontent.com/home-assistant/core/dev/requirements_all.txt>
  (versions of other integrations' packages, e.g. aiobotocore)

A range that can't be satisfied alongside those pins is a bug: Home Assistant
fails with `RequirementsNotFound`. Quote the pins you checked. Home Assistant
requires Python 3.14; this library supports 3.13 and later.

## Project rules

- Edit the source in `src/hiveheating/` only; there is no generated code.
- Keep it flat and concrete: plain classes and functions, no mixins, no
  compatibility aliases or shims. When a name changes, rename it in the fork.
- Everything is fully annotated and `mypy --strict` passes on `src/`. The
  package ships `py.typed`, so the fork type-checks against these annotations
  too. Ruff runs with every rule enabled (`select = ["ALL"]`); the ignore
  lists in `pyproject.toml` are deliberate and short, so fix findings rather
  than adding `noqa` comments.
- The placeholder `aws_access_key_id` / `aws_secret_access_key` /
  `aws_session_token` strings in `auth.py` must stay; they are not leaked
  secrets and boto3 Cognito auth breaks without them.
- Never log tokens or PII; log payloads via `hiveheating.helpers.sanitize_payload`.
  Data in `src/hiveheating/data/*.json` must be anonymised.
- New behaviour and bug fixes need tests in `tests/unit` or `tests/e2e`; coverage must stay at 99%. One test per behaviour, not per
  call site: see "What a test is for" below before adding one.

### Error handling

Exceptions are classified by what the caller should do, not by where they
happened. Everything raised is a subclass of `HiveError` (`exceptions.py`):

| Exception | Meaning | Caller's reaction |
|---|---|---|
| `HiveConnectionError` | Hive or Cognito unreachable, timeout, HTTP 5xx, Cognito throttling, Hive's gateway blocking requests (HTTP 403 `ForbiddenException`, about 1–2 minutes after a burst of roughly 25 requests), unreadable body, a `/nodes/all` reply with no products or no devices | transient: retry later |
| `HiveApiError` | Hive or Cognito refused the request or answered unexpectedly | give up on this request |
| `HiveReauthRequired` | tokens, refresh token or saved device credentials rejected, no tokens, or an SMS code is needed; once a login has raised it, the session raises it again without contacting Cognito until new tokens are stored | the user must log in again |
| `HiveInvalidUsername` / `HiveInvalidPassword` / `HiveInvalid2FACode` | subclasses of `HiveReauthRequired` raised by the login flow | show a form error |

The rules that keep it consistent:

- **Translate third-party errors once, at the boundary.** Every botocore error
  is handled in `HiveAuth._cognito()` and every aiohttp error in `api._send()`
  / `api.request()`. Nothing above those functions imports or catches
  aiohttp or botocore exceptions, and no `HiveError` is ever re-wrapped as
  another `HiveError`. Chain the original with `raise ... from err`.
- **Handle or raise, never both.** Do not log an exception and re-raise it.
  The handler that finally deals with it logs once: a WARNING without a
  traceback for expected failures, `_LOGGER.exception` only for unexpected ones.
- **Programming errors stay uncaught.** `KeyError`, `TypeError`,
  `RuntimeError` and friends are never caught or converted; a traceback is the
  right outcome for a bug. Hive's data is not a programming error: parsing
  never raises (see "Parse JSON in one place").
- **Retry only a rejected token and transient write failures.** There is no
  polling timer: `get_nodes()` raises and the fork's coordinator decides when
  to poll again. There are two retries. One is a single re-login through
  `Hive._api_call()` after Hive rejects the token. The other is
  `api._fetch_with_retry()`, which `api.request()` uses for every POST (each
  one sets a node's state to absolute values, so it is safe to send twice):
  it retries a timeout, a network error or an HTTP 5xx, and an HTTP 404 once,
  because Hive has answered a write to a node that exists with a 404 after a
  10-second stall (#28). It makes at most `_MAX_ATTEMPTS` attempts with a
  random pause of `_RETRY_DELAY` between them, starts none within
  `_MIN_ATTEMPT_TIME` of the `_RETRY_DEADLINE` (30 s from the first send) and
  cuts each attempt's timeout to that deadline. It never retries Hive's
  gateway block (HTTP 403 `ForbiddenException`): it lasts a minute or two and
  every retry adds to the burst. A GET (the poll) is sent once, and the
  login, refresh and other Cognito calls are not retried here. "Reauth required" is raised only when
  Hive or Cognito actually rejected the credentials or asked for an SMS code,
  never as a relabelling of some other failure, and it is never retried: a
  failed login is remembered so that polls and commands do not send an SMS
  code or try a wrong password on every refresh.
- **Timeouts are bounded and explicit.** Every Hive HTTP call has a 20 s total
  and 10 s connect budget (`api._TIMEOUT`), and a command's retries end within
  30 s (`api._RETRY_DEADLINE`); Cognito calls get botocore's
  `standard` retry mode with a single retry and 10 s connect / 20 s read
  timeouts (`auth._COGNITO_CONFIG`) instead of its 60 s defaults and five
  attempts. Home Assistant does the rest of the retrying: setup backoff after
  `ConfigEntryNotReady`, the next poll interval, and the user for commands.
- **Calls to Hive raise; the data they return does not.** `get_nodes()`
  returns a snapshot or raises; the `set_*` commands return nothing or
  raise. All let every `HiveError` propagate. `new_session()` sends nothing and raises
  only `HiveReauthRequired`, for a config with no tokens or incomplete device
  data. The library does not check command values: the fork does.
- **Parse JSON in one place.** `nodes.parse_nodes()` turns a `/nodes/all`
  body into dataclasses. A value Hive left out or sent with an unexpected type
  is `None`; a node that is not a dict, has no string ID or is of a type this
  library does not support is dropped. None of that is an error or logged.

## Commands

All commands run from the repository root.

```bash
# Install the package and the development tools
pip install -e ".[dev]"

# Run all linters (via pre-commit)
pre-commit run --all-files

# Run tests (always run the full suite — coverage is measured against all of src/)
pytest tests/

# Run a single test class
pytest tests/unit/test_nodes.py::TestParseNodes

# Build the wheel and check it imports in a clean venv
make verify-dist

# Individual linters
ruff check --fix src/ tests/ scripts/ examples/
ruff format src/ tests/ scripts/ examples/
pylint src/
mypy src/
bandit --configfile=tests/bandit.yaml src/
```

All work happens on `main` through pull requests, and every merge is a
release: `release.yml` runs `bump-my-version bump patch` on `main`, which
bumps `version` in `pyproject.toml`, commits and tags it, then publishes a
GitHub Release with the wheel attached (see `docs/workflows/README.md`).
Leave `version` alone in a pull request unless it needs a minor or major
release: then set it to the new version, and `release.yml` releases that
version as it is because it has no tag yet.

A PII check pre-commit hook (`scripts/check_data_pii.py`) runs on any
`src/hiveheating/data/*.json` change — ensure fixture data is anonymised before
committing.

### Development scripts

- `scripts/run_session.py` (`make run-file`, `make run-live`, `make call ...`)
  runs a polling session in the background against the fixture or a real
  account and takes admin commands over a local socket (`state DEVICE` shows
  a parsed product; `set-mode`, `set-temp` and `boost` send commands). Read-only unless
  started with `WRITE=1`.
- `scripts/verify_dist.py` installs the built wheel in a throwaway venv, then
  in one that already has the `pyhive-integration` core pins, and
  imports every module each time (`make verify-dist`).
- `examples/example_login.py` is the minimal standalone login example the
  README points at.

### Test organisation

Tests are split into two subdirectories under `tests/`:

| Directory | Purpose |
|---|---|
| `tests/unit/` | Pure unit tests — heavily mocked, no I/O |
| `tests/e2e/` | End-to-end tests over the whole library: file mode on the fixture, and a scripted-fault Hive server on localhost |

`tests/conftest.py` provides the `file_session` fixture (a started file-mode
`Hive`).

`pyproject.toml` sets `asyncio_mode = "auto"`, so async test functions do
**not** need `@pytest.mark.asyncio`.

Coverage is enforced at **99%** (`fail_under = 99` in `pyproject.toml`).
Running only a subset of tests will report very low coverage and fail — always
verify with the full `pytest tests/` run.

### What a test is for

A test earns its place by pinning one behaviour the library promises and
nothing else does. The suite was once twice its current size because every
rule below had been broken; keep it small by applying them before adding a
test, and in review.

- **One behaviour, one test.** Before writing a test, search for the
  behaviour (the exception, the status key, the log line, the call) in
  `tests/`. If it is already pinned, extend that test or add a parametrize
  case; do not write a sibling. Tests that differ only in their input belong
  in one `@pytest.mark.parametrize` with descriptive `ids`.
- **Test at the layer that owns the logic.** `Hive._request`, `_set_state`,
  the `set_*` commands and `get_nodes` are thin delegations; each command's
  body is pinned once in `TestCommands`. Their error propagation is pinned
  once, where the handling lives: aiohttp translation in `TestFetch`, the
  retry of writes in `TestRetry`, botocore translation in `TestCognito`, login-and-retry in
  `TestApiCall`, value parsing in `test_nodes.py`. A wrapper gets one test
  that it calls through, not a copy of the wrapped class's error table. The
  same goes for cross-layer repeats: `tests/e2e` exists for what only a real
  `Hive` can show (the fixture's real nodes, the scripted-fault server), not
  to re-assert unit-level facts against `data.json`.
- **Do not test Python.** No tests for dataclass defaults, `field(default_factory)`,
  `dict.get` defaults, `json.dumps` escaping, `int("abc")` raising, `async
  with` releasing a lock, `set.discard`, `hmac`/sha256 being deterministic,
  a constant equalling its literal, or a method returning `None` (mypy
  enforces `-> None`). Assert the library's own contribution.
- **Do not test mocks.** A test whose only assertion is that a mock received a
  kwarg the code passes straight through, or that a mocked method on the
  object under test was called, pins nothing. Assert the observable result
  (stored state, the exception, the request Hive would receive).
- **Do not pin log wording** except where the log *is* the behaviour: the
  token masking in debug output. Debug-line text is not a contract.
- **A branch nobody can reach gets deleted, not tested.** If covering a line
  needs an input no caller produces (an int salt from Cognito, a flat
  `ExpiresIn`),
  remove the branch rather than write the test. Coverage measures the code
  the fork can exercise.
- **When a feature is removed, grep `tests/` for it.** Platform removals left
  tests looking for plug, light and sensor ids that could no longer exist;
  they passed for months. A test that cannot fail is deleted with the code.
- **The error table is the floor, not the ceiling.** Every row in "Error
  handling" keeps at least one test; a recent fix (401-only reauth, the 403
  `ForbiddenException` block, the reauth latch, full token masking) keeps the
  regression test named for it. Nothing else is owed a test by default.

If a change makes a test redundant under these rules, remove the test in the
same PR.

## Architecture

### Source layout

```text
src/hiveheating/
├── __init__.py         ← public exports: Hive, Auth, LoginResult, the node dataclasses, the exceptions
├── hive.py             ← Hive — the session: login and token refresh, new_session,
│                         get_nodes and the set_* commands; SessionTokens
├── auth.py             ← HiveAuth (exported as Auth) — Cognito SRP login, SMS 2FA,
│                         token refresh, trusted devices, LoginResult; _ensure_client()
│                         reads the SSO login page; _cognito() translates botocore errors
├── api.py              ← request() — HTTP calls to beekeeper.hivehome.com; _send()
│                         translates aiohttp errors; _fetch_with_retry() retries POSTs
├── nodes.py            ← Nodes, User, HeatingProduct, HotWaterProduct, Device, Mode and parse_nodes()
├── srp.py              ← pure SRP/HKDF crypto: functions, constants and the client ephemeral
├── exceptions.py       ← the HiveError hierarchy
├── helpers.py          ← sanitize_payload for logging
├── py.typed            ← PEP 561 marker: the annotations are the public contract
└── data/data.json      ← anonymised all-nodes response for file mode
```

### `Hive` (`hive.py`)

`Hive(websession)` is the whole session. It owns `hive.auth` (`HiveAuth`),
`hive.websession` and `hive.tokens`, and keeps no device state. `_request()` makes every Hive API
call through `api.request()` with the session's token.

- **Login and tokens** — `_login()` logs in with the stored credentials,
  completes the device challenge with `auth.device_login()` and raises
  `HiveReauthRequired` if Hive wants an SMS code. A `HiveReauthRequired` from
  a login is kept in `_reauth_error`, and `_api_call()` and `_login()`
  raise it again straight away until `update_tokens()` stores new tokens
  (`_login()` checks too, for callers that were waiting on the lock).
  `update_tokens()` stores the ID and refresh tokens from a Cognito
  response, from a login or refresh or saved in the config entry. `_refresh_tokens()` refreshes
  at 90% of the token lifetime (`_REFRESH_THRESHOLD`) and falls back to
  `_login()` when the refresh token is rejected. `_api_call()` wraps every API
  call: refresh first, and on `HiveReauthRequired` (HTTP 401) log in once
  and retry.
- **New session** — `new_session(config)` applies the config (username and
  password, `tokens`, `device_data`; the username `use@file.com` switches to
  file mode) and sends nothing.
- **Hive's endpoints** — `get_nodes()` fetches `/nodes/all` (or the fixture),
  raises `HiveConnectionError` for a reply with no products or no devices and
  returns `parse_nodes()` of the body. Discovery and polling are the same
  call. `set_mode`, `set_target_temperature`, `set_heating_boost` and
  `set_hot_water_boost` each post a fixed body for the product they are given
  through `_set_state()`, a no-op in file mode; a transient failure is retried
  (see "Error handling"). There is no boost-off call:
  ending a boost is `set_mode` with the product's `previous_mode`.

### Nodes (`nodes.py`)

`get_nodes()` returns `Nodes(user, products, devices)`, frozen dataclasses with
`products` and `devices` keyed by Hive ID. Fields use Hive's own names in
snake_case and Hive's own values, from the node's `props` or `state`:

| Class | Hive types | Fields |
|---|---|---|
| `User` | `user` | `temperature_unit` |
| `HeatingProduct` | `heating` | `id`, `type`, `name`, `online`, `model`, `version`, `zone`, `temperature`, `working`, `mode` (`SCHEDULE` / `MANUAL` / `OFF` / `BOOST`), `target`, `boost` (minutes), `previous_mode` (a mode or `False`), `previous_target` |
| `HotWaterProduct` | `hotwater` | `id`, `type`, `name`, `online`, `model`, `version`, `zone`, `mode` (`SCHEDULE` / `MANUAL` / `OFF` / `BOOST`), `status` (`ON` / `OFF`), `boost`, `previous_mode` |
| `Device` | `hub`, `thermostatui`, `boilermodule` | `id`, `type`, `name`, `online`, `model`, `manufacturer`, `version`, `zone`, `battery`, `mac_address` |

`online` is each node's own `props.online`. Hive reports heating and hot water
products as the receiver that runs them (their `model` is the receiver's,
`SLR1` / `SLR2`), so a battery thermostat dropping off leaves them online; the
thermostat's own flag is on its `Device`.

### Authentication (`auth.py`)

Two flows:

1. **First login** — `Auth.login()` returns a `LoginResult` whose
   `sms_required` is true. Call `auth.sms_2fa(code, result)`, then
   `auth.device_registration()` to make this machine a trusted device, then
   save the credentials from `auth.get_device_data()`
   (`device_group_key`, `device_key`, `device_password`). A fourth value, the
   token issue time saved by older versions, is accepted and ignored: tokens
   loaded from a config entry count as due for a refresh.
2. **Subsequent logins** — pass the saved `device_data` in the `new_session`
   config. `login()` is answered with a device challenge, which
   `device_login()` completes without an SMS.

Every Cognito call goes through `HiveAuth._cognito(method, code_map, **kwargs)`,
which runs the boto3 call in the executor and maps Cognito's error codes to
the library's exceptions. Only the Hive account owner is supported. Guest
accounts cannot authenticate.

Token layers are distinct:

- `HiveAuth.access_token` — Cognito AccessToken stored on the auth object by
  every successful login, 2FA or refresh; used by the trusted-device methods
- `Hive.tokens.token_data` — session-layer token store (`"token"` and
  `"refreshToken"`); used by `Hive._request()` for API requests

All timestamps are timezone-aware UTC `datetime`s.

### File-based testing

Pass `username="use@file.com"` in the `new_session` config to make the session load
`src/hiveheating/data/data.json`, an anonymised all-nodes response (`user`,
`products`, `devices`), instead of calling the API. Used for development
without live credentials. The fixture is read in a worker thread, so file mode
is safe under Home Assistant. Commands send nothing in file mode.
