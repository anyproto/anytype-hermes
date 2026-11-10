# Local deployment

Updated to **0.5.1** on 2026-10-09. The plugin now bundles the 287-word Anytype
workflow skill, registered as `anytype-platform:anytype`, and asks the agent to
load it for research, task management and Anytype object work. Four review lenses
completed; the research-loading trigger gap was corrected and rechecked.

All **114 plugin tests passed**, including real Hermes discovery, listing and
loading of the skill. All 23 installed files match the final archive. Gateway
PID 25283 is running with Anytype and Telegram connected. No core, configuration
or credential changes and no backups were needed. Records are under
`.runtime/bundled-skill-20261009/`. Model adherence to the skill remains a live
conversation acceptance check.

Updated to **0.5.0** on 2026-10-09. Structured tool activity now uses Hermes's
public pre/post tool hooks and final-turn hook. Routine status uses the optional
adapter status sink; native typing stays enabled. The obsolete activity bridge
was removed from Hermes core and its patch removed from the installed package.
The delivery/receipt patch remains unchanged across its five core files.

All **114 plugin tests and 113 selected Hermes gateway tests passed** in an
isolated checkout with only the recovery patch. The 22 package files were
verified after installation. The managed gateway restarted as PID 56796, with
Anytype and Telegram connected. Configuration, credentials, MCP and transport
state were preserved; no backups were created. No synthetic live activity or
messages were posted. Records are under `.runtime/tool-hooks-20261009/`.

Updated to 0.4.1 on 2026-10-09. Anytype now consumes Hermes's native status text
through its typing refresh and Heart `/status`. The generic label shows elapsed
time after 30 seconds; structured tool calls retain their own refresh and terminal
updates. `display.platforms.anytype.long_running_notifications: false` disables
the separate persistent Working messages, with `live_status: full` and
`tool_progress: off` effective. No new core patches or backups were needed.

All **101 plugin tests passed** against the matching Hermes `1ac3f97f` code.
The 21 installed package files match the archive. The managed restart completed
with gateway PID 49918 running and both Anytype and Telegram connected. No
synthetic live messages or activities were published; client rendering remains
the live conversation check. Installation and verification records are under
`.runtime/native-status-20261009/`.

Updated to 0.4.0 on 2026-10-07: `discovery.spaces: all` now performs one complete
space listing at gateway startup, with independent chat/search streams afterward.
Restart Hermes after adding or granting another space. Installed on the same
Hermes `1ac3f97f` with the existing core hooks, following 98 passing plugin tests.
No backup was created. The managed restart verified all four spaces connected,
14 discovered chats/discussions, 59 creator-owned objects, and Telegram connected.
Sender/admin allowlists, pinned chats, home, MCP, and state remain unchanged.

Installed and enabled on 2026-10-07 (Europe/Berlin): plugin 0.3.0 on Hermes
`1ac3f97fbe0958aa2743c0c26202b20b3b4e760e` (0.21.5). Restored the existing generic
delivery and activity patches after the Hermes update. No backup was created,
as requested. All 90 plugin and 113 selected Hermes gateway regression tests
passed against an isolated copy of this commit with freshly locked dependencies.

Single-space discovery is enabled for the existing Hermes space, with
`watch_agent_objects: true` and `response: all`. The two pinned chats, home channel,
sender/admin allowlists, credentials, MCP configuration, and state database were
preserved. Shared discussion sessions use `thread_sessions_per_user: false`;
ordinary group sessions keep the previous setting. The managed gateway was
restarted and reports Anytype and Telegram connected. Discovery initialized with
2 pinned chats and 34 creator-owned objects. A real new-chat/discussion mention
remains the user-facing acceptance check.

Reinstalled after the Hermes update on 2026-09-30: plugin 0.2.1, runtime commit
`653bc4f288fc00db362c1082a3b652542314fbef`, managed Python 3.14.7. The restored core
uses `hermes-delivery-context-653bc4f.patch` and the existing activity patch.
Backup: `/Users/roma/.hermes/backups/anytype-reinstall-20260930T170215Z`, containing
the pre-reinstall core files, plugin and configuration. Credentials, routing and
the Anytype state database were retained. Validation: 51 plugin tests and 113
Hermes gateway regression tests passed in an isolated current-version checkout.

Activity update, 2026-09-25: version 0.2.0 adds ephemeral thinking and tool-call
status, enabled by `gateway.platforms.anytype.extra.activity_status`. It requires
the additional generic `hermes-activity-callbacks.patch`. The replacement API key
was saved in the profile `.env`; it is not stored in the plugin or package.
After restarting the updated Anytype app, the served schema includes the status
endpoint. A live check through the installed callbacks verified transient activity,
two-second refresh and explicit completion, all accepted with HTTP 200. The gateway
reconnected automatically to both Anytype chats. Cross-client rendering is still
an acceptance check; see [the validation record](VALIDATION.md).

The activity update passed 44 plugin tests and 22 Hermes regression tests against
the installed runtime. Its backup is
`/Users/roma/.hermes/backups/anytype-activity-20260925T161612Z` and includes the prior
profile environment, configuration, plugin and modified core files.

Installed on 2026-09-24 in the default Hermes profile at `/Users/roma/.hermes`.

- Runtime: `/Users/roma/.hermes/hermes-agent`, commit `30565b2db235d48d3f1ab42bcc63645a45dd94fa`, with the included generic delivery-context and activity-callback patches.
- Plugin: `/Users/roma/.hermes/plugins/anytype-platform`.
- Target: the **General** chat in the **Hermes** space, through `http://127.0.0.1:31009`.
- Additional target and Anytype home channel: **Hermes Home**, created at the user's request. Cron jobs with `deliver: anytype` resolve here. Anytype's agent guidance prefers this destination for newly requested scheduled jobs; explicit destinations and other platforms' homes retain normal Hermes behavior.
- Sender and operator: **Roma Kha** only, configured by account identity. The API account is **Agent**.
- Configuration: `/Users/roma/.hermes/config.yaml`; API key stored only in that profile's `.env`.
- MCP: local `@anyproto/anytype-mcp@2.0.1`; all 51 tools discovered by the running gateway, with an authenticated read-only request verified using Hermes's resolved configuration.
- Existing launchd gateway refreshed and restarted; both Anytype and Telegram report connected. Existing startup-at-login behavior retained.
- Backups: `/Users/roma/.hermes/backups/anytype-setup-20260924T154904Z`, including prior configuration, environment, service definition, and the five patched source files.

All 30 plugin tests and 88 Hermes regression tests passed against this installed runtime. The plugin test harness now honors `HERMES_SOURCE`, so these checks use the selected installation rather than the earlier isolated checkout.

Initial setup created no live messages or objects. The subsequent home-channel setup created **Hermes Home** and installed plugin version 0.1.1. All **32 plugin tests** pass, including cron home routing through real Hermes discovery. Desktop/mobile rendering and a live restart during a conversation remain acceptance checks.

```sh
hermes gateway status
hermes anytype status
```

Keep Anytype running for its local API and synchronization. Hermes updates must retain or replace the generic patches' contracts; the plugin checks for the required delivery and activity hooks at connection time.
