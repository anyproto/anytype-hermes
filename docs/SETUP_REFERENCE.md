# Anytype setup reference

For interactive setup, use [the setup guide](SETUP.md). This reference covers
automation fields, publication and recovery contracts, and MCP protocol details.

Gateway 0.6 requires no Hermes core patches. Use the tested stock revision in
[compatibility](../README.md#hermes-compatibility). Keep the existing profile and
`anytype/state.sqlite3` when upgrading; the plugin preserves checkpoints and
historical receipts. Recovery limits are documented in
[operation](OPERATIONS.md#recovery-and-operation).

`hermes anytype setup` prepares and validates a connection in the active Hermes
profile. Select that profile using Hermes's normal profile mechanism. The command
is available without an API key. Platform-menu provisioning is deliberately not
registered: the inspected host's setup callback contract can overwrite plugin
configuration and start a gateway unexpectedly.

Desktop, externally managed endpoints, and native CLI provisioning are supported.
Use released Anytype CLI v0.5.0 as the supported baseline, following
[the installation guide](INSTALL-OTHER-COMPUTER.md#2-install-released-anytype-cli).
Native setup requires its schema-1 automation capabilities.
Hermes checks capabilities and strict JSON envelopes; it never parses human account-key
output. Install that compatible CLI explicitly and select its executable. No installer,
upgrade, restart, service replacement, or per-profile native instance is automatic.
Existing services can also be attached with `--mode external` without native management.

The gateway and Anytype remain independently managed. Stopping/removing Hermes
does not stop Anytype or remove its account/data. An existing account's public
name is preserved. Account replacement is a separate migration; creating another
account is never a way to rename one.

## Native CLI setup

Native mode manages the CLI's **one default instance**, shared independently of the
Hermes profile. The journal binds its executable, absolute config/data/work paths,
account identity, observed network and listener. Changing those facts blocks resume.
An existing Hermes account must be verified through its prior API credential or an
explicit `expected_account` before native mutations. Account replacement and rename
are unsupported. Native service automation supports macOS launchd and Linux systemd;
Windows native setup is not supported.

Choose `service.action: reuse|install|start`. `install` installs a matching native
service definition and starts it; `start` starts an already installed matching service;
`reuse` requires a reachable service. Existing mismatched definitions, binaries,
listeners, environments, or older running services are refused by the CLI. Hermes
never restarts a service or changes its account/network/listener to make it match.
An explicit `service.listen_address` selects a fresh listener or confirms the existing
one. The API origin always comes from live native inspection, including nondefault
ports; an unproven binding remains `syncing` with an operator action.

Choose `account.action: create|login|reuse`. Creation requires the chosen Unicode
`agent_name`; login takes `account.key_ref` privately, and reuse preserves the current
account name. CLI-owned operation receipts recover create/login after interruption. A matching
saved operation can bridge temporarily absent live network/name observations during
recovery; saved proof is retained and fully checked after authentication. A reported
different network/account still blocks replay.
Hermes never requests an account key from create/login, stores it in the profile,
or prints it. Anytype CLI owns recovery credentials and its native auto-login storage.
For a fresh headless instance explicitly choose `cli.credential_backend: private_file`;
the CLI persists that choice for later service auto-login. Without that choice,
keyring access failures fail closed. Existing keyring markers are never migrated.

`network.mode` defaults to `preserve`; `default` refuses an existing custom network,
and `custom` requires an absolute `config_file`. A reused account must keep its
existing config path. The file hash is bound for resume. Optional `expected_id`
confirms an already observed network and cannot be used to guess a fresh network.
The CLI validates the actual account network and config during create/login.

A fresh named-space setup can use the preparation-only input below. Its
`activation: defer` does not save the live connection; follow
[preparation and publication](#preparation-and-publication) to publish it.

```json
{
  "schema_version": 1,
  "mode": "cli",
  "cli": {"executable": "/absolute/path/to/anytype", "instance": "default", "credential_backend": "private_file"},
  "service": {"action": "install"},
  "account": {"action": "create"},
  "agent_name": "Агент 🌿",
  "network": {"mode": "default"},
  "invitation_ref": {"file": "/private/path/invitation"},
  "scope": {"mode": "single"},
  "application_name": "Hermes assistant",
  "access": "operator",
  "operator_identities": ["operator-account-identity"],
  "home": {"action": "skip"},
  "mcp": {"mode": "http"},
  "activation": "defer"
}
```

For reuse, choose `service.action: reuse`, `account.action: reuse`, and an explicit
scope. For login choose `account: {"action":"login","key_ref":{"file":"/private/path/account-key"}}`.
A private invitation is optional; initial single-space scope may omit `space_id` only
when that invitation will resolve it. `join_space: true` requests a private invitation
prompt interactively. An explicit `api_grant` needs already resolved full space IDs.
Native membership list/status runs before named key creation. Pending approval or
sync returns exit 3 without minting the key. Removed/rejected historical spaces do
not block an all-space grant for currently joined spaces.

Hermes saves stable operation IDs and declared intent before each mutation. Resume
reuses them, never searches for a same-name credential and never duplicates an
uncertain operation with a fresh ID. The CLI owns native reconciliation and exact
secret receipts. Once native private input is checkpointed, resume does not need the
original key/invitation reference. If the CLI reports that input was never recorded,
supply the original private input again. Invitations and account keys never enter
the journal. `operation_uncertain` requires inspection; cancellation does not delete
accounts, memberships, services, or older keys.

The integration application name is stable across replacements. It defaults to a
profile-derived `Hermes-…` name for a new connection; existing connections retain
the verified name or require `application_name` when the old credential is unavailable.
Only the generated API key is staged in Hermes. It takes precedence over inherited
`api_key_ref` choices. The native receipt must prove the exact credential ID, account,
application and grant, and the HTTP probe must confirm that same credential.
Application names retain their exact UTF-8 bytes, including leading/trailing spaces;
they are never normalized like an account display name. Dash-prefixed names are
passed as positional values, not command flags.

## Preparation and publication

The wizard validates the candidate API grant, account identity, full IDs, member
and chat surfaces, message history/streams, and selected discovery subscriptions.
The served `/v2/docs/openapi.json` establishes write-route and activity support.
The bot also needs editor, admin or owner membership in every selected space; a
readwrite API key alone does not grant space write permission. No diagnostic
sends a message or creates a dummy object. Optional home creation
and the final test message require explicit choices.

Preparation stages the integration key under a fresh profile secret name. It
stores only non-secret choices, selected identities and operation references in
`<profile-home>/anytype/setup.json`, separate from transport state. The active
configuration and its old secrets are unchanged while the candidate is prepared.
Correcting a private API/MCP reference during an incomplete attempt selects a
fresh staged name when its value changes. Old staged values and completed home
operations are retained. Existing references use Hermes’s authoritative active
profile/external secret scope; a stale `.env` value cannot override that scope.
`activation: defer` returns `restart_required`, with publication `prepared`.

Publishing requires all affected profile consumers to remain stopped until the
single atomic config replacement. HTTP MCP can reconnect independently of the
gateway, so writing an endpoint and token separately is unsafe. The wizard
preserves unrelated profile settings, group/thread settings and platform
extras/overrides. A selected `extra.api_key_env` is authoritative: a missing value
never falls back to `ANYTYPE_API_KEY`.

Two publication paths are available on the inspected Hermes host:

- `manage_gateway: true`: the supported Hermes lifecycle stops/starts a known,
  installed, unshared profile gateway. The operator must close other MCP clients
  and maintain exclusive access. An ambiguous/unmanaged or shared host service
  receives a specific next action instead of an automatic broad stop.
- Explicit offline boundary: the operator stops the profile and its clients,
  prevents starts/config edits during publication, and acknowledges that boundary.
  Setup additionally reserves the profile gateway runtime lock. This is operator
  coordination, not a claim to detect or coordinate arbitrary MCP clients.

`save_stopped` publishes and leaves consumers stopped. `activate` uses the normal
managed lifecycle when selected and available; an offline-only boundary returns
saved settings with an instruction to start through Hermes. Anytype is never
stopped by either path. `doctor` verifies readiness separately after start.
A reported running process does not establish that every MCP client has adopted
the configuration.

Before stopping, setup journals consumer identities and their running state. An
interrupted or failed attempt restores only consumers it stopped, after verifying
the prior configuration and identities. When restoration cannot be proved safe,
the result names the stopped consumers and required action. Old and new integration
keys remain valid; setup never revokes them on an ambiguous outcome.

`setup --cancel` abandons an uncommitted candidate without deleting external
operations. After commit, `setup --rollback` explicitly republishes prior owned
settings and still-valid secret references under the same boundary. Intervening
configuration edits block publication/rollback. The boundary also requires the
operator to exclude uncooperative concurrent configuration writers; the host has
no public cross-process compare-and-swap configuration API. Administrator-managed
Anytype gateway or MCP pins are refused before consumers are stopped; update
those settings through the owning administrator configuration. Unrelated managed
settings do not block setup.

Rollback checks an enabled MCP server independently of whether the prior gateway
was enabled. If no prior account/grant evidence was saved for an MCP-only
connection, setup refuses automatic restoration with an explicit recovery action.
A known prior MCP connection is probed again before publication, including after
a deferred rollback.

## Automation input

Use versioned JSON with non-secret choices and private references. The example
prepares a connection without publishing it; use the publication choices below
to save it before starting the gateway.

```sh
hermes anytype setup --non-interactive --input /private/path/setup.json --json
```

```json
{
  "schema_version": 1,
  "mode": "desktop",
  "base_url": "http://127.0.0.1:31009",
  "api_key_ref": {"file": "/private/path/anytype-api-key"},
  "scope": {"mode": "all"},
  "access": "operator",
  "operator_identities": ["operator-account-identity"],
  "admin_identities": ["operator-account-identity"],
  "home": {"action": "skip"},
  "mcp": {"mode": "none"},
  "activation": "defer"
}
```

Secret files must be absolute, regular files owned by the operator with no group
or other permissions (mode `0600`); symlinks are refused. An `{"env": "NAME"}`
reference resolves in the active profile's secret scope. Raw account keys,
API/MCP tokens and invitations are never accepted as command-line arguments or
input-document values. Interactive secrets use a private terminal prompt.
`--json` selects the result format; it does not authorize mutations.

Other choices:

| Field | Meaning |
| --- | --- |
| `action` | Existing connection: `keep`, `repair`, or `change`. Keep runs read-only validation and retains the connection. |
| `mode` | `cli`, `desktop`, or `external`. An explicit existing API origin is preserved unless replaced deliberately. |
| `expected_account` | Expected stable bot identity, useful for repairing an unavailable old connection. A different account is blocked. |
| `application_name` | Previously verified integration key name. Required with `expected_account` when the old key cannot be inspected during repair. Replacement keys retain the same application name because object ownership depends on it. |
| `agent_name` | Public account name; Unicode/internal spaces preserved, outer whitespace trimmed, empty/control names refused. An existing different name requires supported explicit rename; otherwise setup refuses. |
| `scope` | Gateway discovery: `{"mode":"all"}` discovers every space allowed by the API grant; `{"mode":"single","space_id":"full-id"}` stays fixed. |
| `api_grant` | Intended credential access, separately from discovery: `{"all_spaces":true,"space_ids":[],"permission":"readwrite"}` or `{"all_spaces":false,"space_ids":["full-id-a","full-id-b"],"permission":"readwrite"}`. The candidate must match exactly. When omitted, all discovery requests an all-space grant and single discovery requests that one space; legacy `scope.grant: all` remains accepted. |
| `access` | `operator` (recommended) or explicit `anyone`; administrators remain explicit account identities. |
| `home` | `keep`, `skip`, `select` with full `space_id`/`chat_id`, or explicit `create` with `space_id` for Hermes Home. |
| `activity_status` | Boolean; explicitly disable when the optional activity route is unavailable. |
| `activation` | `defer`, `save_stopped`, or `activate`. |
| `manage_gateway` | Explicitly use the supported lifecycle of the known unshared managed Hermes gateway. |
| `ack_origin_change` | Required acknowledgement that origin changes select a new transport namespace; no receipts/watches are copied. |
| `drop_outside_pins` | Explicitly remove incompatible pinned chats when switching to a fixed new space. Also select/skip a compatible home. |
| `send_test_message` | Explicit final test to the selected home after commit. An ambiguous test send is never repeated automatically. |

An exclusive operator-maintained boundary is supplied as:

```json
{
  "schema_version": 1,
  "activation": "save_stopped",
  "offline_boundary": {
    "profile_home": "/absolute/path/to/the/active/profile",
    "exclusive": true,
    "gateway_stopped": true,
    "mcp_clients_stopped": true
  }
}
```

This minimal document can be passed with `setup --resume`. Do not assert these
conditions unless they hold throughout the commit. For managed gateway activation,
set `manage_gateway: true`, `activation: activate`, and acknowledge exclusive
access/MCP shutdown; setup manages the gateway itself and does not require
`gateway_stopped: true`. The default is preparation, never an implicit stop.

## MCP

The logical MCP name remains `anytype`. Choose chat-only explicitly with
`mcp.mode: none`. If an enabled `anytype` MCP connection already exists, that
choice also requires `mcp.conflict: remove`. A mismatched enabled MCP connection
cannot survive a chat-only candidate activation.

In native mode, `mcp.mode: http` prefills the live reported URL and shared API key;
conflicting URLs or separate-token choices are refused. For desktop/external mode,
supply the backend's actual `url`; `shared_api_key: true` selects the staged API key,
otherwise provide `token_ref`.
Setup initializes MCP, applies the host’s tool filters, discovers tools required
by the bundled skill, and verifies the same account and grant. Transport success
alone is insufficient. No MCP URL is assumed.

Heart `92ed13917ebb` implements Streamable HTTP at `/mcp/full` on its API origin.
For this known contract, setup negotiates `2025-06-18` or `2025-11-25` without a
version header on initialization. Full mode excludes `auth_whoami` from its tool
list, so setup checks `/v2/auth/whoami` with the actual MCP bearer, then calls the
flat `get_member_me` tool for account identity. That REST check is allowed only
for the exact same API origin, canonical path, reported Anytype server identity
and supported protocol. Other MCP endpoints must expose their own grant tool;
a nearby REST API does not establish their credentials. The pinned contract is
covered by isolated tests, not a claim that every installed Heart build has it.

`mcp.mode: wrapper` offers the documented Node wrapper configuration. It must be
installed/cached explicitly by the operator first: validation starts `npx` in
no-install/offline mode, so doctor never installs software. Existing v1-only
wrappers lacking v2 identity/grant tools cannot establish this setup contract.
Choose chat-only or a compatible version instead of assuming tools are safe.

Replacing a current MCP configuration requires `mcp.conflict: replace`; transport
fields are replaced as a unit, while supported tool/timeout preferences are kept.
`mcp.mode: keep` validates that the retained server matches the candidate account
and scope. Corresponding credential rotation must retain its stable application
name; `mcp.application_name` supplies a known prior name when same-endpoint
credentials cannot be inspected. Interactive setup offers replace/remove/keep
choices when a server already exists. Unsupported server fields or inline
credentials must be resolved before setup can journal the connection. A failure
retains the prior live connection.

## Resume and joining

Setup resumes incomplete progress by default; `--resume` requires a saved attempt.
Verified account identity remains binding across failed attempts and later resumes;
a replacement key must still belong to that account. An empty disabled platform
placeholder can be set up as a new connection, including after a fresh setup was
rolled back. Explicit prior connection settings retain account protection even
when that gateway is disabled.
Resume restores attempt-stopped consumers before candidate validation. Interactive
resume asks again whether to defer or publish unless this invocation supplied an
activation choice. Publication revalidates the prepared identity, grant, stable
application name, membership permissions, enabled activity and MCP tools. An interrupted
home creation asks the operator to inspect/select its ID instead of creating a
second chat. Private invitations are never stored merely to support resume.

For desktop/external services, join in the owning client first, then run `join`
to refresh authenticated membership. Non-interactive joining accepts
`joined_space_id` and `discovery_action: retain|switch|all`. Native mode accepts
`invitation_ref` privately (or prompts interactively), then polls its durable join
operation with `join --resume`; a completed later invitation gets its own operation ID.
Choose `discovery_action` before submitting the invitation. Before new or resumed
native joins, Hermes probes the currently selected profile credential/account and
verifies the recorded native origin, credential and instance binding. Changing that
connection requires setup repair first. Administrator-managed Anytype gateway/MCP
pins block native join before probing or mutation; unrelated managed settings and
profile edits do not block joining.
An observed invitation submission keeps `changed: true` through pending responses
or subsequent validation failures. After native membership
completes, `join --resume` also resumes the prepared publication candidate.

Retaining a fixed old scope reports **joined; outside gateway discovery scope**.
For desktop/external mode, a named-space grant needs an explicitly supplied replacement
key under the same stable application name. Native mode creates and recovers the
exact replacement itself; it keeps CLI provenance and stages the new key before
publication. If the old grant already covers the intended scope, native join verifies
its exact credential and reuses it without creating another key. The replacement
may grant both the old and new spaces while gateway discovery switches to the
new one, or discovers all spaces in that named grant. An all-space
grant already covers later spaces. Intended discovery changes prepare a candidate
through the same publication protocol; deferred activation reports **joined;
gateway restart required**. Startup discovers spaces, while individual chats
still need mention/creator/pin enrollment. `response: all` applies to authorized
messages in watched chats.

Scheduled `deliver="anytype"` targets the configured home. Jobs from another space
should retain their origin unless another destination was requested. Setup does
not rewrite existing jobs.

## Result envelope and validation

New commands return `schema_version`, `state`, `changed`, `next_action`, and an
`error` with a typed `type` when applicable. Gateway and MCP results are separate.
Exit codes: `0` requested work completed, `2` input/action/restart needed, `3`
waiting for membership approval/sync, `4` temporarily unavailable, `1` terminal or
incompatible. Diagnostics contain no credentials or message bodies. Local
`status` and offline `reconcile --deep` retain their previous behavior.

The isolated test suite covers staged selectors, interrupted publication,
partial-stop restoration, rollback with legacy selectors, conflicting edits,
desktop setup/resume, identity/grant/application-name mismatch, membership roles,
home ambiguity, MCP filtering and the pinned HTTP MCP contract. A real Hermes
subprocess test verifies plugin discovery and automation exit codes in a temporary
profile without credentials. Native tests cover private subprocess channels, schema/exit
validation, receipt recovery, pending invitations, repeated later joins, exact grants,
reported HTTP MCP, and generated-key precedence. Reviewed-binary smoke uses only
capabilities/help/parse paths. These checks do not establish real
service installation, account creation, sync or remote membership approval. Real
CLI/desktop acceptance runs remain necessary against a selected supported build.
