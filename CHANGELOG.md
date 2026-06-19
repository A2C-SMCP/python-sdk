# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [PEP 440](https://peps.python.org/pep-0440/) versioning.

## [Unreleased]

> A2C-SMCP protocol **v0.2.0 GA** implementation. SDK package version bump to
> `0.2.0` is performed separately at release cut. Tracking issue:
> [#8](https://github.com/A2C-SMCP/python-sdk/issues/8).

### Breaking Changes
- **Connection-plane auth moved from HTTP header to the Socket.IO `auth` dict**
  (#112 / Jira AS-38; Epic TFRM-153). A2C-SMCP is auth-agnostic — **no protocol
  change**; `token` is an SDK/deployment convention aligned with rust-sdk /
  tfrobot-client / TFRS Provider. The credential now travels in the connection
  `auth` dict's `token` field; **HTTP headers no longer authenticate** (routing
  headers such as `X-TF-*` are still passed through, unrelated to auth). This
  **supersedes** the [0.1.5a1] `x-api-key`→`access_token` header default.
  - Constant `DEFAULT_AUTH_HEADER_NAME` → **`DEFAULT_AUTH_FIELD_NAME`** (default
    value `access_token` → **`token`**) in `a2c_smcp.agent` and `a2c_smcp.server`;
    **removed** from `a2c_smcp.computer` (the Computer client holds no credential).
  - `DefaultAgentAuthProvider`: kwarg `api_key_header=` → **`auth_field_name=`**;
    `api_key` is now injected into the connection `auth` dict under `auth_field_name`
    (default `token`) and merged with `auth_data`; `get_connection_headers()` returns
    routing-only headers (no credential).
  - `DefaultAuthenticationProvider` / `DefaultSyncAuthenticationProvider`:
    `authenticate()` reads the credential from the connection `auth` dict
    (`api_key_name`, default `token`) instead of HTTP headers; auth failure rejects
    the connection (`ConnectionRefusedError`).
  - `a2c_smcp.computer.SMCPComputerClient`: **removed** the `auth_header_name=`
    constructor kwarg and the `auth_header_name` property. The connection `auth`
    dict is supplied by the caller via `connect(url, auth=...)` (CLI injects via
    `--auth 'token:...'`).
  - **Migration**: put the credential in the connection `auth` dict under `token`
    — Agent: `DefaultAgentAuthProvider(api_key=...)`; Computer CLI: `--auth 'token:...'`.
    To keep a custom field name, pass `auth_field_name=` (Agent) / `api_key_name=`
    (Server). JWKS/JWT verification is performed by the Server's custom
    `AuthenticationProvider` (e.g. TFRS), not by the SDK default provider.
  - Added `smcp.ConnectAuth` TypedDict (`role` required + `token` `NotRequired`) as a
    protocol-reference type. Note: default providers do **not** inject `role` at
    connect (role is established via `EnterOfficeReq`/`join_office`, consistent with
    rust-sdk); full role-at-connect wiring is a pre-existing protocol-vs-impl
    divergence deferred to a separate protocol-first effort.

### Added
- **Connection protocol-version handshake** (#17 / #18). Clients (Agent + Computer,
  async + sync) auto-append `a2c_version` to the Socket.IO connect URL query;
  `a2c_smcp.PROTOCOL_VERSION` (`"0.2.0"`) is the single source. Server-side
  `a2c_smcp.server.A2CProtocolVersionASGIMiddleware` validates it: missing/invalid →
  HTTP 400; incompatible (MAJOR.MINOR mismatch) → HTTP 400 + Socket.IO `4008`.
  Incompatible clients are rejected and **never connect** (`versioning.md` §4
  loop-defense); `4008` is normalized to `a2c_smcp.exceptions.ProtocolVersionError`.
  Negotiated version is surfaced via `SessionInfo.a2c_version` through `server:list_room`.
- **`client:get_resources` event** (#14, async + sync). Transparent passthrough of
  MCP `resources/list` with caller-controlled `cursor` pagination (SDK does **not**
  auto-paginate). Flat `ErrorPayload` on `4014` (MCP Server not found) / `4015`
  (no `resources` capability). Response keys normalized camelCase→snake_case.
- `base_client.list_resources_page` single-page passthrough API (#13).

### Changed
- **`window://` URI is now a pure identifier** (v0.2). `priority` / `audience` /
  `last_modified` move to MCP `Resource.annotations`; `fullscreen` and other A2C
  extensions move to `_meta`. `WindowURI` no longer parses query; `organize_desktop`
  reads metadata from `annotations` / `_meta` (#9 / #11). Desktop aggregates
  `window://` only — non-window resources never enter the desktop.
- `Finder` removed in favor of the transparent `client:get_resources` event.
- office/role isolation invariants in `on_client_*` now raise
  `a2c_smcp.exceptions.SMCPNamespaceError` instead of `assert` (stripped under
  `python -O`); async + sync namespaces aligned (#31).

### Tests & Docs
- `tests/e2e/test_v02_full_flow.py` (#20): real-process full chain over a real
  Uvicorn ASGI server + real protocol-version middleware + real stdio MCP
  subprocesses — covers handshake negotiation (compatible connects + `a2c_version`
  recorded; incompatible rejected), `window://` desktop aggregation, `client:get_resources`
  pagination with transparent passthrough, and the `client:tool_call` chain.
- **4008 loop-defense test mapping** (#20): no dedicated `test_v02_handshake_loop.py`
  is added — the `versioning.md` §4 infinite-reconnect-defense guarantee
  ("incompatible client never connects") is already covered by
  `tests/integration_tests/test_version_handshake_client.py`
  (`assert *.connected is False`), and the exact `4008 → ProtocolVersionError`
  normalization contract by `tests/unit_tests/test_handshake_4008_normalization.py`.
  `tests/e2e/test_v02_full_flow.py::test_v02_full_flow_incompatible_handshake_rejected`
  re-checks the guarantee at real-process level. Each layer asserts at its own
  deterministic tier (non-flaky), so a separate redundant file would add no signal.
- `README.md`: added the SDK ↔ A2C-SMCP protocol compatibility matrix and v0.2
  protocol-MUST rules. `CLAUDE.md`: event-system conventions updated with the v0.2
  events (`client:get_resources`, connection handshake) and URI/metadata changes.

### Removed
- **DPE scope removed entirely** per a2c-smcp-protocol v0.2.0 GA decision
  (see [`a2c-smcp-protocol/CHANGELOG_DPE_REMOVAL.md`](https://github.com/A2C-SMCP/a2c-smcp-protocol/blob/main/CHANGELOG_DPE_REMOVAL.md)).
  DPE moved to an independent
  [dpe-protocol](https://github.com/A2C-SMCP/dpe-protocol) repository; the A2C-SMCP
  control plane no longer routes, parses, or validates `dpe://` URIs. Although
  this DPE code (introduced in unreleased commits 675c753 / 4223515) never
  shipped in a published version, the rollback is recorded here so future
  cross-SDK protocol-history reviews can correlate Python ↔ Rust SDK timelines:
  - `a2c_smcp.utils.dpe_uri` (`DPEURI`, `is_dpe_uri`) — entire module deleted.
  - `a2c_smcp.smcp`: `GET_DPE_EVENT`; `GetDPEReq` / `GetDPERet`;
    `InlineContents` / `ExternalContents` / `ResolverContents` / `ResolverHint` /
    `ResolvedResource`; `DPEResolutionFailedCategory`; `ErrorCode` members
    `DPE_RESOLVER_NOT_CONFIGURED` (4011) / `INVALID_DPE_URI` (4012) /
    `DPE_RESOLUTION_FAILED` (4013); `ErrorPayload` fields `category` / `dpe_uri`.
- **MCPServerManager host reverse-index machinery removed.** The original
  motivation was routing `client:get_dpe` by host; with that event gone and
  protocol host-uniqueness softened to `SHOULD` (lint-style WARN, non-blocking),
  the index has no protocol-level consumer. Removed: `HostConflictError`,
  `find_server_by_host`, `aretry_pending_host_index`,
  `_host_to_servers` / `_host_index_pending` state, and the registration-time
  conflict detection inside `_astart_client` / `_astop_client` / `_clear_all`.

### Kept
- Non-DPE v0.2 protocol surface stays intact: `PROTOCOL_VERSION`,
  `GET_RESOURCES_EVENT`, `A2CResource`, `ResourceAnnotations`,
  `GetResourcesReq` / `GetResourcesRet`, `ErrorCode` members 4006 / 4007 /
  4008 / 4014 / 4015, `SessionInfo.a2c_version`.
- `WindowURI` parser, `organize_desktop` reading from
  `Resource.annotations` / `_meta`, and `base_client.list_resources_page`
  single-page passthrough (now serving only `client:get_resources`).

### References
- Protocol decision: [`a2c-smcp-protocol/CHANGELOG_DPE_REMOVAL.md`](https://github.com/A2C-SMCP/a2c-smcp-protocol/blob/main/CHANGELOG_DPE_REMOVAL.md)
- Independent DPE protocol: [A2C-SMCP/dpe-protocol](https://github.com/A2C-SMCP/dpe-protocol)
- Tracking issue: [#8](https://github.com/A2C-SMCP/python-sdk/issues/8)
  (closed sub-issues: #10 / #12 / #15 / #16; remaining v0.2 work: #14 / #17 / #18 / #19 / #20)

## [0.1.5a1] - 2026-04-23

### Breaking Changes
- Default auth HTTP header key changed from `x-api-key` to `access_token` across
  `a2c_smcp.agent`, `a2c_smcp.computer`, and `a2c_smcp.server`. A2C-SMCP is
  auth-agnostic; this aligns the SDK defaults with the TuringFocus ecosystem
  convention (Envoy is configured with `headers_with_underscores_action: ALLOW`).
  To keep the previous default, pass `api_key_header="x-api-key"` to
  `DefaultAgentAuthProvider` and/or `api_key_name="x-api-key"` to
  `DefaultAuthenticationProvider` / `DefaultSyncAuthenticationProvider`.
  No protocol change.

### Features
- `a2c_smcp.computer.SMCPComputerClient`: `namespace` and `auth_header_name` are
  now constructor-configurable (previously hardcoded to `/smcp` / `x-api-key`),
  exposed via read-only `namespace` / `auth_header_name` properties, and the
  default `emit` namespace falls back to the instance value.
- `a2c_smcp.agent.AsyncSMCPAgentClient` and `a2c_smcp.agent.SMCPAgentClient`:
  `namespace` is now a constructor kwarg that drives both handler registration
  and the default namespace for every `emit`/`call` site. `connect_to_server`
  accepts an optional `namespace` override; when provided it updates the
  instance namespace and re-registers event handlers before connecting.
- Each SDK module now exports a `DEFAULT_AUTH_HEADER_NAME = "access_token"`
  constant (`a2c_smcp.agent`, `a2c_smcp.server`, `a2c_smcp.computer`) for
  consumers who want to reference the default without hardcoding a literal.

### Fixes
- CLI: `a2c-computer run --namespace` was previously ignored by event handlers
  (only the underlying Socket.IO connect used the override). The CLI now
  propagates the namespace into `SMCPComputerClient`, so event subscriptions
  bind to the requested namespace as intended.

### References
- Aligns with Rust SDK v0.1.15 handshake configurability.
