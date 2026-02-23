#!/usr/bin/env python3
import json, os, re, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime

PROVIDER = os.getenv("PROVIDER", "openai")
MODEL = os.getenv("MODEL", "qwen3-max" if PROVIDER == "qwen" else "gpt-4.1-mini")
BASE_URL = os.getenv("BASE_URL", "https://api.openai.com/v1")
API_KEY = os.getenv("API_KEY") or os.getenv("OPENAI_API_KEY")
MAX_STEPS = int(os.getenv("MAX_STEPS", "30"))
TIMEOUT = int(os.getenv("CMD_TIMEOUT", "20"))
OUTPUT_LIMIT = int(os.getenv("OUTPUT_LIMIT", "12000"))
PAGE_LINES = int(os.getenv("PAGE_LINES", "120"))
SCRATCH_FILE = os.getenv("SCRATCH_FILE", ".agent_scratch.md")
SESSION_SUMMARY_FILE = os.getenv("SESSION_SUMMARY_FILE", ".session_summary.md")
HISTORY_TURNS = int(os.getenv("HISTORY_TURNS", "12"))
MAX_AGENT_INJECT = int(os.getenv("MAX_AGENT_INJECT", "8000"))
MAX_SCRATCH_INJECT = int(os.getenv("MAX_SCRATCH_INJECT", "4000"))
SUMMARY_EVERY = int(os.getenv("SUMMARY_EVERY", "3"))
DENY = r"\b(rm\s+-rf|mkfs|dd\s+if=|shutdown|reboot|curl|wget|nc\b|ssh\b)"
PROMPT = """You are Vibe Coding Runner agent.
Output STRICT JSON only. No markdown, no extra text.

Schema:
{"phase":"init|plan|act|verify|chat|done","decision":"direct_execute|ask_user","questions":["optional"],"say":"user-facing reply when phase=chat","goal":"...","checklist":["[ ] ..."],"cmd":"single bash command","task_md_patch":"FULL TASK.md","memory_add":["optional stable fact/decision"],"notes":"optional"}

Execution rules:
1) Prefer phase=init when project context is missing/stale; do not force it every run.
2) Every step MUST include task_md_patch (full TASK.md content).
3) Use exactly ONE bash command in cmd when phase is init/act/verify.
4) For file edits, use bash methods only (cat <<'EOF', sed, perl, etc.).
5) For long files, read by pagination across multiple steps. Recommended:
   nl -ba <file> | sed -n '<start>,<end>p'
   where window size defaults to PAGE_LINES from context.
6) Use SCRATCH_FILE for temporary notes and intermediate findings. You may append/update it using bash.

Tool/runtime rules:
1) Do not assume a specific programming language or runtime.
2) Prefer commands documented in Agent.md or project files.
3) Before using a tool/runtime, ensure it exists in this environment.
4) If runtime/tool choice is ambiguous or unavailable, use decision=ask_user.

Decision rules:
1) If context is sufficient, use decision=direct_execute.
2) If context is insufficient, use decision=ask_user with concise questions and do not execute.
3) For explanation/consulting tasks, prefer phase=chat and provide user-facing text in say.
4) Use phase=done only when task goal is fully satisfied.

Init rules:
1) In phase=init, build/update Agent.md from project evidence (README, directory, git state, existing Agent.md).
2) Agent.md should include: project overview, environment setup, build/test commands, coding rules, structure hints, safety.
3) After init, continue with plan/act/verify.
4) Prefer init when project context is stale, but do not block the run if init is skipped."""


def read(path, d=""):
    return open(path).read() if os.path.exists(path) else d


def write(path, s):
    open(path, "w").write(s)


def append(path, s):
    with open(path, "a") as f:
        f.write(s)


def run(cmd):
    if not cmd.strip():
        return {"code": 0, "output": "[no command]"}
    if re.search(DENY, cmd):
        return {"code": 126, "output": "[blocked by denylist]"}
    try:
        p = subprocess.run(cmd, shell=True, text=True, capture_output=True, timeout=TIMEOUT)
        out = p.stdout + p.stderr
        if len(out) <= OUTPUT_LIMIT:
            return {"code": p.returncode, "output": out}
        head = out[:OUTPUT_LIMIT // 2]
        tail = out[-OUTPUT_LIMIT // 2:]
        return {"code": p.returncode, "output": f"{head}\n...[truncated]...\n{tail}"}
    except subprocess.TimeoutExpired:
        return {"code": 124, "output": "[command timeout]"}


def git_snapshot():
    inside = run("git rev-parse --is-inside-work-tree")
    if inside["code"] != 0:
        return "\n[git]\n[disabled: not a git repository]"
    s = run("git status --porcelain")["output"]
    d = run("git diff -- . ':(exclude)TASK.md'")["output"]
    return f"\n[git status]\n{s}\n[git diff]\n{d}"


def file_inventory():
    res = run("rg --files -g '!.git' -g '!logs/*' 2>/dev/null | head -n 200")
    if res["code"] != 0 or not res["output"].strip():
        res = run("find . -type f -not -path './.git/*' -not -path './logs/*' | sed 's#^./##' | head -n 200")
    files = [x.strip() for x in res["output"].splitlines() if x.strip()]
    if not files:
        return "(no files found)", []
    return "\n".join(f"- {p}" for p in files), files


def file_previews(files):
    allow = (".md", ".txt", ".py", ".html", ".js", ".css", ".json", ".yml", ".yaml")
    picks = [p for p in files if p.endswith(allow)][:8]
    if not picks:
        return "(no previewable text files)"
    out = []
    for p in picks:
        try:
            s = read(p, "")[:800].strip()
            s = s.replace("\r", "")
            out.append(f"\n### {p}\n{s or '(empty)'}")
        except Exception:
            out.append(f"\n### {p}\n(unreadable)")
    return "\n".join(out)


def project_summary(goal):
    inside = run("git rev-parse --is-inside-work-tree")
    git_state = "yes" if inside["code"] == 0 else "no"
    branch = run("git branch --show-current")["output"].strip() if git_state == "yes" else ""
    readme = read("README.md", "")
    readme_cn = read("readme.md", "")
    agent = read("Agent.md", "")
    files_text, files = file_inventory()
    top = ", ".join(files[:12]) if files else "(none)"
    return (
        f"Project goal: {goal}\n"
        f"Git repo: {git_state}\n"
        f"Git branch: {branch or '(n/a)'}\n"
        f"Top files: {top}\n"
        f"Has Agent.md: {'yes' if agent else 'no'}\n"
        f"Has README: {'yes' if (readme or readme_cn) else 'no'}\n"
        f"Existing files list:\n{files_text}\n"
    )


def bootstrap_agent_md(goal):
    if os.path.exists("Agent.md") and read("Agent.md").strip():
        return
    write(
        "Agent.md",
        "# Project Overview\n"
        "This is a TypeScript REST API backend with user auth.\n\n"
        "## Environment Setup\n"
        "- pnpm install\n"
        "- node >= 18\n\n"
        "## Build & Dev\n"
        "- pnpm dev\n"
        "- pnpm build\n\n"
        "## Testing\n"
        "- pnpm test\n"
        "- pnpm lint\n"
        "- pnpm format\n\n"
        "## Coding Rules\n"
        "### Do\n"
        "- Strict TS\n"
        "- Write unit tests for new features\n\n"
        "### Don't\n"
        "- Hardcode credentials\n"
        "- Skip tests\n\n"
        "## Structure Hints\n"
        "- Controllers: src/controllers\n"
        "- Models: src/models\n"
        "- Tests: tests/\n\n"
        "## Safety\n"
        "Allowed:\n"
        "- search files\n"
        "- run tests\n"
        "Ask before:\n"
        "- install deps\n"
        "- modify prod configs\n",
    )


def project_context_status():
    text = read("Agent.md", "").strip()
    if not text:
        return False, "Agent.md missing/empty"
    low = text.lower()
    checks = [
        any(k in low for k in ("project overview", "overview", "项目概览")),
        any(k in low for k in ("build", "dev", "run", "pnpm", "test", "lint", "format")),
        any(k in low for k in ("coding rules", "do", "don't", "strict", "tests")),
        any(k in low for k in ("safety", "allowed", "ask before", "constraints", "约束")),
    ]
    placeholder = False
    score = sum(1 for x in checks if x)
    ok = (score >= 3) and (not placeholder)
    reason = f"score={score}/4; placeholder={'yes' if placeholder else 'no'}"
    return ok, reason


def ensure_scratch(reset=True):
    if os.path.exists(SCRATCH_FILE) and not reset:
        return
    write(
        SCRATCH_FILE,
        "# Agent Scratchpad\n\n"
        "- Temporary working notes for this runner session only.\n"
        "- Safe to overwrite/append.\n",
    )


def ensure_session_summary(reset=True):
    if os.path.exists(SESSION_SUMMARY_FILE) and not reset:
        return
    write(
        SESSION_SUMMARY_FILE,
        "# Session Summary\n\n"
        "## Goal\n"
        "- (pending)\n\n"
        "## Completed\n"
        "- (none)\n\n"
        "## Decisions\n"
        "- (none)\n\n"
        "## Open Items\n"
        "- (none)\n",
    )


def clip(text, n):
    return text if len(text) <= n else text[:n] + "\n...[truncated]..."


def build_llm_messages(msgs, summary_text):
    base = msgs[:2]
    tail = msgs[2:]
    keep = HISTORY_TURNS * 2
    recent = tail[-keep:] if keep > 0 else tail
    summary_msg = [{"role": "user", "content": f"Session summary (compressed):\n{clip(summary_text, 6000)}"}]
    return base + summary_msg + recent


def update_session_summary(step, phase, cmd, out, decision, notes):
    if step % SUMMARY_EVERY != 0 and phase not in ("done",):
        return
    line = (
        f"- Step {step}: phase={phase}, decision={decision}, "
        f"cmd={cmd[:120] or '(none)'}, notes={clip(notes or '', 160).replace(chr(10), ' ')}"
    )
    append(SESSION_SUMMARY_FILE, f"\n{line}\n")


def llm(messages):
    if not API_KEY:
        print("API_KEY/OPENAI_API_KEY not set", file=sys.stderr); sys.exit(1)
    base = BASE_URL.rstrip("/")
    if PROVIDER == "qwen" and "dashscope.aliyuncs.com" not in base:
        base = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    payload = {
        "model": MODEL,
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
    )
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                out = json.loads(r.read().decode())
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="ignore")
            retryable = e.code in (408, 409, 429) or e.code >= 500
            if i < 2 and retryable:
                time.sleep(2 ** i)
                continue
            print(f"llm http error {e.code}: {body[-800:]}", file=sys.stderr)
            sys.exit(1)
        except (urllib.error.URLError, TimeoutError) as e:
            if i < 2:
                time.sleep(2 ** i)
                continue
            print(f"llm network error: {e}", file=sys.stderr)
            sys.exit(1)
    txt = out["choices"][0]["message"]["content"]
    return json.loads(txt)


def control_console(msgs, log_path):
    print("\n[paused] commands: resume | feedback <text> | run <bash> | quit")
    print("         bare text => feedback + resume")
    while True:
        try:
            raw = input("control> ").strip()
        except KeyboardInterrupt:
            print("\nstill paused")
            continue
        if raw in ("resume", "r"):
            append(log_path, "control=resume\n")
            print("action=resume")
            return msgs, False
        if raw in ("done", "d", "quit", "q", "exit"):
            c = input("confirm quit? [y/N] ").strip().lower()
            if c in ("y", "yes"):
                append(log_path, f"control={'done' if raw in ('done','d') else 'quit'}\n")
                print(f"action={'done' if raw in ('done','d') else 'quit'}")
                return msgs, True
            print("action=cancel_quit")
            continue
        if raw.startswith("feedback "):
            fb = raw[len("feedback "):].strip()
            if fb:
                append(log_path, f"control=feedback {fb}\n")
                msgs.append({"role": "user", "content": f"Human feedback: {fb}"})
                print("action=feedback_sent")
            continue
        if raw.startswith("run "):
            mcmd = raw[len("run "):].strip()
            res = run(mcmd)
            out = f"[exit_code]\n{res['code']}\n{res['output']}" + git_snapshot()
            append(log_path, f"control=run cmd={mcmd}\noutput:\n{out}\n")
            print(out[-1200:])
            msgs.append({"role": "user", "content": f"human_manual_command: {mcmd}\noutput:\n{out}"})
            print("action=run_sent")
            continue
        if raw:
            append(log_path, f"control=feedback {raw}\n")
            msgs.append({"role": "user", "content": f"Human feedback: {raw}"})
            append(log_path, "control=resume_after_bare_text\n")
            print("action=feedback_sent")
            print("action=resume")
            return msgs, False
        print("empty input ignored; use resume/feedback/run/quit")


def post_verify_console(msgs, log_path, mode="verify"):
    prompt = "[verify passed]" if mode == "verify" else "[agent says done]"
    print(f"\n{prompt} commands: done | resume | feedback <text> | run <bash> | quit")
    print("             bare text => feedback + resume")
    while True:
        try:
            raw = input("post-verify> ").strip()
        except KeyboardInterrupt:
            print("\nstill waiting for done/resume")
            continue
        if raw in ("done", "d"):
            c = input("confirm done? [y/N] ").strip().lower()
            if c in ("y", "yes"):
                append(log_path, "status=done_by_user\n")
                print("action=done")
                return msgs, "done"
            print("action=cancel_done")
            continue
        if raw in ("resume", "r"):
            append(log_path, f"status=resume_after_{mode}\n")
            msgs.append({"role": "user", "content": "Human chose resume. Continue improving; do not finalize yet."})
            print("action=resume")
            return msgs, "resume"
        if raw in ("quit", "q", "exit"):
            c = input("confirm quit? [y/N] ").strip().lower()
            if c in ("y", "yes"):
                append(log_path, f"status=quit_after_{mode}\n")
                print("action=quit")
                return msgs, "quit"
            print("action=cancel_quit")
            continue
        if raw.startswith("feedback "):
            fb = raw[len("feedback "):].strip()
            if fb:
                append(log_path, f"post_{mode}_feedback={fb}\n")
                msgs.append({"role": "user", "content": f"Human feedback: {fb}"})
                print("action=feedback_sent")
            continue
        if raw.startswith("run "):
            mcmd = raw[len("run "):].strip()
            res = run(mcmd)
            out = f"[exit_code]\n{res['code']}\n{res['output']}" + git_snapshot()
            append(log_path, f"post_{mode}_run cmd={mcmd}\noutput:\n{out}\n")
            print(out[-1200:])
            msgs.append({"role": "user", "content": f"human_manual_command: {mcmd}\noutput:\n{out}"})
            print("action=run_sent")
            continue
        if raw:
            append(log_path, f"post_{mode}_feedback={raw}\n")
            msgs.append({"role": "user", "content": f"Human feedback: {raw}"})
            append(log_path, f"status=resume_after_{mode}_bare_text\n")
            print("action=feedback_sent")
            print("action=resume")
            return msgs, "resume"
        print("empty input ignored; use done/resume/feedback/run/quit")


def ask_human(questions, log_path):
    print("\nAgent asks for clarification:")
    for q in questions[:3]:
        print(f"- {q}")
    try:
        ans = input("you> ").strip()
    except KeyboardInterrupt:
        print("")
        return None
    append(log_path, f"human_answer={ans}\n")
    return ans


def chat_console(say, msgs, log_path):
    if say:
        print(f"\nAgent: {say}")
        append(log_path, f"chat_say={say}\n")
    print("[chat] commands: resume | feedback <text> | quit")
    print("       bare text => feedback + resume")
    while True:
        raw = input("chat> ").strip()
        if raw in ("resume", "r"):
            append(log_path, "chat=resume\n")
            return msgs, "resume"
        if raw in ("done", "d"):
            append(log_path, "chat=done\n")
            return msgs, "done"
        if raw in ("quit", "q", "exit"):
            append(log_path, "chat=quit\n")
            return msgs, "quit"
        if raw.startswith("feedback "):
            fb = raw[len("feedback "):].strip()
            if fb:
                append(log_path, f"chat_feedback={fb}\n")
                msgs.append({"role": "user", "content": f"Human feedback: {fb}"})
                print("action=feedback_sent")
            continue
        if raw:
            append(log_path, f"chat_feedback={raw}\n")
            msgs.append({"role": "user", "content": f"Human feedback: {raw}"})
            append(log_path, "chat=resume_after_bare_text\n")
            print("action=feedback_sent")
            print("action=resume")
            return msgs, "resume"
        print("empty input ignored; use resume/feedback/quit")


def main():
    goal = " ".join(sys.argv[1:]).strip() or input("Goal> ").strip()
    os.makedirs("logs", exist_ok=True)
    log_path = f"logs/run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    bootstrap_agent_md(goal)
    ensure_scratch(reset=True)
    ensure_session_summary(reset=True)
    agent = read("Agent.md", "# Agent\n- Keep changes minimal\n")
    write("TASK.md", f"# Task Log\n\n## Goal\n- {goal}\n\n## Checklist\n- [ ] Init\n- [ ] Plan\n")
    write(
        log_path,
        f"# Vibe Runner Log\nstarted_at={datetime.now().isoformat()}\n"
        f"provider={PROVIDER}\nmodel={MODEL}\ngoal={goal}\n\n",
    )
    print(f"log file: {log_path}")
    file_list, files = file_inventory()
    previews = file_previews(files)
    summary = project_summary(goal)
    scratch = read(SCRATCH_FILE, "")[:8000]
    context_ok, context_reason = project_context_status()
    append(log_path, f"workspace_files:\n{file_list}\n")
    append(log_path, f"project_context_ok={context_ok}; reason={context_reason}\n")
    msgs = [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content":
            f"Goal: {goal}\n\nWorking directory: {os.getcwd()}\n\n"
            f"Context config:\n- PAGE_LINES={PAGE_LINES}\n- SCRATCH_FILE={SCRATCH_FILE}\n\n"
            f"Project context:\n- project_context_ok={context_ok}\n- reason={context_reason}\n\n"
            f"Project summary:\n{summary}\n\n"
            f"Existing files:\n{file_list}\n\n"
            f"Text previews:\n{previews}\n\n"
            f"Agent.md:\n{agent}\n\n"
            f"Scratch ({SCRATCH_FILE}):\n{scratch}\n\n"
            f"Current TASK.md:\n{read('TASK.md')}"}
    ]
    append(SESSION_SUMMARY_FILE, f"\n## Goal\n- {goal}\n")
    last_agent_seen = None
    last_scratch_seen = None
    for step in range(1, MAX_STEPS + 1):
        current_agent = read("Agent.md", "(missing)")
        current_scratch = read(SCRATCH_FILE, "(missing)")
        inject_parts = []
        if current_agent != last_agent_seen:
            inject_parts.append(
                f"Latest Agent.md (always authoritative):\n{clip(current_agent, MAX_AGENT_INJECT)}"
            )
            last_agent_seen = current_agent
        if current_scratch != last_scratch_seen:
            inject_parts.append(
                f"Latest Scratch ({SCRATCH_FILE}):\n{clip(current_scratch, MAX_SCRATCH_INJECT)}"
            )
            last_scratch_seen = current_scratch
        if inject_parts:
            msgs.append({"role": "user", "content": "\n\n".join(inject_parts)})
        llm_msgs = build_llm_messages(msgs, read(SESSION_SUMMARY_FILE, ""))
        try:
            r = llm(llm_msgs)
        except KeyboardInterrupt:
            msgs, should_quit = control_console(msgs, log_path)
            if should_quit:
                print("QUIT")
                return
            continue
        phase, cmd, patch = r.get("phase", ""), (r.get("cmd") or ""), r.get("task_md_patch")
        decision = (r.get("decision") or "direct_execute").strip()
        questions = r.get("questions") or []
        say = (r.get("say") or "").strip()
        notes = (r.get("notes") or "").strip()
        append(log_path, f"\n## step {step}\nresponse={json.dumps(r, ensure_ascii=False)}\n")
        if phase not in ("init", "plan", "act", "verify", "chat", "done"):
            append(log_path, f"error=invalid phase: {phase}\n")
            print(f"invalid phase: {phase}"); return
        if decision not in ("direct_execute", "ask_user"):
            append(log_path, f"error=invalid decision: {decision}\n")
            print(f"invalid decision: {decision}"); return
        if patch is None:
            print("task_md_patch required"); return
        if phase == "done":
            write("TASK.md", patch)
            append(log_path, "phase=done\n")
            msgs += [{"role": "assistant", "content": json.dumps(r, ensure_ascii=False)}]
            msgs, action = post_verify_console(msgs, log_path, mode="done")
            if action == "done":
                update_session_summary(step, phase, cmd, "", decision, notes)
                print("DONE (user confirmed)")
                return
            if action == "quit":
                print("QUIT")
                return
            continue
        if phase == "chat":
            write("TASK.md", patch)
            msgs += [{"role": "assistant", "content": json.dumps(r, ensure_ascii=False)}]
            msgs, action = chat_console(say or r.get("notes", ""), msgs, log_path)
            if action in ("done", "quit"):
                print("DONE (user confirmed)" if action == "done" else "QUIT")
                return
            continue
        if decision == "ask_user":
            if not questions:
                questions = [r.get("notes") or "Need more information. Please clarify."]
            ans = ask_human(questions, log_path)
            if ans is None:
                msgs, should_quit = control_console(msgs, log_path)
                if should_quit:
                    print("QUIT")
                    return
                continue
            if ans.lower() in ("done", "d", "quit", "q", "exit"):
                print("DONE (user confirmed)" if ans.lower() in ("done", "d") else "QUIT")
                return
            msgs += [
                {"role": "assistant", "content": json.dumps(r, ensure_ascii=False)},
                {"role": "user", "content":
                    f"Human answer: {ans}\n\nLatest Agent.md:\n{read('Agent.md', '(missing)')[:12000]}"
                    f"\n\nLatest Scratch ({SCRATCH_FILE}):\n{read(SCRATCH_FILE, '(missing)')[:8000]}"
                    f"\n\nCurrent TASK.md:\n{read('TASK.md')}"}
            ]
            continue
        if phase == "plan" and cmd.strip():
            append(log_path, "warning=invalid plan cmd; requested retry\n")
            print("warning: plan step must not execute command, retrying...")
            msgs += [
                {"role": "assistant", "content": json.dumps(r, ensure_ascii=False)},
                {"role": "user", "content": "Invalid output: phase=plan must have empty cmd. Please emit a valid next JSON step."}
            ]
            continue
        if phase in ("init", "act", "verify") and not cmd.strip():
            append(log_path, f"warning=missing cmd in {phase}; requested retry\n")
            print(f"warning: {phase} step missing command, retrying...")
            msgs += [
                {"role": "assistant", "content": json.dumps(r, ensure_ascii=False)},
                {"role": "user", "content": f"Invalid output: phase={phase} requires non-empty cmd unless decision=ask_user/chat/done. Re-emit valid JSON."}
            ]
            continue
        write("TASK.md", patch)
        try:
            res = run(cmd) if phase in ("init", "act", "verify") else {"code": 0, "output": "[planning]"}
        except KeyboardInterrupt:
            msgs, should_quit = control_console(msgs, log_path)
            if should_quit:
                print("QUIT")
                return
            continue
        out = f"[exit_code]\n{res['code']}\n{res['output']}" + git_snapshot()
        append(log_path, f"phase={phase}\ncmd={cmd}\nexit_code={res['code']}\noutput:\n{out}\n")
        if phase == "init":
            context_ok, context_reason = project_context_status()
            append(log_path, f"project_context_ok={context_ok}; reason={context_reason}\n")
        update_session_summary(step, phase, cmd, out, decision, notes)
        print(f"\n== step {step} {phase} ==\ncmd: {cmd or '(none)'}\n{out[-1200:]}")
        if phase == "verify" and cmd.strip() and res["code"] == 0:
            msgs, action = post_verify_console(msgs, log_path)
            if action == "done":
                print("DONE (user confirmed)")
                return
            if action == "quit":
                print("QUIT")
                return
        msgs += [
            {"role": "assistant", "content": json.dumps(r, ensure_ascii=False)},
            {"role": "user", "content":
                f"command_output:\n{out}\n\nExisting files now:\n{file_inventory()[0]}"
                f"\n\nAgent.md now:\n{read('Agent.md', '(missing)')[:12000]}"
                f"\n\nScratch now ({SCRATCH_FILE}):\n{read(SCRATCH_FILE, '(missing)')[:8000]}"
                f"\n\nCurrent TASK.md:\n{read('TASK.md')}"}
        ]
    append(log_path, "status=MAX_STEPS exceeded\n")
    print("MAX_STEPS exceeded")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
