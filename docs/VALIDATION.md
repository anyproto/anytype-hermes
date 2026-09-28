# Validation record

## Activity update — 2026-09-25

Version 0.2.0 adds Heart chat-status publication and the generic Hermes activity
callback patch. **44 plugin tests** pass, including actual gateway callback wiring,
parallel calls in different chats, two-second refresh scheduling, unique retry IDs,
completion during an in-flight refresh, terminal ordering, interruption, missing
endpoints, payload limits, redaction and falsy JSON results. **22 existing Hermes
callback/warning/native-stream tests** pass with the new patch.

After the updated Anytype app restarted, a fresh HTTP fetch from
`http://127.0.0.1:31009/docs/openapi.json` succeeded. The exact served schema is
saved as `anytype-openapi-v2.json`: version `2025-11-08`, 39 paths, SHA-256
`039c582019e3629d166d07531ee9cad1548d61f3b7254368363c976724b646b7`.
It includes `POST /v2/spaces/{space_id}/chats/{chat_id}/status`. The separately
saved local source snapshot, `anytype-openapi-v2.source.json`, came from Heart
commit `37bea3596`. The earlier HTTP 404 was resolved by the app restart.

At 17:03 UTC the installed plugin's real adapter callbacks, generic Hermes
callback wiring, publisher and HTTP client passed a live check in **Hermes Home**:

- A transient activity, initial `in_progress`, refresh after 2.003 seconds, and
  explicit `complete` all returned HTTP 200.
- All three tool updates repeated their label and shared one generated execution
  ID, without an `Idempotency-Key`. The completed result recorded the real
  authenticated chat-list request and successful heartbeat check.
- Dry-run validation accepted an error payload and results of `null`, `false`,
  `0`, `""`, `[]` and an object, all with HTTP 200.
- The replacement credential resolves to the existing **Agent** account. The
  running gateway reconnected automatically; General and Hermes Home both report
  connected, as does the existing Telegram adapter.

The live check published one retained test activity plus a transient label; it
created no ordinary chat messages or objects. HTTP acceptance is verified;
rendering and receipt on another client were not observed. This check invokes
the installed callbacks directly and does not claim to exercise an LLM turn.

The same 44 plugin and 22 regression tests also passed against the installed
Hermes runtime after applying the new patch. Plugin 0.2.0 and its credential are
installed; activity is enabled for the existing General and Hermes Home chats.

## Installed runtime and home channel update

On 2026-09-24 the plugin was installed into the default local Hermes profile,
running commit `30565b2db235d48d3f1ab42bcc63645a45dd94fa` with the generic patch.
The running gateway connected to Anytype, and its configured MCP server exposed
51 tools and passed an authenticated read-only request. The second account joined
and was configured as the sole sender and operator.

Version 0.1.1 registers Anytype for Hermes cron delivery. A new **Hermes Home**
chat was created at the user's request and configured as the Anytype home channel.
The plugin suite now passes **32 tests**, including real plugin discovery and
cron home resolution with and without a chat origin. The **88 existing Hermes
regression tests** also passed on this installed runtime during initial setup.
The earlier development-only evidence below records the state before installation.

Date: 2026-09-24. Hermes baseline: `aa8a33d22d27973df6591e48f38228274c8ad771`, with [the generic patch](../patches/hermes-delivery-context.patch) applied only to the isolated `.dev` checkout.

## Automated checks

The plugin suite passed **30 tests**, covering:

- Fragmented SSE/UTF-8, id-only checkpoints, protocol validation and v2 pagination.
- Literal markup, Unicode length limits, intact object links and long replies.
- Durable input/cursor transactions, replay deduplication and account namespace separation.
- Persisted attachments/reply anchors, chunk retries and ambiguous send handling after restart.
- Real plugin discovery/config loading, real Hermes authorization, identity normalization and wildcard policy.
- Real session key isolation, profile A → B → A scoped secrets, admin/prefix controls and inline receipts.
- Input merging, history resynchronization and operator status without message-body disclosure.
- A complete SSE → Hermes handler → REST loop, followed by a new adapter recovering an outage message.
- Generic delivery-context migration, boot redelivery, runtime retry and legacy marker compatibility.

The existing Hermes delivery-ledger, delivery-producer, redirect-anchor, steer-origin, queued-follow-up and busy-command regression suites also passed: **88 tests**. They run through Hermes's canonical test runner. Test handlers replace model calls; the full loop does not claim a live LLM conversation was exercised.

## Live API and MCP

- Authenticated read-only API requests succeed at `http://127.0.0.1:31009`.
- The `Hermes` space and its `General` chat are accessible. At this check the space lists only the API/bot account, so the second account still needs to join for the two-account conversation test.
- `@anyproto/anytype-mcp@2.0.1` starts as a local stdio subprocess and exposes **51 tools**, including objects, collections, tasks through object types, and chat REST operations. Event streams are excluded as expected.
- Authenticated MCP `API-list-spaces` and `API-list-types` calls succeed. The space has `page`, `task`, `collection` and other native types.
- MCP dry runs succeeded for a page with Markdown research content, a collection, and a chat message using `text` and `attachments`.
- No chat messages, objects, collections, read receipts or deletions were committed by these live checks.

An initial MCP probe failed because the Python SDK uses snake-case attributes for tool schemas; the corrected probe succeeded. This was a probe issue, not an MCP server startup failure.

## Original acceptance checklist (before installation)

1. Choose the operator/sender policy and join the second account to the space.
2. Activate the plugin in the intended Hermes profile running the patched source.
3. Send a real request from the second account; verify reply, `/new`, `/stop`, clarification and authorization behavior.
4. Ask the agent to create research and a collection through MCP; open returned links/attachments on Desktop and mobile.
5. Restart the live gateway while a test request arrives, and verify recovery without recreating completed work.

This historical checklist predates installation. Sender policy, the second account,
and activation in the real Hermes profile were subsequently completed as recorded
above. Cross-client rendering, structured-object workflows and recovery during a
live conversation remain acceptance checks. The sibling source checkout remains
untouched; the active Hermes installation has the documented generic patches.
