---
name: anytype
description: Work with objects, research and tasks in Anytype.
---

# Anytype

Use for searches, research, tasks and object edits in Anytype. Keep brief answers
in chat. Create objects when requested or when substantial research benefits from
a lasting result; honor requests to keep the answer in chat.

## Prerequisites

Use the `anytype` MCP server configured in the current Hermes profile. Follow the
available tool schemas. If the required tools are unavailable, explain what could
not be completed.

## Workflow

1. Use the current conversation's `space_id` unless the user requests another
   space. In a discussion, use the supplied parent-object context and retrieve
   more of that object only when needed. Read referenced attachments through MCP.
2. Search for relevant existing objects before creating replacements. Before
   creating or editing structured data, inspect the space's types and properties;
   reuse its task statuses and other conventions rather than inventing fields.
3. Prefer one readable object for a report. Use multiple objects or a collection
   when records are independently useful, such as sources or comparison entries.
   For tasks, set requested details using the available properties. Preserve
   unrelated fields when editing.
4. Check tool results before claiming success. After partial failure, reuse
   successfully created objects; a failed chat reply is not a reason to recreate
   them. Report what remains incomplete.

## Reply and scheduling

Summarize the outcome and link confirmed objects:

`[Title](anytype://object?objectId=OBJECT_ID&spaceId=FULL_SPACE_ID)`

Same-space links become native attachments when the gateway's
`attach_object_links` setting is enabled. Let the gateway post the normal final
reply and tool activity; do not duplicate them with MCP chat sends.

For scheduled jobs, follow the gateway's supplied destination guidance. Use
`deliver="origin"` when the configured Anytype home belongs to another space,
unless the user explicitly requests that destination.
