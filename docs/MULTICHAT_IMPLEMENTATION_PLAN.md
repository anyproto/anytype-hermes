# Multi-chat gateway implementation plan

Date: 2026-10-06

2026-10-07 extension: 0.4 adds startup-only multi-space enumeration in one Hermes
profile, as requested after the single-space rollout. Independent watchers retain
the original single-space contracts below. This extension passed 98 tests; it does
not add collaborator permission isolation. See README and the validation record.

Status: implemented, packaged, and reviewed before and after implementation by
three Astra lenses. All 90 plugin tests pass. Both subscription snapshots were
verified against the running app. Installed and enabled on updated Hermes
`1ac3f97f` on 2026-10-07; live conversation acceptance remains pending.

## Scope and baseline

Implement the single-space stage of [the recommendation](MULTICHAT_RECOMMENDATION.md):
dynamic chat enrollment on an authorized mention, object discussions, creator-based
automatic discussion watches, parent context, and durable restart recovery. Keep
legacy explicitly configured targets working. Automatic multi-space enrollment
and isolated worker provisioning remain a separate stage; discovery accepts one
space per profile and rejects mixing that mode with other configured spaces.

Use the checked-out Heart branch `go-0000-search-stream`, currently `a8f4f0887`.
Do not modify or switch the Heart working tree. The branch's implemented contracts
take precedence over the earlier gateway recommendation where they differ:

- `docs/superpowers/specs/2026-10-06-apiv2-space-chat-stream-design.md`
- `docs/superpowers/specs/2026-10-06-apiv2-object-stream.md`
- `core/api/APIV2.md`, handlers, models, and service tests for both streams.

Use the installed Hermes interface baseline `653bc4f288fc00db362c1082a3b652542314fbef`
and the existing isolated copy under `.runtime/reinstall-20260930/baseline` for
tests. Preserve pre-existing user changes and existing delivery/activity patches.
Package the result; live installation/restart is not part of this source change.

## Implemented Heart contract

1. `GET /v2/spaces/{space_id}/chats/stream?include=discussions&heartbeat=30`
   emits `chat_added` snapshot rows, `snapshot_complete`, then chat, message,
   counter, reaction, and pin events. Every envelope has `type` and `space_id`.
   Chat rows contain kind, parent, counters, and `last_state_id`. Message events
   contain full messages, including their creation `state_id`.
2. The space stream has no SSE IDs, replay, or server-side resync events. A new
   connection is a fresh snapshot. A chat discovered after the snapshot includes
   a backfill of its newest 50 messages. Snapshot chats have no message backfill.
3. `POST /v2/spaces/{space_id}/search/stream` uses a search request body with
   `filter: creator = "<member-id>"` and requested `fields`, including `discussion`.
   It emits `object_added`, `snapshot_complete`, then add/update/remove changes.
   An object envelope uses `object`; a removal uses `object_id`. A row's optional
   `discussion` is top-level. Reconnect always supplies a complete fresh snapshot.
4. Search streams cannot use full-text `query`, have no paging or resume IDs,
   and must not be kept behind the client's mutation pacing lock.
5. Mention fields are deliberately absent. Parse canonical `<mention object_id="…">`
   markup from both `text` and `blocks_text`. Ignore display-name lookalikes and
   escaped/code literals. Reply-to-agent detection uses actual reply authors.
6. Recover sequentially through complete paginated REST history, bounded at
   100,000 messages per chat with explicit unresolved health beyond that bound.
   The per-chat replay's end marker is conditional and its display-order window
   can omit late-synced additions without a resync signal. Do not use it as proof
   of coverage. State IDs sort by local materialization time, independently of
   message display order. Use the same REST traversal for live newly discovered
   chats, whose server backfill contains only the newest 50 messages.
7. Counter updates and chat snapshot state IDs are comparisons for recovery,
   never processing acknowledgements. Advance checkpoints from persisted message
   payloads after complete recovery batches, not from snapshot/counter high-water marks.
8. Space events can precede transaction commit and can describe a rolled-back
   message. Recheck eligible new inputs through REST before dispatch, with bounded
   retry for eventual visibility, and expose an unresolved receipt instead of
   executing absent data. Outage edits/deletions have no replay guarantee; refresh
   recent history on reconciliation and document the remaining coverage limits.

## Implementation work

### 1. Configuration and protocol

Add an opt-in `discovery` configuration with a full `space_id`, creator-watch
toggle, and response policy for dynamically enrolled chats. Allow a discovery-only
profile without pinned chats. Preserve existing defaults for legacy configurations.
Require all pinned targets to belong to the discovery space. Keep sender/admin
allowlists separate and suppress agent-account echoes.

Add client helpers for the GET chat stream and POST search stream. Validate event
type and space identity. Use full IDs in stream
requests (`ids=full`) so routing never persists ambiguous shortened space references.
Always close streams on cancellation. Existing SSE fragmentation, size checks,
heartbeat timeout, auth, retry/backoff, and secret-safe diagnostics remain in use.

### 2. Persistent discovery state

Add SQLite tables without destructive changes to existing chats, inputs, or
delivery receipts. Store the discovered chat catalog, availability, kind, parent,
watch reasons, creator-search result cache, and space initialization state.

Represent watch reasons independently: pinned, authorized mention, and
agent-created parent. Removing an object from the creator query removes only
that reason. An unavailable chat suspends all reasons and pending dispatch/send.
Removing a pinned chat from configuration must not leave a stale pinned reason.

Persist observed messages for unwatched chats to support deduplication, reply
context, and discovery races. Distinguish old baseline history, observed but
untriggered messages, unauthorized messages, pending inputs, and completed inputs.
Transactionally persist mention enrollment and its triggering input. Checkpoints
must never regress when message display order differs from state-ID order.

### 3. Multi-chat discovery coordinator

Add a focused coordinator module used by the existing adapter. Run one long-lived
space chat stream and, when enabled, one creator-search stream. Keep stream
reconnects independent; use complete snapshot boundaries for removal reconciliation.
Incomplete snapshots must not delete previous membership.

On chat discovery, update metadata and refresh active adapter targets. Receive
all message events even for unwatched chats; enroll only on an authorized real
mention/reply or an agent-created parent. Subsequent messages follow the response
policy. A first-mention edit can promote a previously observed input, but editing
an already dispatched/completed input must not run it again.

Coordinate stream ordering: if a comment arrives before creator-search enrollment,
retain it as observed and reconsider it when ownership becomes known. A creator
snapshot does not by itself execute all historical comments. Resolve the parent
mapping before dispatch; an unresolved mapping remains pending/recoverable.

Bootstrap existing history as baseline only on first discovery setup. Process
live buffered additions even when a concurrent baseline read already saw them.
On later reconnects, recover unseen chats as well as previously watched chats,
because mentions may have arrived while the gateway was offline. Sequential
per-chat history traversal avoids allocating permanent per-chat subscriptions.

Keep the adapter's shared dispatcher gated until initial chat/search discovery
is ready. Bound recovery and verification work, preserve cancellation, and expose
per-chat recovery failures without dropping unrelated chats.

### 4. Hermes dispatch, context, and delivery

Use the existing adapter `build_source` and `handle_message` path. Continue using
`<full-space-id>/<actual-chat-id>` as the transport target. Model a discussion as
a shared thread session (explicit thread ID), preserving sender attribution and
the actual discussion chat ID for delivery. Keep ordinary group session behavior.

Fetch parent metadata/content when admitting each eligible discussion input, with a bounded
excerpt in `channel_context`. Use existing object reads, retaining stable object
IDs and links in routing guidance. Treat parent content as untrusted source data.
Unavailable parent access stops the turn; transient fetch failures remain retryable.
This is admission-time context: Hermes can queue a turn, and its existing merge
behavior retains the first input's channel context. Preserve attachment references
in merged input text and document that parent context is not execution-time fresh.
Shared discussion sessions use the normal Hermes thread setting; explicitly show
`thread_sessions_per_user: false` in the example rather than overriding operators.

Update target checks across send, activity, chat-info, reconciliation, and standalone
cron delivery. Standalone sends accept only configured or persistently watched,
available targets in the permitted space; they cannot accept arbitrary chat IDs.
Permit the matching discussion thread identifier without treating it as a second
transport destination. Preserve it in exact-delivery recovery metadata. Recheck
target availability at actual send/status publication, and recheck receipt status
after awaited verification/context reads. Preserve exact delivery receipts and
ordinary `text` sends. Discovery-only cron guidance must work without a home channel.

### 5. Documentation and packaging

Bump the plugin version for multi-chat support, add a configuration example, and
document the actual branch contracts and bootstrap/reconnect limitations. Include
the coordinator in the packaging allowlist. Update the recommendation's status
or implementation notes so it does not imply unimplemented mention fields or a
durable global event cursor are prerequisites for this release.

## Validation

Use meaningful protocol and integration fixtures with the actual Heart envelope
shapes, plus the real installed-baseline Hermes session and plugin interfaces.
Run the existing suite and new multi-chat cases through Hermes's canonical test
runner in the isolated environment. Do not post test messages to live chats.

Required cases:

- Real/escaped/code mentions; block text; allowed/denied authors; self suppression.
- Dynamic enrollment with its first input persisted exactly once; completed edits
  do not rerun; first-mention edits can enroll an unprocessed message.
- Creator snapshot/live updates/removals; object-before-chat and chat-before-object;
  first-comment delivery without a mention; incomplete snapshots preserve state.
- Restart into existing state, newly discovered offline chats, out-of-order state
  IDs, counter-before-message events, replay overflow, and stream cleanup.
- Baseline/live race; search reconnect races; rolled-back events and delayed REST
  visibility; auth/target removal suspends dispatch and sending.
- Shared discussion session keys, fresh bounded parent context, attachments, tool
  activity, same-chat replies, standalone cron sends, and out-of-space rejection.
- Legacy target-only configuration and the existing test suite remain compatible.
- Packaged plugin includes every required module and no runtime state or secrets.

Fetch the running local OpenAPI read-only, if available, to report whether the
updated branch endpoints are actually served on port 31009. A source-contract test
does not establish live desktop/sync behavior; report that distinction.

## Review sequence

Before implementation, three Astra agents independently review this plan:

1. Heart API contract and stream lifecycle correctness.
2. Persistence, recovery, ordering, and race handling.
3. Hermes integration, authorization, space boundaries, and scope.

Resolve findings in this document, implement, and run focused plus regression
tests. Then have the same three lenses review the resulting code and evidence.
Fix substantive findings and rerun the affected checks before delivery.

### Preimplementation review disposition

All three reviews completed. Adopted: complete paginated recovery rather than
assuming a replay terminator/coverage; checkpoint only complete durable batches;
live-versus-baseline receipt promotion; authoritative REST payload revalidation;
durable unresolved visibility and retry; independent watch reasons; availability
checks after awaits and on actual output; persisted discussion thread metadata;
explicit Hermes thread configuration and admission-time context semantics.

### Postimplementation review corrections

The second three-lens review identified and prompted fixes for group authorization
with a discussion thread ID, pending control exemptions in mentions-only mode,
standalone creator-watch policy changes, isolated per-chat REST failures, offline
first-mention edits, historical backfill being mistaken for new input, and a
rolled-back mention removal poisoning the historical comparison payload. Baseline
comparisons now retain authoritative payloads across unverified edits. All three
reviewers confirmed their findings closed after inspecting the fixes.

Bootstrap now persists a local-materialization cutoff before subscribing. Heart
uses BSON ObjectIds for message `state_id`; include the previous timestamp second
conservatively. REST classifies messages at/after that cutoff as eligible observations,
so a crash before consuming their buffered events does not turn them into old
baseline. Unchanged older baseline remains suppressed when a chat reappears.
The previous-second overlap may include a message from immediately before startup.

Final checks: 90 passing tests (51 existing, 39 multi-chat), including both HTTP
subscriptions through the real Hermes handler/reply lifecycle and cancellation
closing both streams. See [the validation record](VALIDATION.md) for evidence and
the remaining live acceptance checks.
