#!/usr/bin/env python3
import json, os, re, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime

PROVIDER = os.getenv("PROVIDER", "openai")
MODEL = os.getenv("MODEL", "qwen3-max" if PROVIDER == "qwen" else "gpt-4.1-mini")
BASE_URL = os.getenv("BASE_URL", "https://api.openai.com/v1")
API_KEY = os.getenv("API_KEY") or os.getenv("OPENAI_API_KEY")
MAX_STEPS = int(os.getenv("MAX_STEPS", "30"))
TIMEOUT = int(os.getenv("CMD_TIMEOUT", "20"))
DENY = r"\b(rm\s+-rf|mkfs|dd\s+if=|shutdown|reboot|curl|wget|nc\b|ssh\b)"
PROMPT = """You are Vibe Coding Runner agent.
Output STRICT JSON only. No markdown, no extra text.

Schema:
{"phase":"plan|act|verify|chat|done","decision":"direct_execute|ask_user","questions":["optional"],"say":"user-facing reply when phase=chat","goal":"...","checklist":["[ ] ..."],"cmd":"single bash command","task_md_patch":"FULL TASK.md","memory_add":["optional stable fact/decision"],"notes":"optional"}

Execution rules:
1) Step 1 MUST be phase=plan and cmd="".
2) Every step MUST include task_md_patch (full TASK.md content).
3) Use exactly ONE bash command in cmd when phase is act/verify.
4) For file edits, use bash methods only (cat <<'EOF', sed, perl, etc.).

Tool/runtime rules:
1) Do not assume a specific programming language or runtime.
2) Prefer commands documented in Agent.md or project files.
3) Before using a tool/runtime, ensure it exists in this environment.
4) If runtime/tool choice is ambiguous or unavailable, use decision=ask_user.

Decision rules:
1) If context is sufficient, use decision=direct_execute.
2) If context is insufficient, use decision=ask_user with concise questions and do not execute.
3) For explanation/consulting tasks, prefer phase=chat and provide user-facing text in say.
4) Use phase=done only when task goal is fully satisfied."""


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
        return {"code": p.returncode, "output": (p.stdout + p.stderr)[-6000:]}
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
        if raw in ("quit", "q", "exit"):
            c = input("confirm quit? [y/N] ").strip().lower()
            if c in ("y", "yes"):
                append(log_path, "control=quit\n")
                print("action=quit")
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
        if raw in ("quit", "q", "exit", "done"):
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
    agent = read("Agent.md", "# Agent\n- Keep changes minimal\n")
    write("TASK.md", f"# Task Log\n\n## Goal\n- {goal}\n\n## Checklist\n- [ ] Plan\n")
    write(
        log_path,
        f"# Vibe Runner Log\nstarted_at={datetime.now().isoformat()}\n"
        f"provider={PROVIDER}\nmodel={MODEL}\ngoal={goal}\n\n",
    )
    print(f"log file: {log_path}")
    file_list, files = file_inventory()
    previews = file_previews(files)
    append(log_path, f"workspace_files:\n{file_list}\n")
    msgs = [
        {"role": "system", "content": PROMPT},
        {"role": "user", "content":
            f"Goal: {goal}\n\nWorking directory: {os.getcwd()}\n\n"
            f"Existing files:\n{file_list}\n\n"
            f"Text previews:\n{previews}\n\n"
            f"Agent.md:\n{agent}\n\nCurrent TASK.md:\n{read('TASK.md')}"}
    ]
    for step in range(1, MAX_STEPS + 1):
        try:
            r = llm(msgs)
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
        append(log_path, f"\n## step {step}\nresponse={json.dumps(r, ensure_ascii=False)}\n")
        if phase not in ("plan", "act", "verify", "chat", "done"):
            append(log_path, f"error=invalid phase: {phase}\n")
            print(f"invalid phase: {phase}"); return
        if decision not in ("direct_execute", "ask_user"):
            append(log_path, f"error=invalid decision: {decision}\n")
            print(f"invalid decision: {decision}"); return
        if step == 1 and phase != "plan":
            print("step1 must be plan"); return
        if patch is None:
            print("task_md_patch required"); return
        if phase == "done":
            write("TASK.md", patch)
            append(log_path, "phase=done\n")
            msgs += [{"role": "assistant", "content": json.dumps(r, ensure_ascii=False)}]
            msgs, action = post_verify_console(msgs, log_path, mode="done")
            if action == "done":
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
            if action == "quit":
                print("QUIT")
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
            if ans.lower() in ("quit", "q", "exit"):
                print("QUIT")
                return
            msgs += [
                {"role": "assistant", "content": json.dumps(r, ensure_ascii=False)},
                {"role": "user", "content": f"Human answer: {ans}\nCurrent TASK.md:\n{read('TASK.md')}"}
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
        if phase in ("act", "verify") and not cmd.strip():
            append(log_path, f"warning=missing cmd in {phase}; requested retry\n")
            print(f"warning: {phase} step missing command, retrying...")
            msgs += [
                {"role": "assistant", "content": json.dumps(r, ensure_ascii=False)},
                {"role": "user", "content": f"Invalid output: phase={phase} requires non-empty cmd unless decision=ask_user/chat/done. Re-emit valid JSON."}
            ]
            continue
        write("TASK.md", patch)
        try:
            res = run(cmd) if phase in ("act", "verify") else {"code": 0, "output": "[planning]"}
        except KeyboardInterrupt:
            msgs, should_quit = control_console(msgs, log_path)
            if should_quit:
                print("QUIT")
                return
            continue
        out = f"[exit_code]\n{res['code']}\n{res['output']}" + git_snapshot()
        append(log_path, f"phase={phase}\ncmd={cmd}\nexit_code={res['code']}\noutput:\n{out}\n")
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
                f"\n\nCurrent TASK.md:\n{read('TASK.md')}"}
        ]
    append(log_path, "status=MAX_STEPS exceeded\n")
    print("MAX_STEPS exceeded")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted by user")
