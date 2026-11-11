# Anytype gateway for Hermes

Chat with Hermes in Anytype. Mention the agent in a chat, ask it to research a
topic, and continue the conversation in the discussion under its research object.

Anytype Desktop or CLI provides the local account, data and API. This plugin
connects its chats to Hermes and sends replies and tool activity. Optional Anytype
MCP gives the agent tools to search your spaces, create objects and manage tasks.
Replies support Markdown, object links and attachments. You choose who can use
the agent and a home chat for scheduled jobs.

## Install

You need Hermes with a configured model, Git, and Anytype with API v2 chat/search
streams. The [installation guide](docs/INSTALL.md) covers CLI/Desktop options,
existing Hermes users and fresh macOS/Linux setups.

With Hermes installed, install the plugin from GitHub:

```sh
hermes plugins install anyproto/anytype-hermes --enable
```

<details>
<summary>Or copy this prompt into an agent on the target machine</summary>

```text
Help me install and set up https://github.com/anyproto/anytype-hermes.
Read docs/INSTALL.md and follow its agent-assisted setup instructions.
Use my current Hermes profile unless I choose another. Guide me through
the Anytype client, bot account/name, space invitation and sender access.
Preserve existing settings, follow installer review prompts, and keep
credentials out of chat and logs. Verify the saved connection with doctor
and guide me through a real mention test.
```

</details>

The repository is private and requires GitHub access. See the
[installation notes](docs/INSTALL.md#3-install-and-enable-the-gateway) for source
review and the [profile example](docs/INSTALL.md#use-a-named-hermes-profile) to
select another Hermes profile.

Keep the gateway stopped during setup. With [Anytype CLI installed](docs/INSTALL.md#2-install-anytype-cli), run:

```sh
hermes anytype setup --mode cli
```

For the desktop app, [prepare its API and key](docs/SETUP.md#desktop-app), then use
`hermes anytype setup --mode desktop` instead.

Choose `save_stopped` at the Publication prompt, then `already_stopped` for
Gateway coordination. Confirm exclusivity only after closing this profile's other
Hermes/MCP clients. The default `defer` leaves the live configuration unchanged.
Once setup confirms it saved the connection, check it:

```sh
hermes anytype doctor --json
```

Resolve any reported `next_action`, then start:

```sh
hermes gateway run
```

The [setup guide](docs/SETUP.md) covers MCP, existing services and interrupted setup.
Use a separate bot account with editor access or higher. Send requests from your
allowed human account; the gateway ignores its own account's messages.

## Try it

With MCP connected, try this from your human account:

1. Use Anytype's account-mention picker to mention the bot and ask:
   “Research three options for our team wiki. Save the comparison as an object
   in this space and send me the link.”
2. Open the object linked in the reply.
3. In its discussion, ask: “Which option would you pick for a team of five?”

With discovery and agent-object watches enabled, the gateway watches that
discussion and includes its parent object as context. For a chat-only check,
mention the bot and ask it to say hello.

## Chats and spaces

With discovery enabled, a mention adds the chat to the watch list. Replies to
the bot can also add a chat.

Multi-space mode lists accessible spaces once at startup. Restart the gateway after
joining a space or extending the API key's access. Chat and object changes stream
continuously within those spaces.

Allowlists control who can invoke the agent. They do not isolate its MCP tools,
memory, shell or other data access between spaces. Use this setup with trusted
participants. See [chat configuration](docs/OPERATIONS.md#chats-and-spaces) for
discovery, shared sessions and permissions.

## Hermes compatibility

Gateway 0.6.0 runs on stock Hermes without core patches. Tool activity uses Hermes's
public hooks. See [tested versions and validation](docs/VALIDATION.md) for the exact
Hermes revision, API requirements and remaining live checks.

Use `hermes anytype status` to inspect chat health and receipt counts. See
[troubleshooting](docs/SETUP.md#troubleshooting) for connection problems, and
[operations](docs/OPERATIONS.md) for updates, removal, scheduled destinations and
recovery limits. A crash can require a resend or produce a recovered reply marked
as a possible duplicate.

For configuration fields, see [hermes.example.yaml](hermes.example.yaml).
The [submission guide](docs/HERMES_PLUGIN_SUBMISSION.md) tracks Hermes catalog inclusion.
