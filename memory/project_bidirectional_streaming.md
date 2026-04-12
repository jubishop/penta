---
name: Plan review and question handling via HTTP hooks
description: Branch worktree-usingPlans implements ExitPlanMode and AskUserQuestion via PreToolUse HTTP hooks with updatedInput — live tested and working with Claude CLI v2.1.87
type: project
---

## Branch: `worktree-usingPlans`

Implements plan review and structured question answering for Claude via PreToolUse HTTP hooks. **Both features live tested and working** with Claude CLI v2.1.87.

## How it works

A localhost HTTP server handles Claude CLI's `PreToolUse` hook requests via `--settings`. Three hook matchers:

1. **`AskUserQuestion` matcher**: Pauses hook response → TUI shows QuestionPickerScreen → user picks answers → answers injected via `updatedInput` → Claude receives them as the AskUserQuestion tool result
2. **`ExitPlanMode` matcher**: Pauses hook response → TUI shows plan inline → user approves/rejects → allow/deny
3. **Wildcard `""` matcher**: Auto-approves all other tools instantly

**Critical discovery**: `updatedInput` for AskUserQuestion requires a **specific matcher** (`"AskUserQuestion"`), not a wildcard (`""`). A wildcard matcher causes the answers to be silently dropped (GitHub issue #29530). This was added in CLI v2.1.85.

## Key protocol details

- `updatedInput` REPLACES the entire tool input — must include original `questions` + added `answers`
- `answers` is `Record<string, string>` — keys are question text, values are selected option labels
- `hookEventName: "PreToolUse"` must be in the response for updatedInput to work
- No extra keys allowed in updatedInput — they corrupt the AskUserQuestion schema
- The HTTP response can block for up to 600s (user review time) — Claude CLI waits

## What the branch implements

- `PermissionServer` with separate matchers for AskUserQuestion/ExitPlanMode/wildcard
- `ClaudeService` accepts `hook_settings` → uses `--settings <json>` instead of `--dangerously-skip-permissions`
- `QuestionPickerScreen` (ModalScreen) — RadioSet/SelectionList, "Other" with Input, validation
- `PlanPickerScreen` (ModalScreen) — disambiguates multiple pending plans
- Non-blocking inline plan review: `/approve`, `/revise <feedback>`, `/plan` interpolation
- `PendingPlan` model, `WAITING_FOR_USER` status, `cancel_all_busy()`
- 232 tests passing (including 10 permission server integration tests)
