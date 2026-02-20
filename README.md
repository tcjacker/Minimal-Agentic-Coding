# Minimal Agentic Coding Runner

A minimal coding runner that uses a single tool: `bash`.

It runs an LLM-driven loop with strict phases and can be interrupted by humans at any time.

## Features

- Single execution tool: `bash(cmd)`
- Strict agent protocol: `plan -> act -> verify -> chat -> done`
- Mandatory `TASK.md` updates per step
- Step logs in `logs/run_YYYYMMDD_HHMMSS.log`
- Works with OpenAI-compatible APIs (OpenAI / Qwen DashScope)
- Human-in-the-loop controls:
  - `Ctrl+C` to pause and enter control mode
  - Post-verify gate: only finishes after user confirms `done`
  - Bare text in interactive prompts = `feedback + resume`

## Requirements

- Python 3.10+
- `OPENAI_API_KEY` or `API_KEY`
- Optional: `rg` (ripgrep), used for faster file inventory

## Quick Start

```bash
python3 vibe_runner.py "创建一个 hello.py 并写入打印语句"
```

### OpenAI config

```bash
export PROVIDER=openai
export BASE_URL="https://api.openai.com/v1"
export API_KEY="<your_api_key>"
export MODEL="gpt-4.1-mini"
python3 vibe_runner.py "your goal"
```

### Qwen (DashScope) config

```bash
export PROVIDER=qwen
export BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
export API_KEY="<your_dashscope_key>"
export MODEL="qwen3-max"
python3 vibe_runner.py "your goal"
```

## Interactive Commands

### `control>` (after `Ctrl+C`)

- `resume` / `r`: continue agent loop
- `feedback <text>`: add feedback to context
- `run <bash>`: run one manual command and inject output back to context
- `quit`: exit runner (with confirmation)
- bare text: treated as feedback and auto-resume

### `post-verify>`

- `done`: finish task (with confirmation)
- `resume`: continue improving
- `feedback <text>` / `run <bash>` / `quit`
- bare text: treated as feedback and auto-resume

### `chat>`

- used when agent chooses `phase=chat` for consulting/explaining tasks
- supports `resume`, `feedback <text>`, `quit`
- bare text: treated as feedback and auto-resume

## Safety / Limits

- Denylist blocks risky commands (`rm -rf`, `mkfs`, `dd`, `shutdown`, `reboot`, `curl`, `wget`, `nc`, `ssh`)
- One command per agent step
- Command timeout controlled by `CMD_TIMEOUT` (default `20s`)
- Max steps controlled by `MAX_STEPS` (default `30`)

## Files

- `vibe_runner.py`: runner implementation
- `TASK.md`: per-task working memory (overwritten each step)
- `logs/run_*.log`: execution audit logs

