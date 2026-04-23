# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [PEP 440](https://peps.python.org/pep-0440/) versioning.

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
