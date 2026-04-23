# Coding Agent Workflow

All coding work in this project must be broken up across three agent roles. The lead agent never writes code directly — it plans, delegates, and integrates.

## Lead agent
- **Model:** Opus 4.7
- **Effort:** Extra high
- **Role:** Orchestrator only. No direct coding.
- **Responsibilities:**
  - Break the task into achievable, independently-verifiable bites.
  - Spawn one sub agent per bite and coordinate their work.
  - Receive verification reports and decide on corrections or follow-ups.
  - Own integration, final summary, and user communication.

## Sub agent
- **Model:** Opus 4.6
- **Effort:** High
- **Scope:** Exactly one task per sub agent. Do not bundle tasks.
- **Responsibilities:**
  - Implement the assigned bite end-to-end.
  - Return a concise report of what changed and any assumptions made.

## Verification agent
- **Model:** Opus 4.6
- **Effort:** Max
- **Independence:** Reviews code without access to the sub agent's reasoning — only the diff and the task spec.
- **Responsibilities:**
  - Check correctness, edge cases, regressions, and adherence to the task spec.
  - Report findings back to the lead agent (not the sub agent).
  - Lead agent decides whether to dispatch fixes to a new sub agent.

## Flow
1. Lead plans → splits task into bites.
2. For each bite: Lead → Sub (implement) → Verification (review) → Lead.
3. Lead integrates results, dispatches follow-up bites as needed, and reports to the user.
