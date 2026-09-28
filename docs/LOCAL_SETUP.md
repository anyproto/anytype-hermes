# Local deployment

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
