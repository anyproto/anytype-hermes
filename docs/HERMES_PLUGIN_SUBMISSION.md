# Anytype gateway: steps toward the Hermes plugin catalog

Updated: 2026-10-09. Current plugin: **0.5.1**.

Recommended route: keep `anyproto/anytype-hermes` as an external platform plugin,
use the public tool hooks now implemented in 0.5.0, resolve or remove the
remaining recovery dependency, then submit a reviewed
**community** catalog entry. Users should eventually install it with
`hermes plugins install anytype-platform`, without applying patches.

## What “official” means

Being listed in Hermes's catalog means the pinned plugin has passed its review
process. The separate `tier: official` designation means **maintained by
NousResearch**. An Anyproto-maintained integration would normally use
`tier: community`; Anytype authorship does not automatically confer the Hermes
official tier. Ask maintainers about a different ownership arrangement only if
that distinction matters. [Hermes catalog documentation](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugin-catalog#whats-in-an-entry)

Catalog inclusion, the official tier, and shipping inside the Hermes repository
are separate decisions. Current contribution policy directs new third-party
product integrations toward external plugins. A catalog listing is therefore
the practical target, rather than moving the Anytype implementation into core.
[Plugin contribution policy](https://github.com/NousResearch/hermes-agent/blob/1ac3f97fbe0958aa2743c0c26202b20b3b4e760e/plugins/AGENTS.md)

## Why our current installation still needs a core patch

Basic Anytype chat transport does not inherently require a patched Hermes.
Our patches were introduced to support stronger recovery guarantees and richer
tool activity than the available adapter interfaces exposed.

Version 0.5.0 has **one remaining patch covering two recovery contracts**:

| Contract | What the patch supplies | Why the plugin uses it |
|---|---|---|
| Durable input receipts | Carries original message IDs through merges, debounce and steering; reports processing outcomes for inline controls. | Finish every consumed receipt when multiple messages become one turn, including inline commands. |
| Durable reply context | Saves reply/attachment metadata and keeps the delivery ID and original content stable across initial send and recovery. | Preserve reply anchors, attachments and operation identity when reconciling a send after restart. |

Both are in [the delivery/receipt patch](../patches/hermes-delivery-context-653bc4f.patch),
which affects five Hermes files. The plugin does not rewrite Hermes files at import
or startup; the local installation currently applies this compatibility patch.

Structured activity now uses public plugin hooks; the activity patch was removed
from the package and is retained in the repository only for migration. The UI
transport is not required. See the implementation and evidence below.

The **0.4.1 native typing/status change requires no additional core patch**.
It uses the existing `supports_status_text`, `set_status_text` and `send_typing`
interfaces. Its elapsed-time label and disabled Working chat messages are
independent of the public tool hooks. Disabling
`extra.activity_status` alone would not remove the receipt/delivery dependency.

This is a temporary compatibility arrangement, not the intended distribution
model. Hermes explicitly rejects runtime core overrides and directs authors to
request a missing public hook. Requiring users to apply source patches is also
in tension with its broader “plugins never touch core” policy. Our recommendation
is to remove that prerequisite before requesting catalog admission; a validator
passing registration alone would not prove that the gateway can connect and run.
[Catalog admission policy](https://github.com/NousResearch/hermes-agent/blob/8bff64d6ed3414a66976bfa8ab72c14b6bca2a6f/plugin-catalog/README.md)

## Do other gateways patch core?

The inspected gateways do not require users to apply separate patches during
installation:

| Example | How it integrates | What we can reuse |
|---|---|---|
| Bundled Telegram / Discord | Their adapters and supporting gateway code ship together in Hermes. | Normal platform registration, message handling, authorization and send/edit interfaces. |
| Bundled Slack | Uses native text status; its structured task-card callbacks are wired by code already in Hermes. | Native status for simple labels; our plugin uses public tool hooks for structured rows. |
| Catalog WhatsApp Agent Platform | Its documented installation uses the plugin installer; the entrypoint exports its adapter registration. | External repository, declared compatibility, pinned catalog release and setup documentation. |
| Catalog Bunny Space | Its entrypoint also exports adapter registration; setup uses normal plugin installation and profile configuration. | A precedent for an external gateway handling rooms and mentions. |

References: [Slack callback wiring](https://github.com/NousResearch/hermes-agent/blob/8bff64d6ed3414a66976bfa8ab72c14b6bca2a6f/gateway/run_turn_runner.py#L786),
[WhatsApp plugin setup](https://github.com/ahtishamdilawar/hermes-whatsapp-agent-platform/blob/4d8fc1c482ed06a34b6609b1dcdf59e370de7d40/README.md),
[Bunny Space setup](https://github.com/jasm001/hermes-plugin-bunny-space/blob/fcea5421ed9418b6bfc4f292933310e43aa076c5/README.md).
These are inspected examples, not a claim that every community plugin has been audited.

A gateway may need a framework change during development. The supported outcome
is a generic feature merged into Hermes and consumed through its public interface,
rather than a separate patch each plugin user must maintain.

## Can we use the UI's tool-call stream instead?

The UI has the data we want. In the inspected Hermes `1ac3f97f` source,
`tui_gateway` attaches agent callbacks and emits:

- `tool.start`: `tool_id`, tool name and arguments.
- `tool.complete`: the matching `tool_id`, result and optional duration/summary.

Desktop receives JSON-RPC events through `/api/ws`. The dashboard's terminal UI
also has a side channel: its TUI process publishes through `/api/pub`, and
`/api/events` broadcasts those frames to subscribers on the same channel.
[UI callbacks](https://github.com/NousResearch/hermes-agent/blob/1ac3f97fbe0958aa2743c0c26202b20b3b4e760e/tui_gateway/agent_callbacks.py#L142),
[event payloads](https://github.com/NousResearch/hermes-agent/blob/1ac3f97fbe0958aa2743c0c26202b20b3b4e760e/tui_gateway/contracts/events.py#L266),
[WebSocket routes](https://github.com/NousResearch/hermes-agent/blob/1ac3f97fbe0958aa2743c0c26202b20b3b4e760e/hermes_cli/web_routers/chat_ws.py#L695).

However, this is not a machine-wide stream of all Hermes tool calls. Our Anytype
turns run in the separate messaging gateway, whose `TurnRunner` attaches its own
callbacks. Connecting an observer to the dashboard does not make those turns
publish UI events. UI replay is also an in-process session buffer, not a durable
event log shared with the messaging gateway. Moving execution into the UI backend
would be a larger transport change, beyond subscribing to tool events.
[Process distinction](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard#connecting-hermes-desktop-to-a-remote-backend),
[gateway callback wiring](https://github.com/NousResearch/hermes-agent/blob/8bff64d6ed3414a66976bfa8ab72c14b6bca2a6f/gateway/run_turn_runner.py#L1262).

Version 0.5.0 uses **public Python plugin hooks** directly. The installed
Hermes source already passes `session_id`, `turn_id`, `tool_call_id` and
`api_request_id` to tool hooks. `post_tool_call` also receives result, duration
and terminal outcome information. These hooks run where the tool executes, so
they do not require a running Desktop or dashboard.
[Tool identity fields](https://github.com/NousResearch/hermes-agent/blob/1ac3f97fbe0958aa2743c0c26202b20b3b4e760e/agent/inline_tool_executors.py#L20),
[completion observer](https://github.com/NousResearch/hermes-agent/blob/1ac3f97fbe0958aa2743c0c26202b20b3b4e760e/model_tools.py#L676).

Implemented flow:

```text
Hermes pre_tool_call / post_tool_call hooks
  → Anytype plugin: processing-task binding to its exact publisher, space and chat
  → existing ActivityPublisher: execution IDs, refresh, redaction, terminal state
  → Heart POST /v2/spaces/{spaceId}/chats/{chatId}/status
```

The compatibility tests cover real adapter processing and Hermes executor/hook
thread propagation, parallel spaces/discussions/profiles, blocked/cancelled/timeout
outcomes, terminals without starts, reused provider IDs, interruption, late callbacks
and falsy results. Callbacks enqueue work and return None. Pre-tool labels say
`Preparing <tool>` because approval may still be pending. Delegated child tools,
muted diagnostic turns and unrelated CLI/cron executions are excluded. Native
status and the optional adapter status sink keep routine activity out of messages.

The activity patch is no longer needed. The UI remains useful precedent for the
payload shape, but its WebSocket feed is not a drop-in observer for messaging
turns. The delivery/receipt patch is a separate dependency.

## Submission steps

### 1. Decide the remaining recovery contract

The public-tool-hook replacement is implemented and tested in 0.5.0. Review the
remaining delivery/receipt patch next. A release without that patch is possible:
adapt receipt tracking to stock event/lifecycle interfaces, and either replace or
explicitly drop exact outgoing-payload recovery across restarts. Keep conservative
handling of ambiguous sends; do not blindly retry them under a new operation ID.
Test the chosen restart, merge and inline-command behavior on clean Hermes.
Removing only the startup check would not make today's code compatible.

If the stronger recovery contract remains part of the release, pursue the
framework changes below. If a smaller supported contract is chosen, skip upstream
work that is no longer required and validate the patch-free implementation.

Then open a focused Hermes issue for the remaining gaps, linking this working
plugin as the concrete consumer. Explain the observed failure cases, not just
the proposed method names. Ask whether maintainers prefer extending an existing
interface or adding an optional adapter hook.

Candidate independently reviewable core changes:

- Input receipt propagation and inline processing completion.
- Optional persisted delivery context and stable delivery identity.

Keep Anytype API details out of those changes. Existing adapters should retain
their behavior unless they opt in. The current patch files are prototypes for
discussion, not PRs to submit unchanged. The official submission guide explicitly
invites issues for missing hooks. [Asking for a hook](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission#asking-for-a-hook)

**Done when:** the public-hook result is documented, and maintainers agree on
interfaces for any remaining gaps, or we choose a documented reduction in
functionality. This task has not opened issues or PRs.

### 2. Land and test the generic changes upstream

Rebase each proposed change onto fresh Hermes `main`. Add behavior tests using
Hermes's canonical test runner. Preserve profile ownership and existing adapter
defaults; keep transport context bounded, JSON-serializable and free of credentials.

Cover merged input completion, inline commands, restart during delivery,
reply/attachment recovery, parallel calls, retries with new execution IDs,
cancelled runs and late callbacks. An ambiguous outbound send must remain
explicitly uncertain; this is not a claim of unconditional exactly-once delivery.

**Done when:** the necessary contracts are available in an unmodified upstream
release and their minimum supported Hermes version is known. A merge into `main`
alone is not a reason to advertise compatibility with an older release.

### 3. Remove the plugin's installation patch requirement

Adapt the plugin to the accepted interfaces. Replace the manual patch instructions
and `scripts/prepare-dev.sh` patch application with a clean-Hermes development
workflow. Move historical patch material out of the release installation path.
Update connection checks to report the actual minimum supported Hermes version.

Keep the native status implementation already used in 0.4.1. Keep the Anytype MCP
server a separate, optional tool connection; this platform plugin should continue
to own chat ingress, routing and replies.

If upstream declines the richer hooks, a smaller patch-free release is possible,
but it needs an explicit redesign and revised recovery guarantees.
Merely deleting the patches or hiding their checks would leave the current code
depending on unavailable fields and methods.

**Done when:** a clean Hermes install can start the adapter and complete the
supported conversation/recovery flows without source edits or monkey-patching.

### 4. Prepare the public plugin release

Use the repository already configured here:
`https://github.com/anyproto/anytype-hermes`.

- Commit and publish the intended release, then select an immutable commit.
- Add an explicit project license under Anyproto's chosen licensing policy;
  this working tree currently has no root license file.
- Declare a truthful `requires_hermes` floor in `plugin.yaml`. The current
  manifest has none; do not claim stock 0.21.5 compatibility because our local
  0.21.5 installation is patched.
- Check runtime dependencies against Hermes constraints and declare what the
  plugin needs. Test a clean environment rather than relying on locally
  installed packages.
- Document the minimum Anytype/Heart build providing message streams, chat and
  creator-search subscriptions, and `/status`. If those APIs still require a
  development build, say so explicitly rather than promising stable-app support.
- Document local API access, key storage, profile state, watched spaces,
  sender/admin allowlists, parent context, optional MCP setup and service restart.
  Keep real tokens, account identities and local runtime artifacts out of the release.
- Explain that the current sender allowlist is not per-user MCP authorization
  across spaces; the planned collaborative permission filtering is not implemented.

Provide screenshots of a chat, object discussion and structured tool activity,
using non-sensitive sample data. Treat the reviewed Git commit as the installable
release; `dist/anytype-platform.zip` is only a local packaging artifact.

**Done when:** a new user can follow the README with only documented public
prerequisites, and the published tree matches the version being submitted.

### 5. Validate the exact release on clean Hermes

Use a disposable profile and unmodified Hermes checkout/runtime, not the installed
profile with our patches. Run the plugin's full tests and a real two-account
conversation covering mentions, discussions, parent context, multi-space routing,
home-channel jobs and status. Verify reconnect/restart behavior and persistence.

Run the catalog's documented admission command against the exact plugin checkout:

```sh
hermes plugins validate /path/to/pinned/anytype-hermes --install-deps
```

Resolve failures and review warnings. That validation checks registration and
packaging; our runtime tests must also establish the gateway behavior. Add CI for
the minimum supported release and current Hermes development version, with
compatibility failures visible before updating the catalog pin.

**Done when:** both admission validation and the runtime suite pass on unmodified
Hermes, with results attached to the release. [Submission validation](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission#before-you-submit)

### 6. Submit the catalog entry

Open a PR to `NousResearch/hermes-agent` adding
`plugin-catalog/anytype-platform.yaml`. Use `category: platform`, a public HTTPS
repository and the full 40-character release commit. Submit as the repository
owner or a major contributor. A maintainer must review and merge the listing;
listing updates likewise require a reviewed SHA change.
[Catalog rules and entry schema](https://github.com/NousResearch/hermes-agent/blob/8bff64d6ed3414a66976bfa8ab72c14b6bca2a6f/plugin-catalog/README.md)

Draft only; replace the placeholders after steps 2–5:

```yaml
name: anytype-platform
title: Anytype
repo: https://github.com/anyproto/anytype-hermes
sha: <full-40-character-release-commit>
version: "<published-plugin-version>"
requires_hermes: ">=<first-supported-unmodified-Hermes-version>"
description: >-
  Anytype API v2 chat and discussion gateway with mention enrollment,
  startup space discovery and native activity. Uses the configured Anytype
  API and stores transport state under the owning Hermes profile.
maintainer: anyproto
tier: community
category: platform
docs_url: https://github.com/anyproto/anytype-hermes#readme
capabilities:
  provides_tools: []
  provides_hooks:
    - pre_tool_call
    - post_tool_call
    - post_llm_call
  provides_middleware: []
  requires_env:
    - ANYTYPE_API_KEY
```

These capabilities cover the platform, CLI command and three lifecycle hooks,
with no model tools or middleware. Version 0.5.1 also bundles the read-only
`anytype-platform:anytype` workflow skill through `ctx.register_skill`. Document
the skill in the catalog submission; MCP tools remain separate.

The PR should link the enabling Hermes changes, show validation evidence and
screenshots, and disclose API traffic, persisted message/recovery state, background
subscriptions and the profile's configured tool/model processing of chat content.
Review the disclosure wording against the pinned code. The official guide describes
the PR and CI process. [Opening the PR](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission#opening-the-pr)

### 7. Verify catalog installation and maintain the pin

After acceptance, verify in a fresh profile:

```sh
hermes plugins install anytype-platform
hermes plugins enable anytype-platform
```

Complete Anytype configuration and start the gateway. Confirm discovery and one
real reply before calling the catalog release complete. Future releases should
include migration notes and compatibility evidence with each catalog pin update.
[Catalog install/update workflow](https://hermes-agent.nousresearch.com/docs/user-guide/features/plugin-catalog)

## Current evidence and remaining work

- **0.5.0**: 114 plugin tests passed on Hermes
  `1ac3f97fbe0958aa2743c0c26202b20b3b4e760e` with only the delivery/receipt patch.
  See [local deployment](LOCAL_SETUP.md) and [validation](VALIDATION.md).
- Fresh source check: upstream `main` at
  `8bff64d6ed3414a66976bfa8ab72c14b6bca2a6f`, committed 2026-10-09, still lacks
  `get_delivery_context`, `ingress_message_ids` and `get_activity_callbacks` in
  the inspected gateway paths. `gateway/activity_callbacks.py` is absent.
  Native text status is present. This was a source inspection, not a runtime test
  of that new commit. [Base adapter](https://github.com/NousResearch/hermes-agent/blob/8bff64d6ed3414a66976bfa8ab72c14b6bca2a6f/gateway/platforms/base.py),
  [message events](https://github.com/NousResearch/hermes-agent/blob/8bff64d6ed3414a66976bfa8ab72c14b6bca2a6f/gateway/platforms/event.py),
  [turn callback wiring](https://github.com/NousResearch/hermes-agent/blob/8bff64d6ed3414a66976bfa8ab72c14b6bca2a6f/gateway/run_turn_runner.py).
- The September research snapshot remains useful historical evidence; its
  validator result was for plugin 0.2.0 on a locally patched runtime. It is not
  admission evidence for today's release.
- UI/event and public tool-hook findings above were verified in the installed
  `1ac3f97f` source; those files are not among our local core modifications.
  The replacement uses those public hooks without modifying them.
- The next concrete action is **step 1: review the recovery dependency** and choose
  the release contract. Publication,
  maintainer outreach and upstream PR submission remain future work.
