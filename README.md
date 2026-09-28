# Anytype platform for Hermes

An external Hermes gateway plugin for Anytype API v2. It receives HTTP SSE chat events and sends `text` replies with optional object/file attachments and reply anchors. It never sends a chat `blocks` field. Anytype MCP supplies the agent's space tools.

See [validation evidence and remaining checks](docs/VALIDATION.md), [the specification](SPEC.md), and [the configuration example](hermes.example.yaml).

## Features

- Explicit space/chat targets, account-identity or participant-ID allowlists, and anyone mode.
- Bot echo suppression, per-user group sessions, and all-message or literal-prefix triggers.
- Final replies, reply context, object/collection links, native attachments, and UTF-16-safe chunking.
- Durable input receipts, restart catch-up, periodic history reconciliation, and persisted per-chunk sends.
- Scoped credentials and state, standard Hermes commands/approvals, and explicit-target standalone sends.
- Local `@anyproto/anytype-mcp@2.0.1`, verified against the running API.
- Ephemeral thinking/status labels and tool execution activity through Heart's chat `/status` route.

Binary upload/download hooks, incremental edits, read receipts and reply-only triggers are follow-ups. Incoming attachments are supplied as object references for MCP. Existing file/image object IDs can be attached to outgoing messages.

## Hermes compatibility

Version 0.2 also requires [the activity callbacks patch](patches/hermes-activity-callbacks.patch).
It adds a generic opt-in adapter hook for the ID-bearing tool callbacks and routine status
updates, preserving existing voice/card callbacks and warning delivery. Apply it after the
delivery-context patch. Both patches apply to the installed Hermes commit
`30565b2db235d48d3f1ab42bcc63645a45dd94fa`.

Tested with Hermes commit `aa8a33d22d27973df6591e48f38228274c8ad771` and the included [generic Hermes patch](patches/hermes-delivery-context.patch). The original `../hermes-agent` checkout remains unchanged; tests use an isolated copy in `.dev`.

The patch addresses two concrete gaps in that Hermes version:

1. Persist optional reply/attachment context and pass a stable delivery ID on initial and recovered sends. Opting-in adapters receive the original content and handle delivery ambiguity themselves. Existing adapters retain the recovered-message marker behavior.
2. Preserve original input IDs across merges, debounce and steering, and report inline control completion through lifecycle hooks. Queued follow-up hooks already exist in Hermes.

These changes are generic and contain no Anytype-specific branches. Apply them to the Hermes checkout you will actually run before enabling the plugin. The plugin refuses to connect when the required delivery hook is missing. Check applicability first:

```sh
git -C ../hermes-agent apply --check "$PWD/patches/hermes-delivery-context.patch"
git -C ../hermes-agent apply "$PWD/patches/hermes-delivery-context.patch"
git -C ../hermes-agent apply --check "$PWD/patches/hermes-activity-callbacks.patch"
git -C ../hermes-agent apply "$PWD/patches/hermes-activity-callbacks.patch"
```

## Install and configure

1. Apply both patches above, or use a Hermes release that supplies the same tested contracts.
2. Run `python3 scripts/package.py`. Extract `dist/anytype-platform.zip` into the owning profile's `plugins` directory. The resulting directory must be `<profile-home>/plugins/anytype-platform/`, containing `plugin.yaml` and `__init__.py`.
3. Merge [hermes.example.yaml](hermes.example.yaml) into that profile's `config.yaml`. Append `anytype-platform` to any existing `plugins.enabled` list and keep existing model/provider settings.
4. Put `ANYTYPE_API_KEY=<your key>` in the owning profile's `.env` or secret store. Fill in full space/chat IDs and your **other account's** identity or participant ID. Use `group_allow_from: ["*"]` for anyone in those configured chats.
5. Keep explicit operator IDs in `group_allow_admin_from`. An empty admin list disables Hermes's slash-command privilege gate; this is independent of chat admission.
6. Enable the Anytype MCP tools for the relevant Hermes profile/platform using Hermes's normal tool configuration. Start that profile's gateway using the patched Hermes installation.

Both the gateway and MCP subprocess must reach the same API origin. The example uses `http://127.0.0.1:31009`; in a container, loopback belongs to the container. Gateway and MCP secrets resolve through the owning profile. Never copy the example over an existing profile wholesale.

The API account is the bot: it must belong to the configured space, as must the other account that will send requests. Messages sent manually from the bot account are intentionally ignored.

`group_sessions_per_user: true` separates each participant's agent context. Setting it to `false` shares a conversation within each chat and also changes group behavior for other platforms in the same profile. Tool access follows the routed Hermes profile, not the sender allowlist.

In prefix mode, use `!hermes your request`. Authorized slash commands and active clarification/approval replies can bypass the prefix through standard Hermes control handling. Replies are sent as Markdown, which Anytype parses into inline marks (bold, italic, code, links, mentions). If Anytype rejects a chunk's markup, that chunk is resent with the markup escaped into readable text; links stay intact.

## Object workflows

The plugin supplies stable agent guidance to save substantial research as native objects, reuse the space's types/properties, and return links from confirmed tool results. Independently useful records can be grouped into a collection. Brief replies remain in chat; explicit user preferences take precedence.

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

Known unsent/rejected chunks can retry with their original payload/key. An uncertain send remains uncertain across restarts; a generic Hermes retry cannot turn it into a fresh POST. Review the local receipt and remote chat before resolving it. The initial operator CLI reports these cases; it does not yet provide a guided manual resolution command. Interrupted tool-bearing inputs are left to Hermes's recovery or reported as uncertain, never independently replayed by the plugin.

This is not an exactly-once system. Anytype's idempotency cache is in memory, and arbitrarily old out-of-order additions can fall outside a finite history overlap. Default reconciliation checks 1,000 recent messages every five minutes and scans further when the prior boundary is outside that window. A stream `resync_required` requests a deeper scan. Unresolved coverage remains visible.

Writes are paced to one per second per client, with server 429 guidance handled by the normal delivery ledger. MCP and other processes share Anytype's server-side budget. Do not automatically delete final chat messages or attached files for cleanup.

## Chat activity (0.2)

With `extra.activity_status: true` (the default), the gateway publishes to
`POST /v2/spaces/{space_id}/chats/{chat_id}/status`. Thinking and routine status use
text-only transient updates. Tool calls use a fresh ID for each execution, including
retries, with `in_progress`, `complete`, or `error` and an expandable JSON result.
Parallel calls remain separate even when they use the same tool. Labels repeat on
every update; running calls refresh about every two seconds. Terminal updates stop
their refresh worker, and cancelled/interrupted runs explicitly settle unfinished calls.
`null`, `false`, zero and empty strings remain valid results.

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

The wrapper invokes Hermes's prescribed `scripts/run_tests.sh`, including isolated homes, sanitized environment and per-file subprocesses. The local `.venv` is the default when `HERMES_PYTHON` is omitted. Source remains in this repository; `.dev`, `.venv`, `.runtime` and `dist` are excluded from version control. The package builder uses an explicit file allowlist and includes no token or runtime database.

Protocol, state and sender tests use fake HTTP responses. Integration tests load the real plugin/gateway paths and cover a full chat loop plus recovery with a fresh adapter. Live second-account conversation and Desktop/mobile rendering remain release acceptance checks.
