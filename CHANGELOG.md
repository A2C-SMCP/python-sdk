# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [PEP 440](https://peps.python.org/pep-0440/) versioning.

## [Unreleased]

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
