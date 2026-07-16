# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [PEP 440](https://peps.python.org/pep-0440/) versioning.

## [Unreleased]

> A2C-SMCP protocol **v0.2.0 GA** implementation. SDK package version bump to
> `0.2.0` is performed separately at release cut. Tracking issue:
> [#8](https://github.com/A2C-SMCP/python-sdk/issues/8).

### Breaking Changes
- **plugin ↔ MCP Server is a dependency relation, not an ownership one** (#153, aligned
  with a2c-smcp-protocol `runtime-contract.md` §2.5 / §4.9.1 / §5.6; adjudication record
  in protocol Discussion #23 D3/F1; rust mirror rust-sdk#139 — **both SDKs share the same
  on-disk ledger format, a divergence is data corruption**).
  - **Ledger field renamed and re-typed**: `bundledMcpServers` (array of display **name**)
    → **`mcpServers` (array of `bundle_id`)**, written unconditionally (`[]` when empty).
    It records the MCP servers a plugin **declares a dependency on**. The ledger MUST NOT
    store display names (they go stale when an explicit `bundleId` server is renamed) nor
    point-in-time facts such as provenance/introduced (they rot as *other* plugins are
    uninstalled — the transitive-leak source). **Legacy records are dropped wholesale and
    rebuilt from the `installedPlugins` intent** (§4.9.1-4); no name→bundle_id migration
    mapping is written.
  - **Uninstall/disable/gc no longer reclaim unconditionally.** New criterion (§4.9.1-2,
    a pure function in `reconciler.py`): **reclaim X ⟺ no other plugin declares a
    dependency on X ∧ X is not user-declared**. Consequences: a user's own server is
    **never** collaterally removed, and a dependency shared by several plugins survives
    until its last dependent is uninstalled (no leak).
  - **`MCPServerNameConflictError` removed**, along with `_conflict_check` and the whole
    `owned` notion. Installing a plugin whose declared `bundle_id` already exists is
    **dependency satisfied** → report and install normally (exit 0); it MUST NOT be
    rejected. `plugin enable` **reuses** an already-satisfied dependency instead of
    remounting over it. Same display name with a different `bundle_id` is legal
    coexistence (§5.6). The CLI JSON error code `mcp_server_name_conflict` is gone.
  - **Injected-callback seam is now bundle_id-keyed**: `ExistingServerNames` →
    **`ExistingBundleIds`** (`install_plugin` / `enable_plugin` / `materialize_plugin`
    kwarg `existing_server_names=` → **`existing_bundle_ids=`**); `RemoveServer` and
    `gc_plugins(mcp_teardown=)` now take a **bundle_id**; `Computer.reconcile_governance`
    kwarg `existing_server_names=` → **`existing_bundle_ids=`**.
  - **Dependency prechecks and governance remount now read the runtime-authoritative
    config set** (`manager.server_configs()`), never the construction-time snapshot
    `Computer.mcp_servers` (§2.5-4) — under the CLI that snapshot is permanently empty,
    so every "dependency satisfied" was misjudged as "unsatisfied".
  - CLI output: `plugin install/list/info` field `bundledMcpServers` → **`mcpServers`**
    (values are now bundle_ids).
- **Plugin install/enable separation — install no longer activates** (#123, aligned
  with a2c-smcp-protocol **v0.3.0** `runtime-contract.md` §2.3/§2.4/§4.8; adjudication
  record in #120 / protocol#11; rust mirror rust-sdk#103).
  - **`enabledPlugins` default flipped**: an absent entry now means **not enabled**
    (only an explicit `true` activates; `false` explicitly disables and overrides a
    lower scope). Under v0.2.x, absent meant enabled.
  - New declarative intent **`installedPlugins`** (settings.json array of
    `<plugin>@<marketplace>`): the global install set. `install_plugin` now writes it
    config-first to the **user scope**, materializes (clone / manifest validation /
    MCP dependency precheck / ledger), and **does not activate** — no SKILL
    staging, no bundled-server mount, no `enabledPlugins` write → `installed_disabled`.
    Materialization failure atomically rolls the intent entry back.
  - `install_plugin` signature: **removed** `register_server=` / `remove_server=` /
    `inject_inputs=` kwargs (install mounts nothing); the precheck kwarg is kept but was
    since renamed to `existing_bundle_ids=` and demoted to report-only (see #153 above —
    it no longer rejects). New `materialize_plugin()` exposes activation-free
    materialization (reused by boot re-materialization).
  - **`enable_plugin` is now atomic**: skills and bundled servers light up together;
    on mount failure it rolls back to `installed_disabled` (unregisters newly staged
    skills, removes newly mounted servers via the new `remove_server=` kwarg, restores
    the previous `enabledPlugins` value — absent entries are deleted, not set `false`).
  - `uninstall_plugin` now also removes the `installedPlugins` entry and clears
    `enabledPlugins` entries (user always; project/local derived from recorded
    `projectPath`) once no ledger records remain.
  - **Boot recovery is intent-driven** (`recovery.py`): the install set comes from the
    merged `installedPlugins` (the ledger is a rebuildable derived cache — deleting
    `installed_plugins.json` is lossless: boot re-materializes missing entries, reported
    via the new `GovernanceRecoveryReport.rematerialized`); the active set is
    installed ∧ enabled; `installed_disabled` restores lazily (no projection).
    Orphan detection (`list_orphan_plugins`) now keys off `installedPlugins`
    (`declared_plugin_ids` **removed** in favor of `declared_installed_plugin_ids`).
  - **One-time migration** (`migrate_legacy_installs`, run by `Computer.boot_up`):
    existing ledger installs are written into `installedPlugins`, and plugins without
    any `enabledPlugins` entry get `enabledPlugins=true` in the user scope so
    pre-upgrade active plugins stay active (explicit `false` stays disabled). The
    presence of the `installedPlugins` key in user settings marks migration done
    (written even when empty), so manual intent removals are never resurrected.
  - CLI: `plugin install` prints `installed_disabled` state and mounts nothing;
    `plugin list` shows **all** installed plugins with a two-state enabled column
    (`--available` kept as a compat no-op); `plugin list`/`info` enabled now means
    explicit `true`; `plugin enable` wires `remove_server` for rollback.
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
- **Plugin lifecycle follow-ups** (#125, closing out the #123 isolated-review items;
  rust mirror evaluation via rust-sdk#103):
  - **Re-materialization scope inference** — boot recovery now infers the original
    install scope of rebuilt ledger records from per-layer `enabledPlugins` entries
    (and project/local `installedPlugins` declarations) instead of always normalizing
    to the user scope; multi-layer clues rebuild multiple records, dead stale-scope
    records are swept, and clue-less rebuilds are normalized to user scope with a
    WARN plus the new `GovernanceRecoveryReport.scope_normalized` field. This keeps
    `plugin disable`/`uninstall` writing to the correct settings layer across boots.
    `uninstall_plugin` additionally clears cwd-visible project/local `enabledPlugins`
    entries (guarded so it never creates a `.tfrobot/` dir in a bare cwd). Known
    blind spot (documented): layers of *other* project paths are not visible from
    the current cwd — precise restoration remains pin-lock territory (§4.9.2).
  - **Dangling-intent diagnosis & prune** — new `list_dangling_plugin_intents`
    (reconciler) detects `installedPlugins` entries with no live materialization that
    are statically unreachable (four reasons: `marketplace-not-added`,
    `catalog-missing`, `manifest-unreadable`, `entry-missing`; reachable-but-not-yet
    materialized intents are reported as recoverable and self-heal at next boot).
    `plugin gc` now reports them (JSON: `removed` unchanged + new `dangling`,
    `prunedIntents`, `recoverable`) and can prune via the new
    `installer.prune_plugin_intent` — REPL behind the confirm gate, non-interactive
    Typer requires the explicit `--prune-dangling` flag (pruning deletes
    authoritative intent, unlike orphan gc which only drops derived cache).
    Committable project/local `installedPlugins` declarations are never rewritten
    (WARN with the file path instead).
  - **Ledger liveness now validates bundled JSON** — `ledger_entry_materialized`
    (public in reconciler, migrated from `recovery._ledger_materialized`) treats a
    record as materialized only if the `installPath` directory exists **and**
    `load_bundled_servers` parses; a corrupt bundled server JSON now triggers
    re-materialization (repair) or keeps the plugin wholly `installed_disabled`
    (no more "skill lit, server WARN-skipped" half-state).

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

### Deprecated
- `plugin list --available` is a no-op since v0.3.0 (listing all installed plugins is
  the default) and now emits a deprecation notice (non-JSON mode; JSON mode logs a
  warning to keep stdout parseable). Planned for removal in a future release (#125).

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
