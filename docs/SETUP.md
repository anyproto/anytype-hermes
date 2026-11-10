# Set up Anytype

Install the plugin first using [the installation guide](INSTALL-OTHER-COMPUTER.md).
Gateway 0.6 runs on the tested [stock Hermes revision](../README.md#hermes-compatibility)
without core patches. Use `hermes anytype setup` for the connection wizard.

Choose a connection:

| Mode | Use it when | What to prepare |
| --- | --- | --- |
| [CLI](#anytype-cli) | Hermes should guide account, service and space setup. | Anytype CLI v0.5.0 installed locally. |
| [Desktop](#desktop-app) | The bot runs in the Anytype desktop app. | A signed-in bot account, local API and API key. |
| [External](#externally-managed-api) | You already manage an Anytype service. | Its reachable API address and key. |

All paths finish with [saving and starting the connection](#save-and-start).
Commands below use the active Hermes profile. See the
[named-profile example](INSTALL-OTHER-COMPUTER.md#use-a-named-hermes-profile)
to install and configure another one.

The bot account and your human account must both belong to the space. The bot
needs editor, admin or owner membership in every space selected during setup.
Use your human account in the sender allowlist; the gateway ignores the bot's
own messages. Allowlists do not restrict the agent's wider MCP, memory or tool access.

## Anytype CLI

Use released [Anytype CLI v0.5.0](https://github.com/anyproto/anytype-cli/releases/tag/v0.5.0)
as the supported baseline. The [fresh-install walkthrough](INSTALL-OTHER-COMPUTER.md#2-install-released-anytype-cli)
includes installation and a capability check. Hermes checks the CLI's automation
capabilities before making changes.

For a new bot, follow [the wizard choices](INSTALL-OTHER-COMPUTER.md#4-run-the-onboarding-wizard)
for service installation, account creation, agent name, invitation and HTTP MCP.
That walkthrough also selects private-file credential storage, which works on
headless machines without a desktop keyring.

For an existing local CLI account and running service:

```sh
hermes anytype setup --mode cli
```

Choose `reuse` for both Native service and Native account. If a matching service
is installed but stopped, choose `start` for the service. Use `login` only when
restoring an account into a CLI instance without one; supply the recovery key at
the private prompt. Setup preserves an existing account's name and refuses
account replacement.

Native setup manages the CLI's one default instance on macOS or Linux. A separate
Hermes profile does not create a separate Anytype instance. The CLI owns its
service and recovery credentials; stopping Hermes leaves Anytype running.
If setup reports pending invitation approval, approve it from your human account,
then [resume](#resume-or-repair-setup). Continue to [save and start](#save-and-start).

## Desktop app

Prepare the desktop app before starting the wizard:

1. Sign in as the bot account. Invite it to the intended space from your human
   account and grant editor access or higher. Keep the desktop app running while
   Hermes uses its API.
2. Open **Settings → Local API** (called **API keys** in older builds). Enable
   the local API and confirm it is listening. Note the displayed address; the
   wizard defaults to `http://127.0.0.1:31009`, but your app may use another port.
3. Choose **Create new** to create an API v2 key. Give the integration a stable
   name, such as `Hermes`, and select **Read and write**. For the wizard's `all`
   discovery choice, grant **All spaces**; for `single`, grant exactly that space.
   Keep the application name when replacing an existing integration key.
4. Copy the key for the wizard's private prompt. The account recovery key is a
   different credential and is not used for desktop API authentication.

Run:

```sh
hermes anytype setup --mode desktop
```

Enter the API address and application key, then choose the matching discovery
scope. Select `operator` access and your human account identity from the displayed
members. Choose a home chat for scheduled output, or `skip` if you do not need one.

At the MCP prompt, choose `http` only if your app provides an HTTP MCP endpoint.
Enter the endpoint reported by that build and confirm whether it shares the API
key. The CLI's built-in MCP support does not establish support in every desktop
build. Choose `none` for chat-only operation, or use the
[Node wrapper option](#mcp-options). Then [save and start](#save-and-start).

The wizard connects to the desktop API; it does not create the desktop account,
join spaces, enable the API or issue its key. A build without the required API v2
chat/search streams must be updated before discovery can work.

## Externally managed API

Start the service and join the bot to the space through its owning client first.
Obtain a readwrite API key with the same scope rules described for
[desktop setup](#desktop-app). Use an address reachable from the Hermes process;
inside a container, `127.0.0.1` refers to that container.

```sh
hermes anytype setup --mode external
```

Supply the API address and key, choose the sender policy and home chat, then
select [MCP](#mcp-options) and [save the connection](#save-and-start).
Hermes does not install, start or stop an external Anytype service.

## MCP options

MCP gives the agent tools to search and edit objects. The gateway handles chat.

| Choice | Requirements |
| --- | --- |
| `http` | Native CLI setup discovers its built-in endpoint and uses the same key. Desktop/external setup needs the backend's actual URL and authentication details. |
| `wrapper` | Install/cache `@anyproto/anytype-mcp@2.0.1` with Node/npm before setup. Validation runs offline and never installs it for you. |
| `none` | Chat-only operation. Remove an existing enabled Anytype MCP connection explicitly if switching to this choice. |
| `keep` | Retain an existing connection after setup verifies its account and grant. |

To prepare the wrapper cache, run this once, wait for the server to start, then
stop it with Ctrl-C before setup:

```sh
npx -y @anyproto/anytype-mcp@2.0.1
```

The MCP connection must match the gateway's account and intended API grant.
Setup offers explicit replacement choices when a connection already exists.
Protocol details and automation fields are in the [MCP reference](SETUP_REFERENCE.md#mcp).

## Save and start

Keep the profile's gateway and other Hermes/MCP clients stopped while saving.
Stop a foreground gateway with Ctrl-C, or use `hermes gateway stop` for its service.
Keep Anytype running.

At the wizard's final prompts:

| Prompt | Choice for a manual start |
| --- | --- |
| Publication | `save_stopped` |
| Gateway coordination | `already_stopped` |
| Other clients stopped and exclusive access | Confirm only after closing those clients; prevent concurrent starts or config edits until saving finishes. |

The default `defer` prepares and validates settings but leaves the live connection
unchanged. If you chose it, run `hermes anytype setup --resume` and select the
choices above. A gateway restart alone does not publish deferred settings.

After the wizard confirms the connection is saved:

```sh
hermes anytype doctor --json
```

Resolve any reported `next_action`. When the gateway and selected MCP connection
are ready, start the gateway:

```sh
hermes gateway run
```

From your allowed human account, use Anytype's account-mention picker to mention
the bot in a chat. A typed display name is not an account mention. For background
operation after this check, use the [service commands](INSTALL-OTHER-COMPUTER.md#5-check-and-start).

For setup to manage a known, unshared Hermes gateway service, choose `managed`
coordination instead. See [publication options](SETUP_REFERENCE.md#preparation-and-publication)
for activation, shutdown and rollback behavior.

## Resume or repair setup

Use these commands only for the corresponding situation:

| Situation | Action |
| --- | --- |
| Interrupted setup, deferred settings or approved invitation | `hermes anytype setup --resume` |
| Check an already saved connection | `hermes anytype doctor --json` |
| Inspect chat health and receipt counts | `hermes anytype status` |
| Abandon a candidate that has not been saved | `hermes anytype setup --cancel` |
| Restore settings from before a saved attempt | `hermes anytype setup --rollback` |
| Add a space after initial setup | `hermes anytype join` |

Cancellation does not undo created accounts, memberships, services or keys.
Rollback can be refused if configuration changed or the previous connection
cannot be verified. Follow the reported action rather than deleting setup state.
For desktop/external mode, join the space in the owning client before running
`join`. Restart the gateway after adding a space covered by its grant; space
enumeration happens at startup. See [joining details](SETUP_REFERENCE.md#resume-and-joining)
for grant or discovery changes.

An HTTP 401 means the saved credential was rejected: check the key and API
address, then run setup to repair the existing connection. A membership warning
requires editor access or higher in each selected space, even with a readwrite
key. Missing API routes require an updated Anytype build. Keep the profile's
`anytype/state.sqlite3` when upgrading or repairing.

For scripts and custom grants, use the [automation input reference](SETUP_REFERENCE.md#automation-input).
It covers private credential files, publication controls and exit codes.
[Test evidence and remaining live checks](VALIDATION.md) are recorded separately.
