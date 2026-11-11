# Validation record

## Native installation and documentation — 2026-11-11

Hermes `ec3249919accf7bc51f780dcff663e8f19a0e176` installed the repository through
`hermes plugins install anyproto/anytype-hermes --enable` in an isolated profile.
The fetched gateway commit was `26d636460513f32a6148dc0fc04eb03a75a3c740`, version
0.6.0. The installer reported `CAUTION`; its source-review prompt was accepted
with the user's approval. This is an interactive installation path.

Checks confirmed:

- `plugins list --user --json` reports `anytype-platform` enabled, version 0.6.0,
  with Git provenance. `anytype setup --help` exposes the current setup options.
- `plugins update anytype-platform` completes with the same source-review prompt
  and reports already up to date. This checks the update command, not a version jump.
- Disable, re-enable and removal change the plugin's listed state as expected.
  Each preserves synthetic platform/MCP configuration, a dummy credential-file
  marker and a byte-for-byte SQLite state fixture outside the plugin directory.
- Edited documentation links, heading anchors and shell syntax pass checks.
  The archive includes the renamed installation guide and its old-path redirect;
  it excludes the archived gateway spec, original research, patches, credentials
  and runtime state.

The profile and lifecycle evidence are under `.runtime/docs-install-20261111/`.
No live gateway was changed and no Anytype account, message or object was created.
This verifies distribution and CLI discovery; the setup/chat acceptance checks
below remain separate.

## Stock Hermes migration — 2026-11-10

Version **0.6.0** passes **325 tests, 0 failures, 0 skips** on unmodified Hermes
`ec3249919accf7bc51f780dcff663e8f19a0e176` (main, fetched 2026-11-10).
The previous patched implementation is committed at `02555cd` for history.
No changes were made to the installed Hermes or live plugin/configuration.

Validation uses Hermes's canonical per-file runner with isolated homes and
mocked Anytype HTTP. It reuses the existing Python 3.14 test interpreter; it is
not a fresh dependency-lock installation or a live chat/model acceptance run.
The optional local CLI read-only capability smoke also passed. Evidence:
`.runtime/patch-free-20261110/tests-final.log`.

Coverage includes all 15 native activity-hook tests, 39 multichat tests and
8 multispace tests. New/revised checks cover:

- Standard MessageEvent construction and plugin/CLI/skill discovery on stock core.
- Merged and inline-control handoffs without per-input execution-completion claims.
- Failed/cancelled handoffs, restart state migration and suppression of replay.
- Stock boot/runtime ledger recovery with a visible marker, preserved destination,
  and explicitly absent original reply anchor/metadata-only attachments.
- No immediate plain-text fallback after ambiguous anchored or unanchored POSTs.
- Retry of only unsent anchored chunks; no unmarked replay of unanchored partial sends.
- Existing history catch-up, profile scope, identity policy, setup and MCP wiring.

The dev helper smoke verifies that only committed upstream source is cloned,
clean reruns succeed and modified destination files are preserved on refusal.
Packaging excludes all patches and runtime state. Historical patch files stay in
source for reference only. The supported contract and upgrade limitations are in
[operations guide](OPERATIONS.md#recovery-and-operation).

Before public release, perform a live new-profile setup/chat/restart acceptance
run and review remaining private-import warnings for catalog admission. A test
on main does not certify v0.23.1 or other release tags without separate checks.
The dated records below describe earlier implementations and installations.

## Bundled workflow skill — 2026-10-09

Version 0.5.1 passes **114 plugin tests** on the same isolated Hermes `1ac3f97f`
baseline with the unchanged recovery patch. The discovery integration test now
loads the plugin through Hermes's real loader, lists `anytype-platform:anytype`,
and reads its bundled content through `skill_view`. No copy is installed in the
profile's mutable skills directory.

Four independent reviews covered compactness/clarity, practical workflow
balance, scope/authority and Hermes integration/packaging. The compactness review
identified a trigger gap: object-only loading could miss substantial research.
The gateway hint now explicitly includes research and task management. The
reviewer confirmed the fix; no actionable findings remain. The skill is 287
words, with concise guidance on MCP, object reuse, discussion context, tasks,
links, partial failures and job destinations. Model adherence remains a live
conversation acceptance check. No core changes were made for this update.

## Public tool hooks — 2026-10-09

Version 0.5.0 passes **114 plugin tests** and **113 selected Hermes gateway tests**
on an isolated `1ac3f97fbe0958aa2743c0c26202b20b3b4e760e` checkout containing only
the unchanged delivery/receipt patch. The activity callback patch is absent.
Both suites ran through Hermes's canonical test runner.

Fifteen hook/status tests replace the two bridge-specific tests. They exercise
real adapter processing, the Hermes executor and plugin hook dispatch, concurrent
spaces/discussions/profile managers, terminal-only results, falsy JSON values,
blocked/cancelled/timeout outcomes, observer failure without policy interference,
request-ID reuse, stale callbacks, processing cancellation, delegated/muted/cron
exclusion, and routine status versus warning delivery. Existing native typing,
transport, discovery, sender and recovery tests also pass.

The runtime package declares `pre_tool_call`, `post_tool_call`, `post_llm_call`
and excludes the obsolete activity patch. The remaining recovery patch has not
been redesigned or removed. Stock-Hermes release compatibility still needs that
separate review and implementation. No synthetic live chat activity is required
for this test suite; actual client rendering remains a live acceptance check.

## Native status — 2026-10-09

Version 0.4.1 passes **101 plugin tests** against the existing patched Hermes
`1ac3f97f` baseline. Three new regressions exercise the real Hermes progress
callback with its message queue disabled, identical chat IDs across spaces,
discussion status routing, secret redaction, independent elapsed timers and
reset, structured tool priority and explicit completion, and the native typing
loop's cleanup. Regular replies still use the normal sender. No new Hermes core
changes are required. See [local deployment](LOCAL_SETUP.md) for installation status.

## Startup multi-space discovery — 2026-10-07

Version 0.4.0 lists all accessible spaces once at startup and runs independent
watchers for that fixed set. **98 plugin tests pass** on the patched Hermes
`1ac3f97fbe0958aa2743c0c26202b20b3b4e760e` baseline. The eight added multi-space
cases cover complete startup pagination, isolated readiness, per-space participant
identities, identical chat/message IDs in different spaces, parent context and
replies, stream cancellation, per-space failures versus credential revocation,
member initialization retry, restart discovery/removal, new-space downtime
mentions, standalone destinations, stream capacity, and origin scheduling guidance.
The existing 90 tests continue to pass. No additional Hermes core changes are needed.

A read-only preflight found four accessible spaces behind one agent account and
an all-spaces API grant, within Heart's current 16-space stream limit. Installed
with `discovery.spaces: all`, retaining the sender/admin policy, pinned chats, home,
MCP, and existing state. No backups were created. After the managed restart,
Anytype and Telegram report connected without platform errors. All four spaces
completed discovery and report connected health; the catalog contains 4 ordinary
chats and 10 discussions, and creator search tracks 59 objects. The installed
package files match the final archive. No synthetic live chat messages were used.
Evidence is under `.runtime/enable-20261007/` (`multispace-tests.log`,
`multispace-install.json`, and `multispace-status.json`).

## Multi-chat installation after Hermes update — 2026-10-07

Installed plugin 0.3.0 on Hermes 0.21.5, commit
`1ac3f97fbe0958aa2743c0c26202b20b3b4e760e`. The update had removed the required
delivery/activity hooks; the existing patches applied cleanly. A fresh isolated
test environment built through Hermes PM from the updated lock passed **90 plugin
tests and 113 Hermes gateway regression tests** before installation.

The tested seven core files and plugin package were installed with content-hash
verification. Configuration now enables discovery in the existing space, creator
discussion watches, and all-message replies in enrolled chats. Sender/admin
allowlists, the home channel, two pinned chats, MCP configuration, credentials,
and existing state were preserved. No backup was created, as explicitly requested.

After a managed gateway restart, runtime status shows the updated code commit
and both Anytype and Telegram connected without platform errors. The persisted
discovery snapshot is initialized, both pinned chats have connected health, and
the creator-search cache contains 34 objects. Logs, the install manifest, and
test output are under `.runtime/enable-20261007/`. No synthetic live messages were
sent. This confirms installation and live subscriptions; new-chat mentions and
discussion replies still require a real conversation acceptance check.

## Multi-chat implementation — 2026-10-06

Live read-only recheck at 21:52 UTC after the updated app started: the served
OpenAPI now includes both new subscription routes. Authentication succeeded;
the gateway's actual client opened both streams and verified their space scope,
envelopes, and snapshot completion. Chat discovery returned 2 chats; the
creator-filtered search returned 34 objects. Both snapshots completed without
warnings or resume IDs. Streams were then closed without sending messages or
changing objects. Evidence: `.runtime/multichat-20261006/openapi-live.json` and
`live-check.json`. The API prerequisite is now satisfied; installation and a real
multi-chat conversation remain pending.

Version 0.3.0 implements single-space discovery against Heart branch
`go-0000-search-stream`, source commit `a8f4f0887`. Its chat and creator-search
subscriptions use fresh snapshots without resume cursors; recovery uses bounded
paginated REST history and authoritative message verification. The local specs
are `docs/superpowers/specs/2026-10-06-apiv2-space-chat-stream-design.md` and
`docs/superpowers/specs/2026-10-06-apiv2-object-stream.md` in the Heart checkout.

**90 plugin tests passed: 51 existing tests and 39 multi-chat cases.** The full
suite ran through Hermes's canonical parallel test runner against the isolated
`653bc4f288fc00db362c1082a3b652542314fbef` baseline with the existing generic
delivery/activity patches. Coverage includes:

- Authorized mentions, first-mention edits, creator discussions, independent watch
  reasons, and ordering between the chat and search streams.
- Crash during bootstrap, historical backfill, late-synced additions beyond the
  recent-message window, incomplete pagination, and rolled-back mention edits.
- Real Hermes group authorization and thread sessions, pending control answers,
  bounded parent context, merged attachment references, and standalone delivery.
- Removal and scope checks on message/activity output, independent per-chat
  failures, and read-only POST search streaming outside the mutation lock.
- Both HTTP subscriptions through adapter startup, creator-based first-comment
  admission, the real Hermes message-handler path, a REST reply to that discussion,
  receipt completion, and shutdown closing both streams. Model calls are mocked.

Three Astra reviewers examined the plan before implementation and the code after
implementation: Heart contracts, persistence/recovery, and Hermes authorization
and scope. Their findings were fixed, covered by regressions, and re-reviewed;
no remaining blockers were reported in the final targeted reviews.

The initial authenticated OpenAPI fetch from port 31009 exposed only the existing
per-chat message stream, not the new chat/search subscription routes. Its snapshot
is saved locally under `.runtime/multichat-20261006/openapi.json`. Therefore this
release was initially source-tested and packaged without live verification. The
read-only recheck above resolves that API blocker. The gateway is **not installed
or tested in a live conversation**. No live chat messages or objects were created.
Enable `extra.discovery` in the owning profile after installation and verify real mentions,
creator discussions, offline catch-up, and Desktop/mobile rendering before
declaring live acceptance complete.

## Reinstall after Hermes update — 2026-09-30

Version 0.2.1 adapts the delivery patch to Hermes commit
`653bc4f288fc00db362c1082a3b652542314fbef`, retaining upstream's `reply_expected`
field and merge behavior. The activity patch is unchanged. The updated delivery
patch is `patches/hermes-delivery-context-653bc4f.patch`; the original patch remains
available for the earlier baseline.

**51 plugin tests and 113 Hermes gateway regression tests passed**, using Python
3.14.7 and a fresh environment built by Hermes PM with its locked dev/test groups.
Checks used an isolated archive of the current Hermes commit containing exactly
the seven restored production files. Running the upstream tests inside the live
installation initially triggered its real-home I/O guard and warning-path failures;
the untouched isolated baseline and the patched isolated checkout passed the
comparison checks. The plugin fixture no longer needs PyYAML, which current Hermes
has replaced. Its test wrapper now passes the selected source checkout as a pytest
option so the canonical runner's environment scrub cannot silently select `.dev`.

The saved API credential authenticated with HTTP 200 and the running Anytype schema
still exposes the chat status endpoint. Reinstallation preserves the configured
chats, sender/admin identities, home channel, MCP configuration and state database.
After restarting through the managed Hermes launcher, the gateway reports running
on the updated commit, with Anytype and Telegram connected. `hermes anytype status`
confirms both General and Hermes Home are connected, with no uncertain operations.

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
