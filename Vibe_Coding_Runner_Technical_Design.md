
# Vibe Coding Runner — Technical Design Document

## 1. Overview

Vibe Coding Runner is a minimal agent runner implemented in ~100 lines of Python.
The agent **only has one execution tool: bash(cmd)**. All file discovery, reading,
creation, and modification are performed via shell commands (including here-docs).

The runner enforces **strong behavioral constraints** so that the AI behaves like
a disciplined engineer rather than a free-form code generator.

Key characteristics:
- Single tool: bash
- Deterministic workflow: plan → act → verify → done
- Persistent task tracking via TASK.md
- Git-based convergence using git status / git diff
- Long-term project memory via mem0 (facts & decisions only)

---

## 2. Goals

- Enable “vibe coding”: iterative, exploratory, command-driven coding
- Keep implementation extremely small and understandable
- Make agent behavior observable and auditable
- Prevent uncontrolled edits and context drift
- Accumulate stable project knowledge across tasks

## Non-goals

- IDE integration
- Multiple tools beyond bash
- Automatic patch application logic
- Network access
- Fine-grained permission systems

---
## 3. Core Design Principles

### 3.1 Plan Before Act (Hard Rule)

- Step 1 **must** be `phase=plan`
- Any attempt to execute commands before planning is rejected
- Planning requires reading Agent.md and producing a checklist

### 3.2 Mandatory Task Logging

- Every step must provide `task_md_patch`
- Runner overwrites TASK.md on every step
- TASK.md acts as:
  - Agent’s working memory
  - Human-readable audit log
  - Progress tracker

### 3.3 Git as the Convergence Mechanism

- After every command, runner injects:
  - `git status --porcelain`
  - `git diff`
- The agent always sees the real effects of its actions
- Encourages minimal, deliberate changes

### 3.4 Memory Discipline (mem0)

- mem0 stores only:
  - Stable project facts
  - Architectural decisions and rationale
- Explicitly forbidden:
  - Logs
  - Stack traces
  - Command outputs
- Runner filters memory writes to enforce this

---

## 4. File Conventions

### 4.1 Agent.md (Required)

Project “source of truth”. Written and maintained by humans.

Typical contents:
- Project purpose
- Repo structure
- Entry points
- Build & test commands
- Constraints (no network, env vars, etc.)

### 4.2 TASK.md (Managed by Agent)

Overwritten on every step.

Example structure:

```md
# Task Log

## Goal
- Add /health endpoint

## Checklist
- [x] Read Agent.md
- [x] Locate FastAPI app
- [ ] Add endpoint
- [ ] Verify with pytest

## Progress
- Step 1: Planned changes
- Step 2: Located app entrypoint
```

---

## 5. Agent Output Protocol

The agent must output **strict JSON** on every step.

```json
{
  "phase": "plan | act | verify | done",
  "goal": "One-sentence task goal",
  "checklist": ["[ ] item 1", "[ ] item 2"],
  "cmd": "single bash command",
  "task_md_patch": "FULL TASK.md content",
  "memory_add": ["optional stable fact or decision"],
  "notes": "optional"
}
```

### Mandatory Rules

- One command per step
- `task_md_patch` is always required
- No markdown, no commentary outside JSON

---

## 6. Bash Execution Model

### 6.1 Allowed Usage

- File inspection: ls, cat, rg, grep, find
- File modification:
  - here-doc overwrite
  - sed / perl for small edits
- Repo inspection:
  - git status
  - git diff
- Verification:
  - pytest, npm test, etc.

### 6.2 Safety Controls

- Denylist patterns:
  - rm -rf
  - mkfs, dd
  - shutdown, reboot
  - curl, wget, nc, ssh
- Allowlist by command prefix
- Execution timeout
- Output truncation

---

## 7. mem0 Integration

### 7.1 What Goes Into Memory

Examples:
- “This project uses FastAPI, app defined in src/main.py”
- “Tests must be run using pytest -q”
- “Network access is not allowed during tests”

### 7.2 What Never Goes Into Memory

- Command output
- Error logs
- Stack traces
- Temporary investigation results

### 7.3 Metadata Stored

- repo path
- task_id
- step number
- type = fact_or_decision

---

## 8. Execution Lifecycle

1. Runner loads Agent.md
2. Runner initializes TASK.md
3. Agent performs planning step
4. Agent iterates:
   - propose command
   - update TASK.md
   - execute bash
   - inspect git state
5. Agent runs verification command
6. Runner exits only on successful verification

---

## 9. Failure Modes

- Invalid JSON → agent must retry
- Missing plan at step 1 → rejected
- Missing TASK.md update → rejected
- Verification failure → agent must debug
- MAX_STEPS exceeded → runner exits with failure

---

## 10. Why This Works

This design:
- Forces explicit reasoning without chain-of-thought leakage
- Makes agent behavior legible and reviewable
- Prevents silent over-editing
- Scales from tiny scripts to large repos
- Matches real developer workflows (shell + git)

In practice, this runner produces behavior comparable to Claude Code or Gemini CLI,
while remaining fully transparent and hackable.

---

## 11. Future Extensions (Optional)

- Temporary sandbox repo with patch export
- Auto-commit after successful verification
- Multi-task memory namespaces
- Read-only mode for investigation

---

**End of Document**
