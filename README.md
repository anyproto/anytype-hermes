# Anytype platform for Hermes

An external Hermes gateway plugin for Anytype API v2. It receives HTTP SSE chat events and sends `text` replies with optional object/file attachments and reply anchors. It never sends a chat `blocks` field. Anytype MCP supplies the agent's space tools.

See [validation evidence and remaining checks](docs/VALIDATION.md), [the specification](SPEC.md), and [the configuration example](hermes.example.yaml).

The [setup guide](docs/SETUP.md) documents standalone onboarding and diagnostics.
The [onboarding specification](docs/ONBOARDING_SETUP_CLI_SPEC.md) also covers the
native CLI automation. The setup guide identifies the reviewed CLI schema/capability
contract and documents native account/service/join/key setup and Heart HTTP MCP.

## Features

- Explicit space/chat targets, account-identity or participant-ID allowlists, and anyone mode.
- Bot echo suppression, per-user group sessions, and all-message or literal-prefix triggers.
- Final replies, reply context, object/collection links, native attachments, and UTF-16-safe chunking.
- Durable handoff receipts, restart catch-up, periodic history reconciliation, and persisted per-chunk sends.
- Scoped credentials and state, standard Hermes commands/approvals, and explicit-target standalone sends.
- Local `@anyproto/anytype-mcp@2.0.1`, verified against the running API.
- Ephemeral thinking/status labels and tool execution activity through Heart's chat `/status` route.
- Optional multi-space discovery at startup: enroll on real mentions/replies, follow discussions
  under objects created by the agent account, and load the parent object as context.

## Multi-space chats and discussions (0.4)

Requires Heart branch `go-0000-search-stream`, verified against source commit `a8f4f0887`.
It needs both `GET /v2/spaces/{space_id}/chats/stream` and
`POST /v2/spaces/{space_id}/search/stream`. An app still serving the older schema
must be rebuilt/restarted with those endpoints before discovery can connect.

Add this to the platform's `extra` configuration:

```yaml
discovery:
  spaces: all
  watch_agent_objects: true
  response: all  # or mentions, including replies to the agent
```

`spaces: all` performs one complete paginated `GET /v2/spaces?ids=full` listing at
gateway startup, then opens independent chat and creator-search streams in each
accessible space. There is no recurring space-list poll. Joining a space or granting
the key another space requires a gateway restart. Existing chat/object subscriptions
continue streaming normally. Space failures and initial snapshots are independent;
credential rejection suspends all spaces. Heart currently permits 16 space-chat
and 16 search streams process-wide; more than 16 discovered spaces fails explicitly.

For the previous single-space behavior, replace `spaces: all` with
`space_id: "<full-space-id>"`. Configure only one selector. `chats` remains the
pinned/home-channel list and may be empty in discovery mode; in single-space mode
all pins must belong to that space. Existing configurations without `discovery`
retain their per-chat transport and policies. Sender and admin allowlists still
apply. A display name or literal `@Agent` is not an identity mention: use Anytype's
actual mention UI. Replies to an agent-authored message can also enroll a chat.
Use account identities in the allowlist across spaces: member/participant IDs
belong to an individual space and are resolved independently.

The gateway keeps a durable chat catalog and independent pinned, mention, and
agent-created-object watch reasons. Creator search uses the current space member
ID; no MCP creation interception is needed. Search and chat snapshots reconcile
membership only when complete. A comment arriving before the creator result is
retained and reconsidered. Ordinary parent edits do not create agent turns.

Discussions use their actual chat ID as the delivery destination and a matching
Hermes thread ID. Set `thread_sessions_per_user: false` at the Hermes top level for
shared discussion history; an explicit `true` keeps participants isolated. Parent
content is bounded and fetched when each input is admitted. Hermes can queue or
merge inputs, so that excerpt is not guaranteed to be fresh at model execution.
Attachment/block references remain in merged input text.

Both new streams have fresh snapshots and **no resume cursor**. Recovery uses
paginated REST history when a chat's creation-state checkpoint advances, including
late-synced messages outside the newest-message window. It scans at most 100,000
messages per chat; incomplete coverage stays visible as `gap_unresolved`. Recent
history is refreshed on reconnect even without a new creation state. Older edits,
deletions during outages, and messages deleted before retrieval cannot be guaranteed.
Unread counters never acknowledge processing. Precommit events are verified through
REST before admission; unresolved visibility remains durable and is retried.

First setup persists a bootstrap cutoff before subscribing, using Heart's BSON
creation-state timestamps with a one-second overlap. Older history becomes baseline;
messages materialized at the cutoff or later remain eligible even if the process
crashes before consuming a buffered event. The overlap can include a message from
just before startup. Historical backfill after a chat reappears does not revive
unchanged baseline requests. Multi-space mode retains its first activation cutoff
for subsequently discovered spaces, so their downtime mentions can be recovered on
restart. Messages older than initial activation remain baseline. Restarts retain
watches and receipts, including dynamically enrolled cron destinations. A home
channel applies to its own space; agent guidance defaults to `deliver: origin` in
other spaces. Existing jobs explicitly targeting the home retain that destination.
Standalone delivery rejects unwatched, unavailable, mismatched
thread, and out-of-space destinations.

This release implements transport discovery, not tenant isolation. Profiles,
prompts, and transport routing do not restrict broader MCP, memory, shell, or
connector access. Restricting MCP calls, memory, and outputs to individual
collaborators' permissions remains future work; the current allowlist is preserved.

Binary upload/download hooks, incremental edits, read receipts and reply-only triggers are follow-ups. Incoming attachments are supplied as object references for MCP. Existing file/image object IDs can be attached to outgoing messages.

## Hermes compatibility

For the route to reviewed catalog inclusion and an explanation of the remaining
core dependencies, see [the plugin submission steps](docs/HERMES_PLUGIN_SUBMISSION.md).

Version **0.6.0** runs on stock Hermes, with no activity or recovery patches.
Validated against unmodified `ec3249919accf7bc51f780dcff663e8f19a0e176`
(`main`, 2026-11-10). Tool activity uses the public `pre_tool_call`,
`post_tool_call` and `post_llm_call` hooks; typing and routine status use the
adapter interfaces. See [validation](docs/VALIDATION.md) for test coverage and limits.

The previous implementation, including its recovery patches, is preserved in
commit `02555cd`. Historical patch files remain in the repository for migration
reference and are excluded from release archives. Do not apply them to current
Hermes. When upgrading an existing patched installation, use a clean upstream
checkout/environment and keep the owning profile's configuration and Anytype
state database. Do not blindly reverse old patches over newer Hermes code.

## Install and configure

1. Use the stock Hermes revision above. No core patch is needed.
2. Run `python3 scripts/package.py`. Extract `dist/anytype-platform.zip` into the owning profile's `plugins` directory. The resulting directory must be `<profile-home>/plugins/anytype-platform/`, containing `plugin.yaml` and `__init__.py`.
3. Merge [hermes.example.yaml](hermes.example.yaml) into that profile's `config.yaml`. Append `anytype-platform` to any existing `plugins.enabled` list and keep existing model/provider settings.
4. Put `ANYTYPE_API_KEY=<your key>` in the owning profile's `.env` or secret store. Fill in full space/chat IDs and your **other account's** identity or participant ID. Use `group_allow_from: ["*"]` for anyone in those configured chats.
5. Keep explicit operator IDs in `group_allow_admin_from`. An empty admin list disables Hermes's slash-command privilege gate; this is independent of chat admission.
6. Enable the Anytype MCP tools for the relevant Hermes profile/platform using Hermes's normal tool configuration. Start that profile's gateway using the stock Hermes installation.

Both the gateway and MCP subprocess must reach the same API origin. The example uses `http://127.0.0.1:31009`; in a container, loopback belongs to the container. Gateway and MCP secrets resolve through the owning profile. Never copy the example over an existing profile wholesale.

The API account is the bot: it must belong to the configured space, as must the other account that will send requests. Messages sent manually from the bot account are intentionally ignored.

`group_sessions_per_user: true` separates each participant's agent context. Setting it to `false` shares a conversation within each chat and also changes group behavior for other platforms in the same profile. Tool access follows the routed Hermes profile, not the sender allowlist.

In prefix mode, use `!hermes your request`. Authorized slash commands and active clarification/approval replies can bypass the prefix through standard Hermes control handling. Replies are sent as Markdown, which Anytype parses into inline marks (bold, italic, code, links, mentions). If Anytype rejects a chunk's markup, that chunk is resent with the markup escaped into readable text; links stay intact.

## Object workflows

The plugin bundles a small [Anytype workflow skill](skills/anytype/SKILL.md),
registered as `anytype-platform:anytype`. It covers searches, research objects,
tasks, discussion context, object links and scheduled destinations. The gateway
hint asks the agent to load it with `skill_view` for research, task management or Anytype object work;
essential space and delivery rules remain in the hint. Hermes exposes the skill
through `skills_list`; it is read-only and is not automatically added to the
system prompt's skill index. Anytype MCP remains a separate optional connection.

The skill favors one object for substantial research, related records or a
collection when useful, and the space's existing types/properties. Brief replies
remain in chat; explicit user preferences take precedence.

```text
[Research report](anytype://object?objectId=OBJECT_ID&spaceId=FULL_SPACE_ID)
```

Explicit same-space object links in the final answer become native attachments when `attach_object_links` is enabled. Cross-space links remain inline. Only the gateway posts the normal final reply; MCP should not duplicate it. A failed chat send must not cause the agent to recreate successful objects.

## Recovery and operation

For cron and proactive notifications, set `gateway.platforms.anytype.home_channel`
to `{platform: anytype, chat_id: "<full-space-id>/<chat-id>", name: "Hermes Home"}`
and include that chat in `extra.chats`. Cron jobs using `deliver: anytype` resolve
to this home, including jobs created outside Anytype. The platform guidance asks
the agent to use this destination for scheduled jobs requested in Anytype unless
the user specifies another destination. Explicit destinations and `deliver: origin`
retain Hermes's normal routing behavior; other platforms keep their own homes.

The first connection establishes a skip baseline. Later reconnects restore checkpoints and recover downtime messages. State is stored under `<profile-home>/anytype/state.sqlite3`, bound to API origin, bot account and target. Rotating a key for the same account does not reset history.

```sh
hermes anytype status
# Stop this account's gateway before an offline history reconciliation:
hermes anytype reconcile --deep
```

Use the selected profile's normal Hermes CLI option when invoking these commands. Status exposes health, counts and uncertain operation IDs without printing message bodies. Deep reconciliation scans up to 100,000 retained messages per chat and records eligible inputs for dispatch when the gateway next starts. It does not run the agent or post replies.

Input receipts track transport handoff, not agent execution. Eligible inputs are
`pending`, then `handing_off` before calling Hermes, and `handed_off` when the
call returns. A handoff may queue, merge, filter or handle a command; it does not
prove durable admission, execution completion or successful delivery. An exception
or interruption leaves `uncertain`; the plugin never independently replays that
input. Hermes owns execution recovery. A crash after handoff but before durable
Hermes admission can therefore require the user to resend the request.

Existing checkpoints, watch state and receipts are retained. On upgrade, old
`dispatched`/`processing` receipts become `uncertain`; old terminal receipts stay
terminal. New inputs arriving during an outage are still recovered from history.
Handed-off inputs do not count toward the plugin's pending-input limit.

Outgoing payloads and per-chunk API receipts remain persisted. Immediate retries
of an anchored reply reuse its operation and skip delivered chunks. Ambiguous
POSTs do not trigger an immediate retry or plain-text fallback. Unanchored partial
sends defer to Hermes recovery instead of silently repeating their delivered head.
Independent standalone sends, even with identical text, remain separate operations.

Hermes's standard delivery ledger owns delayed/restart recovery of final replies.
It saves text, chat and thread, but not our exact reply anchor, explicit attachment
metadata or operation ID. A recovery may send a new message with a visible
“may be a duplicate” marker; its original uncertain receipt stays visible.
Links remain in the recovered text and can regenerate available same-space previews.
Standalone/cron sends follow the recovery policy of their Hermes caller.

This is best-effort delivery, not an exactly-once system. Anytype's idempotency
cache is in memory. In legacy target-only mode, arbitrarily old out-of-order
additions can fall outside a finite history overlap: reconciliation checks 1,000
recent messages every five minutes and scans further when the prior boundary is
outside that window; `resync_required` requests a deeper scan. Discovery mode uses
the bounded REST recovery described above. Unresolved coverage remains visible.

Writes are paced to one per second per client, with server 429 guidance handled by the normal delivery ledger. MCP and other processes share Anytype's server-side budget. Do not automatically delete final chat messages or attached files for cleanup.

## Chat activity (0.5)

With `extra.activity_status: true` (the default), the gateway publishes to
`POST /v2/spaces/{space_id}/chats/{chat_id}/status`. Thinking and routine status use
text-only transient updates. Tool calls use a fresh ID for each execution, including
retries, with `in_progress`, `complete`, or `error` and an expandable JSON result.
The pre-tool hook runs before approval, so its label says `Preparing <tool>`;
`in_progress` includes time spent waiting for policy/approval. Completion uses the
actual result and terminal status, including blocked, cancelled and timed-out calls.
Parallel calls remain separate even when they use the same tool. Labels repeat on
every update; running calls refresh about every two seconds. Terminal updates stop
their refresh worker, and cancelled/interrupted runs explicitly settle unfinished calls.
`null`, `false`, zero and empty strings remain valid results.

Each processing task binds the exact adapter/publisher and full space/chat target.
Hermes carries this binding into agent and hook workers. Session/turn/request/call
IDs correlate updates; the plugin generates globally unique execution IDs for
Heart. Closed turns reject late callbacks. Delegated child tools, diagnostic-only
muted turns, standalone CLI/cron calls, and calls outside an Anytype processing
task do not publish activity into that chat; the parent delegation call is visible.
Scheduled result delivery still uses the configured destination.

Hermes's native `set_status_text` / typing refresh also uses this endpoint, even
with `tool_progress: off`. Between tool calls, the fallback label shows elapsed
time after 30 seconds. Timers and labels are scoped to the full space/chat key
(including object discussions) and cleared when typing stops. Running structured
tool calls take priority over the generic typing label.

The plugin uses Hermes's low display tier, which disables permanent progress and
long-running messages by default. Existing global display settings override tier
defaults, so explicitly set `display.platforms.anytype.long_running_notifications:
false` when upgrading. `live_status: full` includes native tool argument previews;
`verb` omits those previews, and `off` leaves the generic typing fallback. Structured
tool activity is controlled separately by `extra.activity_status`.

Activity bypasses the ordinary message pacing lock; Heart's write limit and retry
guidance still apply. A missing/unavailable status endpoint does not prevent chat
replies. Terminal publications retry for up to 30 seconds; shutdown allows five
seconds to flush them. A killed process or unavailable Heart can still leave stale
activity because pubsub has no replay. Staleness is never inferred to mean success.

Credentials are redacted through Hermes's egress scrubber; oversized activity is
visibly truncated below Heart's 65,508-byte limit. Activity is not written to the
message ledger or sent as chat messages/blocks. `display.tool_progress: "off"` can
remain set to suppress ordinary progress bubbles. Warnings and approval/clarification
prompts retain their normal delivery. Set `extra.activity_status: false` to disable
this lane; it does not enable streaming message edits.

## Development

```sh
bash scripts/prepare-dev.sh ../hermes-agent
# Use a Python environment containing the Hermes dependencies plus
# pytest, pytest-asyncio and pytest-timeout.
HERMES_PYTHON=/absolute/path/to/python bash scripts/run-tests.sh -j 3
```

The development helper creates a clean, detached upstream checkout and never applies patches. It refuses to replace an existing modified checkout; pass a new destination as its second argument and set `HERMES_SOURCE` for tests.

The wrapper invokes Hermes's prescribed `scripts/run_tests.sh`, including isolated homes, sanitized environment and per-file subprocesses. The local `.venv` is the default when `HERMES_PYTHON` is omitted. Source remains in this repository; `.dev`, `.venv`, `.runtime` and `dist` are excluded from version control. The package builder uses an explicit file allowlist and includes no token or runtime database.

Protocol, state and sender tests use fake HTTP responses. Integration tests load the real plugin/gateway paths and cover a full chat loop plus recovery with a fresh adapter. Live second-account conversation and Desktop/mobile rendering remain release acceptance checks.
## Guided setup and diagnostics

The standalone `hermes anytype setup` command prepares and validates an Anytype
connection before publication. It is available before an API key is configured.
`hermes anytype doctor --json` checks the live API, grant, account, home and MCP
independently. Existing `status` and `reconcile --deep` remain local operator tools.

See [the setup guide](docs/SETUP.md) for desktop/external endpoint onboarding,
private credential input, interrupted setup, consumer coordination and the current
schema-checked native CLI provisioning. Setup never provisions through Hermes's platform menu.
