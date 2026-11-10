# Install the current Anytype gateway on another computer

Updated 2026-11-10. This guide covers a fresh macOS/Linux installation using Anytype CLI
and the default Hermes profile. For a running desktop app or external service,
install Hermes and the plugin (steps 1 and 3), then follow the
[connection setup guide](SETUP.md). For another Hermes profile, use the
[named-profile example](#use-a-named-hermes-profile).

Gateway 0.6.0 is available from
[anyproto/anytype-hermes](https://github.com/anyproto/anytype-hermes) on `main`.
Its runtime was tested at commit `fdada2be551e3dad394cf59804ade775915d4d6e`;
later documentation updates do not change that runtime. It requires no Hermes
core patches. All 325 tests passed on the stock host below.

## 1. Install the tested stock Hermes revision

The tested revision is `ec3249919accf7bc51f780dcff663e8f19a0e176`
(main, fetched 2026-11-10). For a fresh installation:

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

## 2. Install released Anytype CLI

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

Build the plugin from `main`. These commands require Git, Python 3 and `unzip`:

```sh
git clone --branch main https://github.com/anyproto/anytype-hermes.git
cd anytype-hermes
python3 scripts/package.py
mkdir -p "$HOME/.hermes/plugins"
unzip dist/anytype-platform.zip -d "$HOME/.hermes/plugins"
hermes plugins enable anytype-platform
hermes anytype setup --help
```

No activity, delivery or recovery patch is needed.

The help output must contain setup options such as `--mode`, `--resume` and `--input`. Seeing only `status` and `reconcile` means an older plugin copy is being loaded.

## 4. Run the onboarding wizard

For a separate test computer, create a new bot account and invite it to your space. This lets the original gateway continue running under its original identity.

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

| Prompt | Fresh test-machine choice |
| --- | --- |
| Native service | `install` (installs and starts Anytype's service) |
| Native account | `create` |
| Agent display name | Your chosen bot name, e.g. `Hermes Test` |
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

The wizard obtains and stores the API key and configures the HTTP MCP connection. You do not need to copy a token into shell commands. For reuse of an existing bot instead, select `login` and provide its account recovery key privately; stop the original gateway before running another gateway against that same bot account and chats.

## 5. Check and start

```sh
hermes anytype doctor --json
```

Resolve any reported `next_action`. When `doctor` reports the connection ready, start it:

```sh
hermes gateway run
```

Keep the foreground gateway running and mention the bot using Anytype's actual account-mention picker from your allowed human account. The mention enrolls that chat for watching; typing a display name as ordinary text is insufficient.

Once the foreground test works, stop it with Ctrl-C and use Hermes's service lifecycle for background operation:

```sh
hermes gateway install
hermes gateway start
hermes gateway status
```

Anytype CLI's local API defaults to `http://127.0.0.1:31012`; the wizard discovers the effective API/MCP endpoints. Adding another space later requires a gateway restart because space enumeration currently happens once at startup.

## Use a named Hermes profile

Use this example instead of the default-profile installation/setup commands above.
It assumes the standard Hermes root at `$HOME/.hermes` and a new profile named
`anytype`. For a custom root, use the directory printed by profile creation.
For an existing profile, skip creation and substitute its name and path throughout.

From the gateway repository, after installing Hermes and Anytype CLI:

```sh
hermes profile create anytype --no-alias
hermes -p anytype setup
python3 scripts/package.py
mkdir -p "$HOME/.hermes/profiles/anytype/plugins"
unzip dist/anytype-platform.zip -d "$HOME/.hermes/profiles/anytype/plugins"
hermes -p anytype plugins enable anytype-platform
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

## Validation limits

- [Validation evidence](VALIDATION.md) records the 325 passing tests and exact stock Hermes revision. Rebuilt ZIPs can differ because documentation and ZIP timestamps change.
- Input receipts track handoff, not successful execution. Hermes owns final-reply recovery; a recovered reply can carry a duplicate warning and omit its original reply anchor or explicit attachment metadata.
- Tests use synthetic API/MCP/native-CLI fixtures; the second computer's account creation, invitation approval, sync and actual conversation are the live acceptance check.
- This guide covers macOS/Linux native service setup. Windows foreground CLI automation exists, but this complete gateway onboarding/service installation path has not been validated on Windows.
