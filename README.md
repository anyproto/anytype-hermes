# Anytype gateway for Hermes

Chat with Hermes in Anytype. This plugin connects a local Anytype desktop app or
Anytype CLI to the Hermes gateway. Add Anytype MCP to let the agent search your
space, create research objects and manage tasks.

- Mention the agent to add a chat to its watch list.
- Continue conversations in object discussions, with the parent object as context.
- Follow discussions under objects the agent created.
- Receive Markdown replies, object links and attachments, plus live tool activity.
- Choose who can use the agent and a home chat for scheduled jobs.

## Install

Follow [the installation guide](docs/INSTALL-OTHER-COMPUTER.md) for a fresh
macOS/Linux setup. It covers Hermes, Anytype CLI, the bot account and space invitation.

With Hermes installed, build and enable the plugin from this checkout:

```sh
python3 scripts/package.py
mkdir -p "$HOME/.hermes/plugins"
unzip dist/anytype-platform.zip -d "$HOME/.hermes/plugins"
hermes plugins enable anytype-platform
```

These paths use the default Hermes profile. For another profile, follow the
[named-profile example](docs/INSTALL-OTHER-COMPUTER.md#use-a-named-hermes-profile).

Keep the gateway stopped while running setup. Choose one connection mode;
desktop users should first [prepare the API and key](docs/SETUP.md#desktop-app):

```sh
hermes anytype setup --mode cli      # Anytype CLI account and service setup
hermes anytype setup --mode desktop  # Connect to a running desktop app
```

Choose `save_stopped` at the Publication prompt, then `already_stopped` for
Gateway coordination. Confirm exclusivity only after closing this profile's other
Hermes/MCP clients. The default `defer` leaves the live configuration unchanged.
Once setup confirms it saved the connection, check and start it:

```sh
hermes anytype doctor --json
hermes gateway run
```

The [setup guide](docs/SETUP.md) covers credentials, MCP, existing services and
resuming interrupted setup. Use a separate Anytype account for the bot, with editor
access or higher. Send requests from your allowed human account; the gateway ignores
messages sent by the bot itself.

## Chats and spaces

With discovery enabled, use Anytype's account-mention picker to mention the bot.
The gateway then watches that chat. Replies to the bot can also add a chat, and
discussions under its objects can be watched automatically.

Multi-space mode lists accessible spaces once at startup. Restart the gateway after
joining a space or extending the API key's access. Chat and object changes stream
continuously within those spaces.

Allowlists control who can invoke the agent. They do not isolate its MCP tools,
memory, shell or other data access between spaces. Use this setup with trusted
participants. See [chat configuration](docs/OPERATIONS.md#chats-and-spaces) for
discovery, shared sessions and permissions.

## Hermes compatibility

Gateway 0.6.0 runs on stock Hermes without core patches. All 325 tests passed against
unmodified `ec3249919accf7bc51f780dcff663e8f19a0e176` on 2026-11-10.
Tool activity uses Hermes's public hooks.

Anytype must expose the API v2 chat and search streams. See
[validation and remaining checks](docs/VALIDATION.md) for the tested API contract,
and [migration guidance](docs/OPERATIONS.md#upgrading-from-a-patched-installation)
if your Hermes installation still has older patches.

## Recovery and operation

The gateway keeps chat watches, input receipts and outgoing message records across
restarts. Hermes handles execution and final-reply recovery. A crash can leave a
request needing a resend, or produce a recovered reply marked as a possible duplicate.

Use `hermes anytype status` to inspect local health and receipt counts.
[Operations](docs/OPERATIONS.md) covers recovery limits, scheduled destinations,
tool activity and development commands.

For configuration fields, see [hermes.example.yaml](hermes.example.yaml).
For implementation details, see the [specification](SPEC.md) and
[test evidence](docs/VALIDATION.md). The [plugin submission guide](docs/HERMES_PLUGIN_SUBMISSION.md)
tracks the steps toward Hermes catalog inclusion.
