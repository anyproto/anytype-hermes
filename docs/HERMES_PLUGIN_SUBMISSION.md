# Publishing the Anytype Hermes plugin

Version 0.6 runs as an external platform plugin on stock Hermes. The previous
0.5.1 implementation and its recovery patches are preserved at commit `02555cd`.
See [compatibility and installation](../README.md#hermes-compatibility) and
[validation](VALIDATION.md) for the exact tested host revision and limitations.

## Integration

`register(ctx)` registers the platform, operator CLI commands, workflow skill and
native `pre_tool_call`, `post_tool_call`, `post_llm_call` observers. The adapter
handles Anytype subscriptions, history catch-up, identity filtering, chat/discussion
routing and HTTP sends. MCP remains a separately configured connection for object
operations. No local UI bridge or activity-callback patch is needed.

Processing hooks bind tool activity to its originating space/chat and close it
when processing ends. They do not claim to track the completion of every original
input after Hermes merges, steers or handles it inline. The plugin records
`handing_off`/`handed_off`; interruption is `uncertain`, never independently replayed.

Hermes's stock ledger owns final-reply recovery. A recovered reply may carry a
visible duplicate warning and omit its exact reply anchor or explicit attachment
metadata. The plugin retains per-chunk receipts and prevents immediate fallback
from silently duplicating an ambiguous POST. Full guarantees are in
[the recovery guide](OPERATIONS.md#recovery-and-operation).

## Steps to publish

1. Commit the 0.6 implementation, then push it to the external repository. Build
   with `python3 scripts/package.py`; verify the archive includes the skill and
   excludes patches, credentials, runtime files and local deployment records.
2. Test on an unmodified, pinned Hermes checkout. Run the plugin suite through
   `scripts/run-tests.sh`, and verify new-profile setup, discovery, messages,
   discussions, native activity, MCP object links, scheduled delivery and restart
   behavior live. Do not treat mocked HTTP tests as live acceptance.
3. Run `hermes plugins validate <plugin-directory>` under an isolated profile.
   Resolve errors and review private-import warnings against
   `hermes_cli/data/plugin_api.json`. Passing imports today is not a stability
   guarantee for internal APIs. This plugin still uses internal helpers for
   status redaction/presentation, pending controls and setup lifecycle inspection;
   removing patches does not finish that public-API cleanup.
4. Declare the tested Hermes support range and installation/setup instructions.
   Test stock releases individually before widening it; a passing test on main
   does not certify every earlier release. Keep recovery limitations visible.
5. Submit a catalog entry in `NousResearch/hermes-agent/plugin-catalog/` pointing
   to the external repository at a full 40-character commit SHA. Follow that
   checkout's `plugin-catalog/README.md` and submission guide. Catalog validation
   runs at the pin; update it only after the matching release is verified.
6. Exercise installation through the catalog into a fresh profile before
   announcing availability. Confirm platform/CLI/skill discovery and setup with
   no manual changes to Hermes source.

Upstream policy favors external third-party integrations. A missing generic
capability should be proposed as an upstream extension with a concrete consumer,
not implemented as a runtime monkey patch or bundled core-source modification.

## Upgrade from the patched version

Use a clean upstream Hermes checkout/environment, retaining the profile's config,
credentials and Anytype state. Do not reapply archived patches, blindly reverse
patches over changed source, or delete receipts to get a clean startup. Old
in-flight receipts become uncertain; historical terminal rows remain terminal.
Review old pending/uncertain deliveries before a restart because stock recovery
can post a marked duplicate without preserving old transport metadata.

The migration itself does not modify or restart an installed Hermes instance.
