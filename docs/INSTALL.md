# Install the Anytype gateway

This guide covers macOS/Linux. Commands use the active Hermes profile; the
[named-profile example](#use-a-named-hermes-profile) shows how to select another one.

- **Hermes already installed:** configure its model/provider with `hermes setup`
  if needed. Continue with [Anytype CLI](#2-install-anytype-cli), or go straight to
  [plugin installation](#3-install-and-enable-the-gateway) for Desktop/external mode.
- **Fresh installation:** start at step 1.
- **Updating an existing gateway:** use the [update instructions](OPERATIONS.md#update-the-plugin).

Choose how Anytype will run:

| Connection | Use it when | Required before connection setup |
| --- | --- | --- |
| Anytype CLI | You want a background service, including on a headless machine. | CLI v0.5.0; the wizard can create the bot, install its service and join a space. |
| Desktop app | You already use the bot account in Desktop. | Running app, API v2 enabled, API key, and bot membership in the space. |
| External API | You manage the Anytype service yourself. | Reachable API address, key, and bot membership in the space. |

The bot needs a separate account from your human account and editor access or
higher in each selected space. The API must provide chat and search streams.
Gateway 0.6 runs on stock Hermes without patches; [validation](VALIDATION.md)
records the exact tested versions and limits.

## 1. Install Hermes

For a fresh installation, use the tested stock revision below:

```sh
curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/ec3249919accf7bc51f780dcff663e8f19a0e176/scripts/install.sh -o /tmp/hermes-install.sh
bash /tmp/hermes-install.sh --commit ec3249919accf7bc51f780dcff663e8f19a0e176 --skip-setup
export PATH="$HOME/.local/bin:$PATH"
hermes setup
```

Configure the model/provider and leave the gateway stopped until Anytype setup
finishes. For an existing patched installation, retain its profile and state,
and use a clean stock runtime; do not apply archived patches or reverse them
blindly over another revision. These fresh-machine commands are not an in-place
migration procedure.

## 2. Install Anytype CLI

Skip this step for Desktop or an externally managed API.

[Anytype CLI v0.5.0](https://github.com/anyproto/anytype-cli/releases/tag/v0.5.0) includes the JSON automation interface and built-in HTTP MCP used by the wizard. The official installer downloads the current release:

```sh
curl -fsSL https://raw.githubusercontent.com/anyproto/anytype-cli/v0.5.0/install.sh -o /tmp/anytype-cli-install.sh
bash /tmp/anytype-cli-install.sh
export PATH="$HOME/.local/bin:$PATH"
anytype version
anytype capabilities --json
```

The capabilities command must produce a schema-1 JSON envelope with `state: ready`, `account_create: true`, `account_login: true` and `embedded_http_mcp`. No development build directory needs to be added to PATH.

Do not create an account or start a service manually for the fresh-account path below: the wizard will invoke the CLI for those explicit choices. The CLI owns its OS service independently of Hermes.

## 3. Install and enable the gateway

With Git available, install from the repository:

```sh
hermes plugins install anyproto/anytype-hermes --enable
hermes plugins list --user
hermes anytype setup --help
```

The repository is currently private. Authenticate GitHub with an account that has
access, for example with `gh auth login`, if cloning fails. Hermes's scanner
currently returns `CAUTION` for this repository and asks for source review before
installation. Inspect its findings before deciding whether to continue; `--enable`
does not accept that prompt. The installed plugin is named `anytype-platform`;
installation does not start a chat connection.

The command follows the repository's default branch. For an exact version, append
`--ref <full-40-character-commit-SHA>`. A pinned installation must be explicitly
reinstalled at a new SHA to update. For a previous ZIP installation, follow
[conversion to native installation](OPERATIONS.md#replace-a-manual-installation).

The help output must contain setup options such as `--mode`, `--resume` and `--input`. Seeing only `status` and `reconcile` means an older plugin copy is being loaded.

For Desktop or an external API, continue with the [connection setup guide](SETUP.md).
For a new CLI bot, continue below.

## 4. Run the onboarding wizard

Keep this profile's gateway stopped: use Ctrl-C for a foreground process or
`hermes gateway stop` for its service. Close other Hermes/MCP clients using the
same profile before saving setup. Keep an existing Anytype service running.

Create a bot account and invite it to your space. If moving an existing bot from
another computer, use the account-restoration instructions below and stop its old
gateway before starting this one.

The small input file below explicitly selects the CLI's private-file credential backend, which also works without a desktop keyring. It contains no credentials. The wizard will ask for the remaining choices.

```sh
cat > anytype-setup.json <<'JSON'
{
  "schema_version": 1,
  "cli": {"credential_backend": "private_file"}
}
JSON
hermes anytype setup --mode cli --input anytype-setup.json
```

Select:

| Prompt | Choice for a new CLI bot |
| --- | --- |
| Native service | `install` (installs and starts Anytype's service) |
| Native account | `create` |
| Agent display name | Your chosen bot name, e.g. `Hermes` |
| Native network | `default`, unless your space uses a custom network |
| Join a space | `yes`, then enter the private invitation link |
| Discovery scope | `all` for all joined spaces accessible to the API key, or `single` |
| Sender access | `operator` |
| Operator identity | Your human/other account identity, from the displayed member list |
| Home chat | `create` or select an existing chat for cron/job output |
| Anytype MCP tools | `http` (the CLI's built-in server; no Node wrapper needed) |
| Publication | `save_stopped` |
| Gateway coordination | `already_stopped`; confirm exclusivity after closing other Hermes/MCP clients |

Choose Publication explicitly: the default `defer` prepares settings without saving
the live connection. If you accepted it, resume setup and select `save_stopped`
before starting the gateway.

The new account needs editor or higher membership. If the invitation needs approval, approve it from your other account and resume the same setup:

```sh
hermes anytype setup --resume
```

The wizard obtains and stores the API key and configures HTTP MCP. You do not need
to copy a token into shell commands. If the local CLI already has your bot account,
select `reuse`. To restore it into a CLI instance without an account, select `login`
and provide its account recovery key privately. See [existing CLI setup](SETUP.md#anytype-cli)
for service reuse and start choices.

## 5. Check and start

```sh
hermes anytype doctor --json
```

Resolve any reported `next_action`. When `doctor` reports the connection ready, start it:

```sh
hermes gateway run
```

Keep the foreground gateway running. From your allowed human account, use
Anytype's account-mention picker to mention the bot and ask it to say hello. Expect
a reply in the same chat. The mention adds that chat to the watch list; typing a
display name as ordinary text is insufficient. With MCP connected, try the
[research-object workflow](../README.md#try-it) next. If there is no reply, check
[troubleshooting](SETUP.md#troubleshooting).

Once the foreground test works, stop it with Ctrl-C and use Hermes's service lifecycle for background operation:

```sh
hermes gateway install
hermes gateway start
hermes gateway status
```

Anytype CLI's local API defaults to `http://127.0.0.1:31012`; the wizard discovers the effective API/MCP endpoints. Adding another space later requires a gateway restart because space enumeration currently happens once at startup.

## Use a named Hermes profile

Use this example to select a new profile named `anytype` explicitly. For an
existing profile, skip creation and substitute its name throughout.

After installing Hermes and Anytype CLI:

```sh
hermes profile create anytype --no-alias
hermes -p anytype setup
hermes -p anytype plugins install anyproto/anytype-hermes --enable
cat > anytype-setup.json <<'JSON'
{"schema_version": 1, "cli": {"credential_backend": "private_file"}}
JSON
hermes -p anytype anytype setup --mode cli --input anytype-setup.json
```

Configure that profile's model/provider with `hermes -p anytype setup`. Keep its
gateway stopped during Anytype setup and use the same wizard choices from step 4,
including `save_stopped` and `already_stopped`. For a desktop connection, replace
the last command with `hermes -p anytype anytype setup --mode desktop` after
[preparing the desktop API](SETUP.md#desktop-app).

Check the saved connection, resolve any `next_action`, then start that profile:

```sh
hermes -p anytype anytype doctor --json
```

```sh
hermes -p anytype gateway run
```

After a successful foreground test, stop it with Ctrl-C. For background operation:

```sh
hermes -p anytype gateway install
hermes -p anytype gateway start
hermes -p anytype gateway status
```

Keep `-p anytype` on subsequent setup, join, status and gateway commands. This
selects the Hermes profile; the native Anytype CLI still has one default instance.
Do not run two gateways against the same bot account and chats.

## Agent-assisted setup

An agent on the target machine can follow this guide. Use the setup wizard or its
[JSON automation interface](SETUP_REFERENCE.md#automation-input) so account,
permission and MCP checks also run. Reuse an existing installation when possible.
Ask for the client mode, bot account/name, space invitation and sender policy as
needed. Use private prompts or the documented secret-file references for credentials.

If the agent runs inside the Hermes profile being configured, choose Publication
`defer` and hand off the final save/start to a separate terminal. Close that agent
and other clients using the profile, then run `hermes anytype setup --resume` and
follow [save and start](SETUP.md#save-and-start). Use `hermes -p <name>` throughout
for a named profile. Do not confirm exclusive access while the setup agent is
still using that profile. Deferral leaves the live Hermes connection unchanged;
chosen account creation, service installation or space joins may already have run.

## After installation

Anytype CLI and the Hermes gateway run as separate services. Stopping Hermes
leaves Anytype running. Keep the Anytype service or desktop app available while
Hermes uses chat or MCP.

See [operations](OPERATIONS.md) for updating, disabling or removing the plugin,
scheduled output and recovery behavior. [Validation](VALIDATION.md) records tested
versions and remaining live checks. The complete Windows setup/service path has
not been validated.
