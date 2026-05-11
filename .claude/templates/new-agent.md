<!--
Sprint 13 Bit 13.2-rest (2026-05-11) — template for adding a new
sub-agent definition under .claude/agents/.

Usage:
  1. Create .claude/agents/<agent-name>.md (this file's destination).
  2. Replace placeholders.
  3. Delete this HTML comment block before committing.

Sub-agents are invoked via the Agent tool with subagent_type=<agent-name>.
They run in their own context (no access to parent's tools by default;
each agent definition declares its tool set). See Anthropic Claude Code
docs for the agent-definition schema.

Cross-refs:
- Existing built-in agents: Explore (read-only search), Plan
  (architecture), general-purpose (catch-all).
- This Bit 13.2-rest template ships the scaffold for project-specific
  sub-agents (e.g., adv-reviewer, test-writer, doc-drift-fixer).
-->
---
name: <agent-name>
description: <ONE_SENTENCE_DESCRIPTION>. Use when: <TRIGGER_PATTERNS>.
tools: [<TOOL_NAME_1>, <TOOL_NAME_2>, ...]
---

# <Agent Title>

## Purpose

<2-3 sentences: what this agent does, why it exists, when to dispatch it.>

## Invocation

```python
Agent(
    description="<short task description>",
    subagent_type="<agent-name>",
    prompt="<self-contained task brief — agent has NO context from this conversation>",
)
```

## Capabilities

- <CAPABILITY_1>
- <CAPABILITY_2>
- <CAPABILITY_3>

## Tools (declared)

- `<TOOL_NAME_1>` — <why this tool>
- `<TOOL_NAME_2>` — <why this tool>

Tools NOT in the list above are inaccessible — design the agent's
brief to fit the declared tool set.

## Prompt template

```
You are doing <TASK_TYPE> for <CONTEXT>. cwd: /Users/gabrielkagan/Documents/kalshi-bot.

**Context:** <relevant background — git state, prior Bits, KB pointers>

**Your mandate:**
1. <STEP_1>
2. <STEP_2>
3. <STEP_3>

**Classify findings:**
- CRITICAL = ships breakage
- MAJOR = ships drift
- MINOR = cosmetic narrative

**Report format:** <output spec — sections, length cap, classification>
```

## When NOT to dispatch

- If the task is small enough to do inline (saves agent overhead).
- If the task needs tools the agent doesn't have (check `tools:` list).
- If the parent context already has the relevant information.

## Cross-refs
- `.claude/onboarding.md` § "Skill routing" — when to use skills vs agents.
- `CONTRIBUTING.md` § "Discipline" — adversarial review uses Agent dispatches.
