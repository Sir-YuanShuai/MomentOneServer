---
name: record-with-moment-one
description: Use Moment One to record, search, review, or summarize personal moments, bookkeeping, habits, reminders, account allowances, and explicit product feedback. Trigger when the user asks to remember or log something, record income or spending, manage or check a habit, inspect a timeline or daily review, create a reminder, or send feedback to Moment One.
---

# Use Moment One

Choose the narrowest real MCP tool that completes the request. Never call `agent_plan`,
`bookkeeping_plan`, or `a2ui_action`; those are reserved for authenticated Moment One glasses.

## Record safely

- Ask only for information required to avoid a materially wrong record.
- Generate a fresh `idempotencyKey` for every new write and reuse it only when retrying the same write.
- Supply `expectedRevision` when updating an existing object.
- Treat `createdAt` as server-owned. Use `occurredAt` only for when the event happened.
- For reminders, prefer `localDateTime` plus the user's IANA timezone or `afterMinutes`. Do not guess an ambiguous date or timezone.
- Do not claim success until the tool returns success.

## Select tools

- Use `bookkeeping_create`, `bookkeeping_list`, and `bookkeeping_summary` for financial records.
- Use `moments_create`, `moments_list`, `moments_search`, `moments_get`, `moments_count`, and `reviews_daily` for the timeline.
- Use `habit_goals_list`, `habit_goal_create`, `habit_goal_update`, `habit_checkin_create`, and `habit_progress` for habits.
- Use `reminder_create` for scheduled reminders; never write notifications directly.
- Use `feedback_submit` only when the user explicitly wants to send feedback or request a feature.
- Use `account_entitlements` only when allowances or subscription capabilities are relevant.

## Handle attachments without user-visible upload steps

Use an attachment only when it materially belongs to the record. If the host supplies a usable
attachment reference or the client has already produced a ready `assetId`, carry it into the
record. Otherwise create the text record without an attachment. Never invent a URL, request a
Base64 copy, or tell the user that an attachment was uploaded when the host did not expose one.

When the host supplies a short-lived HTTPS URL, call `asset_import_from_url` and pass its returned
ready `assetId` to the record tool. Do not expose this intermediate transport step to the user.

When a client provides raw file bytes, use `asset_upload_intent_create`, upload to the returned
short-lived URL, call `asset_upload_complete`, and pass the resulting `assetId` to the record tool.
Keep these transport details out of the user-facing response unless an upload fails.

## Present results

Rely on the tool's MCP Apps result card when available. In text fallback, report the actual count,
date range, totals, and pagination state returned by the tool; never silently reduce a requested
list or date range.
