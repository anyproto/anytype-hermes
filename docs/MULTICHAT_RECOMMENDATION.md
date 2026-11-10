# Anytype–Hermes: multi-chat, discussions, and multiple spaces

Date: 2026-10-06

Status: architecture recommendation with implemented stages noted below.

2026-10-07 update: gateway 0.4 adds the approved single-operator multi-space mode:
list accessible spaces once at startup, then run independent per-space chat and
creator-search streams. Restart to discover new spaces; spaces/global-search
subscriptions can replace this later. This does not implement the isolated
collaboration environments proposed below or permission filtering for MCP tools.

Implementation update: gateway 0.3 targets the single-space stage against Heart
`go-0000-search-stream` at `a8f4f0887`. The branch deliberately omits structured
mention fields and global replay cursors. The implementation parses canonical
mention markup and uses fresh snapshots plus bounded REST history reconciliation.
Those implemented contracts supersede the corresponding API proposals below;
multi-space isolation remains a later stage. See [the reviewed implementation plan](MULTICHAT_IMPLEMENTATION_PLAN.md)
and [configuration/limitations](OPERATIONS.md#chats-and-spaces).

This document consolidates the proposed gateway extension and the Heart API v2
changes it needs. It replaces earlier exploratory recommendations for these
features, including tracking agent-created objects through MCP callbacks. The
existing [gateway specification](../SPEC.md) continues to describe delivery,
attachments, tool activity, and other implemented behavior.

## 1. Recommended design

Use one space-wide chat event subscription to discover conversations and receive
their activity. Use a separate creator-filtered search subscription to discover
objects created by the agent and their discussions. Persist watched conversations
and processing checkpoints in the gateway.

Treat each space with a different audience as a separate trust boundary. Give it
its own Hermes environment and space-restricted tool access. One Anytype account
can remain the visible agent across those environments.

Deliver in two stages:

1. Multi-chat and object discussions inside one space.
2. Automatic discovery of additional spaces, backed by isolated environments.

## 2. User-visible behavior

| Situation | Required behavior |
| --- | --- |
| An authorized user mentions the agent in an unknown chat | Watch the chat and process the triggering message. |
| An authorized user mentions the agent in an object's discussion | Watch that discussion, load its parent object as context, and reply there. |
| A user comments under an object created by the agent | Discover and watch the discussion automatically; an explicit mention is unnecessary. Sender authorization still applies. |
| A watched conversation receives more messages | Apply its configured response policy. |
| The gateway reconnects or restarts | Restore watches and resume processing without treating the triggering message as old history. |
| The agent joins another permitted space | Discover the space, prepare its isolated environment, and route eligible mentions to it. |

Keep watching policy separate from response policy. Recommended defaults are all
authorized messages in enrolled discussions, with ordinary chats configurable as
either all authorized messages or mentions/replies only. Existing explicitly
configured chats and the home channel remain pinned watches.

Admission remains configurable: named account identities, space participant IDs,
or anyone in the permitted space. A mention does not bypass that policy. Ignore
messages authored by the agent's own account. Administrative commands retain a
separate explicit authorization policy.

Normal replies continue to use `text`, optional `attachments`, and `reply_to`.
Do not introduce outgoing chat `blocks`. The discussion chat ID is the destination
for replies and tool activity. Research and other substantial results can still
be created as native objects or collections, with links returned in the chat.

## 3. What Hermes already supports

Hermes's platform adapter and session routing can handle dynamically discovered
chat IDs. This feature does not require an Anytype-specific routing branch in
Hermes core. The current plugin's fixed target list is the main adapter constraint.
Existing generic delivery and activity patch requirements still apply until the
installed Hermes version provides equivalent contracts.

Use a stable transport target containing the full space ID and actual chat ID:

```text
<full-space-id>/<chat-or-discussion-id>
```

Keep each discussion in a separate conversation session. Choose shared discussion
history deliberately: Hermes normally isolates ordinary group sessions per user,
whereas its thread behavior can share a conversation. Keep sender attribution in
shared sessions. A parent object ID is contextual metadata, not a reply channel.

Before a discussion turn, provide the parent object's name, type, ID/link,
relevant properties, and a bounded content excerpt through incoming channel
context. Include relevant reply/history context separately. Treat object content
as source material, not as instructions that can change permissions.

Fetch the parent with the existing object GET route; use Markdown rendering or
properties/content as appropriate. Cache by space and object ID, and refresh
using search updates or a fresh read. Object edits update context without
automatically starting an agent turn. Keep stable routing guidance separate from
changing document content.

## 4. Discover agent-created objects through search

Use the existing space-scoped search filter capability and the planned search
stream. Conceptually, the subscription is:

```text
space = current space
creator = agent's member ID in this space
```

The creator property currently uses a space member/participant ID. Resolve the
agent account with `GET /v2/spaces/{space_id}/members/me`. If the API later accepts
raw account identities in filters, it can perform that resolution internally.
The expression above describes the predicate, not a new filter syntax.

The initial result set discovers existing objects. Subsequent result changes
discover newly created objects and changes to their discussion references. Each
result must expose the object ID and optional `discussion` chat ID, consistently
with object GET responses. Search rows do not currently expose that top-level
discussion member; adding it is part of this recommendation.

When a matching object has a discussion, add a watch with reason
`agent_created_object`. When it gains its first discussion, enroll the discussion
and recover its first eligible message. Retain the parent ID for context loading.
If event arrival precedes the search update, resolve the parent through an object
read or queue the event for reconciliation; do not silently discard it.

Search is the source of truth for ownership. A local cache is useful for routing,
but no MCP creation hook or independent ownership ledger is required. This also
finds objects created by the agent account outside the current Hermes session.

This intentionally defines "agent-created" as "created by the agent's Anytype
account." A dedicated account makes that definition appropriate. Manual creations
from the same account are included too.

Search events identify objects and discussion references. Chat events identify
comments. A new comment in an existing discussion must not depend on the parent
object changing or the search subscription re-emitting its row.

## 5. Heart API v2 changes

The following paths and additional fields are recommended contracts. New stream
routes and their event names are proposals, not claims of current availability.

| Surface | Recommendation | Priority |
| --- | --- | --- |
| `GET /v2/spaces/{space_id}/chats?include=discussions` | Return ordinary chats and object discussions with `id`, `name`, `kind`, optional `parent_id`, unread counters, and a state/version marker. | Required |
| `GET /v2/spaces/{space_id}/chats/stream?include=discussions` | Add space-wide chat discovery, state changes, and message events with resume support. | Required |
| `POST /v2/spaces/{space_id}/search/stream` | Stream an initial query result and subsequent membership/row changes, using the existing search request/filter model. Expose discussion references and changes to them. | Required for creator-based automatic watches |
| `ChatMessage` in REST and SSE | Add structured direct-mention participant IDs and reasons the authenticated account is addressed. | Required |
| `GET /v2/spaces/{space_id}/chats/{chat_id}` | Return one chat's metadata, including its discussion parent. | Useful; optional if events embed sufficient metadata |
| `GET /v2/spaces/{space_id}/chats/{chat_id}/messages/{message_id}` | Read a triggering message or reply target directly. | Useful; optional when full event payloads/history suffice |
| `GET /v2/spaces/stream` | Report spaces becoming available, unavailable, or changing access, scoped to the API grant. | Multi-space stage; polling the space list is an interim option |

No separate discussion-message API is needed. Existing routes already provide:

- `GET /v2/spaces/{space_id}/objects/{object_id}` for parent context and its
  optional `discussion` reference.
- `POST /v2/spaces/{space_id}/objects/{object_id}/discussion` when the agent needs
  to create a discussion itself.
- Chat message read/write routes for a discussion's chat ID.
- `POST /v2/spaces/{space_id}/chats/{chat_id}/status` for activity.

No Heart mutation to "watch this chat" is required. Watching is gateway policy,
independent of Anytype's unread state.

### Chat list and mention semantics

Use `kind: chat | discussion`. For a discussion, `id` is its actual chat ID and
`parent_id` is the owning object. An optional `kind=discussion` filter could offer
a discussions-only view without creating another resource family.

Unread counters are scoped to the authenticated Anytype account. They help with
discovery snapshots and diagnostics, but must not be the sole processing trigger:
another client can mark a message read before the gateway observes a counter.

Proposed message fields are `mention_ids` for direct mentions and
`attention_reasons` for the authenticated account, with values such as
`direct_mention` and `reply_to_me`. Heart currently treats replies to one's own
messages as mentions too; preserve the distinction in the JSON representation.
Extract mentions from both content marks and text blocks. Do not infer identity
from display names or a literal `@Agent` string.

An edit introducing a first mention should be discoverable as `message_updated`.
It may enroll and process a previously unprocessed message. Edits to an already
processed message refresh stored context without automatically running it again.

### Space-wide chat stream

Recommended event families:

| Event | Payload purpose |
| --- | --- |
| `chat_added`, `chat_updated`, `chat_removed` | Chat identity, metadata, availability, and parent mapping. |
| `state_updated` | Current per-chat unread counters and state. |
| `message_added`, `message_updated`, `message_deleted` | Individual message activity, independent of read state. |
| `resync_required` | The requested cursor cannot establish continuous coverage. |

Every message event identifies `space_id`, `chat_id`, `kind`, and `parent_id`
where applicable. Prefer the full existing message DTO with the new mention
fields for additions and edits in the first version. Deletions carry a message
ID. Full payloads avoid a mandatory extra fetch before processing a new mention.

A complete row update may replace intermediate counter states. Message triggers
must remain individually recoverable; a latest-message preview alone is not a
queue of all mentions or comments.

### Search stream

Deliver an initial result snapshot and then `object_added`, `object_updated`, and
`object_removed` events, or equivalent names consistent with the final API.
"Removed" means leaving the query result, including archive/deletion or access
loss; it need not mean physical deletion.

The contract must cover the complete matching set. A limited live window of the
most recent results is insufficient for watching all agent-created objects.
Support a consistent paged snapshot if the full set cannot be sent at once.

Changes to the `discussion` reference must emit an update even when the object
continues to match `creator`. Result removal removes the ownership-based watch
reason; other explicit watch reasons can remain if the conversation is still
accessible. Loss of access always stops reads, turns, and sends for that target.

### Internal Heart services and RPCs

Reuse existing mechanisms where their semantics fit:

- `Chat.SubscribeToMessagePreviews` already follows newly discovered chats and
  exposes previews/state. Its current discovery filter excludes discussions;
  it is not a complete, durable message journal.
- Object index subscriptions can back streamed search membership and row updates.
- `ChatGetMessagesByIds` can back the optional single-message HTTP lookup.

Add a space-scoped chat-event subscription service for the unified feed. If it
also needs a protobuf RPC surface, `Chat.SubscribeToEvents` and
`Chat.UnsubscribeFromEvents` are proposed names. The HTTP handler may instead
call the internal service directly. Preserve existing desktop preview behavior.

Watch changes to parent `discussionId` as well as newly indexed chat objects.
Do not implement space discovery by allocating a full independent HTTP-style
message subscription for every discussion. Share backend observation where
possible. Review unread-list filtering cost for spaces with many discussions;
the current local implementation scans candidate rows to filter unread state.

## 6. Resume, enrollment, and delivery rules

1. **Close the snapshot/subscription gap.** Return a snapshot watermark and replay
   changes after it, or perform snapshot plus subscription as one operation. A
   changing offset-paged list followed by an unrelated live stream is insufficient.
2. **Use stream-specific cursors.** SSE `id` and `Last-Event-ID` use an opaque cursor
   for that stream. Do not reuse one chat's `last_state_id` as a space cursor. Bind
   search cursors to the query and caller scope.
3. **Document retention and reset behavior.** Replay retained events; emit
   `resync_required` when continuity cannot be established. Use a durable local
   activity journal if message-trigger recovery must survive Heart restarts.
   A fresh search snapshot can reconstruct object ownership, but counters cannot
   reconstruct every missed mention already marked read elsewhere.
4. **Persist before acknowledging.** Atomically record enrollment and the
   triggering input before advancing the corresponding checkpoint. Accept
   at-least-once events and deduplicate inputs by account/space/chat/message ID.
5. **Preserve the first comment.** Discussion creation and its initial message
   may race with discovery. Queue/replay the message or recover it through history.
   Do not apply the existing startup "skip history" baseline to that trigger.
6. **Define initial history separately.** Bootstrap old history as context, not
   as automatic work. Process eligible activity after the agreed discovery
   watermark, including activity received while a new space worker starts.
7. **Keep read receipts independent.** Marking a conversation read is neither
   enrollment nor a processing acknowledgement.
8. **Preserve existing send recovery.** Agent-created objects must not be recreated
   because chat delivery failed. Status pubsub remains ephemeral, with explicit
   final tool statuses; it is not the discovery or processing event journal.

The system does not promise exactly-once tool execution. Existing Hermes recovery
and durable delivery obligations continue to govern interrupted turns.

## 7. Gateway implementation changes

Replace the fixed configured-target map with a persistent registry containing
full space/chat IDs, kind, parent ID, watch reasons, response policy, and enrollment
boundary. Keep configured targets as pinned entries. Keep independent checkpoints
for chat discovery, creator-filtered search, and message processing as needed.

Update every target-dependent path: startup, inbound dispatch, replies, activity
callbacks, metadata lookup, reconciliation, and standalone/cron sends. Newly
watched targets must remain valid after restart and outside an active chat turn.
Bound simultaneous agent turns independently from the number of watched chats.

Use one chat event stream per space as the target architecture. Existing per-chat
streams can support a bounded migration, but Heart currently caps those streams
at 64 process-wide. Polling list/counter state remains useful for reconciliation;
it is not a reliable replacement for individual activity events.

Each space has its own home-channel choice. Scheduled jobs retain their originating
space and authorized destination. Do not silently route another space's jobs to
the original personal home channel.

## 8. Multiple spaces and confidentiality

Separate session IDs prevent conversation collisions; they do not enforce data
isolation. In the inspected Hermes version, persistent memory is profile-scoped,
and `session_search` can explicitly open another local profile's database. Broad
MCP credentials, readable host files, shared external memory, and generated skills
are additional routes for cross-space disclosure.

For spaces with different audiences, use a fresh Hermes profile and isolated
worker environment per space. Containers/VMs or enforced OS permissions must
prevent workers from reading other profiles, credentials, workspaces, or the
router's state. Separate processes under the same unrestricted user are not a
sufficient boundary. Isolating only the terminal tool leaves other in-process
tools, including session search, outside that boundary.

| Resource | Required scope |
| --- | --- |
| Memory, history, summaries, files, generated skills | One space; external memory namespaces must also be separate. |
| Anytype MCP | A key restricted to that space, or an enforcing proxy outside the worker. |
| Other connectors and secrets | Granted independently to the space worker; do not inherit personal access. |
| Outbound messages and jobs | Destinations in the worker's permitted space. |
| Creator-search and discussion context | Queries, caches, and results scoped to the worker's space. |

Start profiles with empty private state. Hermes profile cloning can copy memory
and credentials; only intentionally shared nonprivate configuration or skills
should be reused. Prompt instructions about scope supplement enforced permissions.

For one visible agent account, use a trusted discovery/router component outside
the workers. It discovers spaces and owns account-level chat transport, then
delivers events only to the worker mapped to the authenticated event's space ID.
It verifies a worker's fixed space capability before accepting outbound requests.
Do not let model-supplied routing fields widen access.

This requires an internal router-to-worker adapter bridge; it is not a feature
obtained merely by adding another configured chat. Review the plugin's current
account-level connection lock and Hermes's duplicate-bot ownership rules. Keep a
single transport owner rather than blindly disabling those protections.

Heart's source already supports space-restricted API grants. Workers must not
receive the discovery component's broader credential. Verify that every MCP route
used respects the restricted grant, including global search; update an incompatible
wrapper rather than widening its key.

The multi-space onboarding sequence is:

1. The Anytype account joins a space and its API grant permits discovery.
2. The space stream or list reconciliation detects that the space is ready.
3. Local enrollment policy selects or provisions its isolated worker and scoped
   access. A preconfigured automatic policy can preserve the invite-and-mention UX.
4. Replay eligible chat activity after the onboarding watermark, retaining a
   mention that arrived while the worker was being prepared.
5. On removal or access loss, stop dispatch and delivery and cancel its watches.

A key restricted to previously configured spaces cannot discover a new space
until granted access. An all-spaces discovery grant can cover future spaces, but
belongs only in the trusted routing component.

Space-level isolation assumes members of that space share the intended audience.
If private chats or objects have a narrower audience, use a narrower isolation
boundary or enforce those permissions through memory, tools, and delivery too.

## 9. Delivery sequence and acceptance checks

### Stage 1: one-space multi-chat

Implement list/discussion metadata, structured mentions, and the space-wide chat
stream. Implement the creator-filtered search stream with discussion projection.
Then add dynamic watches, shared discussion sessions, parent context, and recovery
to the plugin. Keep the existing message/attachment/status delivery contracts.

Acceptance checks:

- A permitted mention enrolls an unknown chat and receives one logical response.
- An unauthorized mention cannot enroll a chat or execute commands.
- A mentioned discussion receives the reply and correct parent context.
- A first comment under an agent-created object is handled without a mention,
  including when it arrives before discovery catches up.
- Existing agent-created objects are found on bootstrap without MCP interception.
- New comments in an existing discussion arrive without a parent-object update.
- Read-state changes cannot hide a new mention from the event consumer.
- Reconnects, duplicate events, expired cursors, and restarts have explicit behavior.
- Parent edits refresh context; archiving/deletion/access loss reconcile watches.
- Many watched discussions do not require one existing SSE connection each.
- Standalone jobs and activity publication work for dynamically enrolled targets.

### Stage 2: isolated multiple spaces

Add space lifecycle discovery, the routing bridge, worker provisioning, scoped
credentials, and per-space home channels. Verify isolation before admitting users
from another audience.

Acceptance checks:

- Joining a permitted space and mentioning the account reaches the correct worker,
  including during initial worker startup.
- A request in Space B cannot retrieve Space A's objects, transcripts, memory,
  files, generated skills, or credentials through any enabled tool.
- Cross-profile session search and host file access cannot cross the boundary.
- MCP calls, global search, direct HTTP access, and outgoing delivery reject an
  attempted target outside the worker's space.
- Jobs, delegates, and retries retain the same space boundary.
- Space removal or permission revocation stops further access and delivery.

## 10. Research basis

The research checked the local API on port 31009, Heart source, and installed
Hermes on 2026-10-06. The served OpenAPI still returned only `id` and `name` for
chat-list rows; local Heart source already contained discussion inclusion and
unread list fields. Stream additions in this document remain proposed.

Installed Hermes baseline inspected:
`653bc4f288fc00db362c1082a3b652542314fbef`. Existing deployment and test evidence is
recorded in [VALIDATION.md](VALIDATION.md); that evidence does not validate the
extensions proposed here.

Primary references:

- [Hermes platform adapter documentation](https://hermes-agent.nousresearch.com/docs/developer-guide/adding-platform-adapters/)
- [Hermes profiles documentation](https://hermes-agent.nousresearch.com/docs/user-guide/profiles/)
- This repository: [adapter.py](../adapter.py), [config.py](../config.py), and
  [state.py](../state.py).
- Heart checkout: `core/api/v2/service/chat.go`, `chat_stream.go`, `search.go`,
  `discussion.go`, `object.go`; `core/api/v2/model/chat.go`, `model.go`;
  `core/block/chats/service.go`, `chatmodel/chatmodel.go`; `core/api/util/grant.go`;
  and `pb/protos/commands.proto`.
- Installed Hermes: `gateway/session.py`, `gateway/run_inbound.py`,
  `tools/memory_tool.py`, `tools/session_search_tool.py`, and
  `hermes_cli/profiles.py`.
