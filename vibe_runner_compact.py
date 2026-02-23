#!/usr/bin/env python3
import json, os, re, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime

P=os.getenv("PROVIDER","openai"); M=os.getenv("MODEL","qwen3-max" if P=="qwen" else "gpt-4.1-mini")
B=os.getenv("BASE_URL","https://api.openai.com/v1"); K=os.getenv("API_KEY") or os.getenv("OPENAI_API_KEY")
S=int(os.getenv("MAX_STEPS","30")); T=int(os.getenv("CMD_TIMEOUT","20")); OL=int(os.getenv("OUTPUT_LIMIT","12000"))
PL=int(os.getenv("PAGE_LINES","120")); SF=os.getenv("SCRATCH_FILE",".agent_scratch.md")
SUMF=os.getenv("SESSION_SUMMARY_FILE",".session_summary.md"); HT=int(os.getenv("HISTORY_TURNS","12"))
MA=int(os.getenv("MAX_AGENT_INJECT","8000")); MS=int(os.getenv("MAX_SCRATCH_INJECT","4000")); SE=int(os.getenv("SUMMARY_EVERY","3"))
D=r"\b(rm\s+-rf|mkfs|dd\s+if=|shutdown|reboot|curl|wget|nc\b|ssh\b)"
PR="""You are Vibe Coding Runner agent. Output STRICT JSON only.
Schema:{"phase":"init|plan|act|verify|chat|done","decision":"direct_execute|ask_user","questions":["optional"],"say":"user-facing reply when phase=chat","goal":"...","checklist":["[ ] ..."],"cmd":"single bash command","task_md_patch":"FULL TASK.md","memory_add":["optional stable fact/decision"],"notes":"optional"}
Rules: step1 must be init; every step needs task_md_patch; one bash cmd in init/act/verify; file edits via bash only.
For long files read by pagination across steps: nl -ba <file> | sed -n '<start>,<end>p' (PAGE_LINES from context).
Use SCRATCH_FILE for temporary notes. Don't assume runtime; use project docs; ask_user when ambiguous.
Prefer chat for consulting; done only when goal is fully satisfied.
Init builds/updates Agent.md from README/tree/git/existing Agent.md."""

rf=lambda p,d="":open(p).read() if os.path.exists(p) else d

def wf(p,s,a=False):
    with open(p,"a" if a else "w") as f:f.write(s)

def clip(s,n): return s if len(s)<=n else s[:n]+"\n...[truncated]..."

def sh(c):
    if not c.strip(): return {"code":0,"output":"[no command]"}
    if re.search(D,c): return {"code":126,"output":"[blocked by denylist]"}
    try:
        p=subprocess.run(c,shell=True,text=True,capture_output=True,timeout=T); o=p.stdout+p.stderr
        return {"code":p.returncode,"output":o if len(o)<=OL else o[:OL//2]+"\n...[truncated]...\n"+o[-OL//2:]}
    except subprocess.TimeoutExpired: return {"code":124,"output":"[command timeout]"}

def gits():
    if sh("git rev-parse --is-inside-work-tree")["code"]!=0: return "\n[git]\n[disabled: not a git repository]"
    s=sh("git status --porcelain")["output"]; d=sh("git diff -- . ':(exclude)TASK.md'")["output"]
    return f"\n[git status]\n{s}\n[git diff]\n{d}"

def inv():
    r=sh("rg --files -g '!.git' -g '!logs/*' 2>/dev/null | head -n 200")
    if r["code"]!=0 or not r["output"].strip(): r=sh("find . -type f -not -path './.git/*' -not -path './logs/*' | sed 's#^./##' | head -n 200")
    fs=[x.strip() for x in r["output"].splitlines() if x.strip()]
    return ("(no files found)",[]) if not fs else ("\n".join(f"- {p}" for p in fs),fs)

def previews(fs):
    p=[f for f in fs if f.endswith((".md",".txt",".py",".html",".js",".css",".json",".yml",".yaml"))][:8]
    if not p: return "(no previewable text files)"
    o=[]
    for f in p:
        try:o.append(f"\n### {f}\n{rf(f,'')[:800].replace(chr(13),'').strip() or '(empty)'}")
        except Exception:o.append(f"\n### {f}\n(unreadable)")
    return "\n".join(o)

def psum(goal):
    ig=sh("git rev-parse --is-inside-work-tree")["code"]==0; ft,fs=inv(); br=sh("git branch --show-current")["output"].strip() if ig else "(n/a)"
    return (f"Project goal: {goal}\nGit repo: {'yes' if ig else 'no'}\nGit branch: {br}\nTop files: {', '.join(fs[:12]) if fs else '(none)'}\n"
            f"Has Agent.md: {'yes' if rf('Agent.md').strip() else 'no'}\nHas README: {'yes' if (rf('README.md').strip() or rf('readme.md').strip()) else 'no'}\nExisting files list:\n{ft}\n")

def ensure_agent(goal):
    if rf("Agent.md").strip(): return
    wf("Agent.md",f"# Agent.md\n\n## Project Goal\n- {goal}\n\n## Project Snapshot\n{psum(goal)}\n## Run/Test Commands\n- Fill after init\n\n## Coding Style\n- Keep edits minimal and reviewable\n\n## Constraints\n- Prefer local commands and deterministic checks\n")

def ensure_scratch(reset=True):
    if os.path.exists(SF) and not reset: return
    wf(SF,"# Agent Scratchpad\n\n- Temporary working notes for this runner session only.\n- Safe to overwrite/append.\n")

def ensure_sum(reset=True):
    if os.path.exists(SUMF) and not reset: return
    wf(SUMF,"# Session Summary\n\n## Goal\n- (pending)\n\n## Completed\n- (none)\n\n## Decisions\n- (none)\n\n## Open Items\n- (none)\n")

def llm(ms):
    if not K: print("API_KEY/OPENAI_API_KEY not set",file=sys.stderr); sys.exit(1)
    b=B.rstrip("/"); b="https://dashscope.aliyuncs.com/compatible-mode/v1" if P=="qwen" and "dashscope.aliyuncs.com" not in b else b
    req=urllib.request.Request(f"{b}/chat/completions",data=json.dumps({"model":M,"messages":[{"role":m["role"],"content":m["content"]} for m in ms],"response_format":{"type":"json_object"},"temperature":0.2}).encode(),headers={"Authorization":f"Bearer {K}","Content-Type":"application/json"})
    for i in range(3):
        try:
            with urllib.request.urlopen(req,timeout=60) as r: return json.loads(json.loads(r.read().decode())["choices"][0]["message"]["content"])
        except urllib.error.HTTPError as e:
            body=e.read().decode(errors="ignore")
            if i<2 and (e.code in (408,409,429) or e.code>=500): time.sleep(2**i); continue
            print(f"llm http error {e.code}: {body[-800:]}",file=sys.stderr); sys.exit(1)
        except (urllib.error.URLError,TimeoutError) as e:
            if i<2: time.sleep(2**i); continue
            print(f"llm network error: {e}",file=sys.stderr); sys.exit(1)

def add(ms,a,u): ms+=[{"role":"assistant","content":json.dumps(a,ensure_ascii=False)},{"role":"user","content":u}]

def llm_msgs(ms):
    base=ms[:2]; tail=ms[2:]; recent=tail[-(HT*2):] if HT>0 else tail
    return base+[{"role":"user","content":f"Session summary (compressed):\n{clip(rf(SUMF,''),6000)}"}]+recent

def sum_update(step,ph,cmd,dc,notes):
    if step%SE!=0 and ph!="done": return
    wf(SUMF,f"\n- Step {step}: phase={ph}, decision={dc}, cmd={(cmd[:120] or '(none)')}, notes={clip(notes or '',160).replace(chr(10),' ')}\n",True)

def ask(qs,log):
    print("\nAgent asks for clarification:"); [print(f"- {q}") for q in qs[:3]]
    try:a=input("you> ").strip()
    except KeyboardInterrupt: print(""); return None
    wf(log,f"human_answer={a}\n",True); return a

def con(kind,ms,log,say=""):
    h={"control":"[paused] commands: resume | feedback <text> | run <bash> | quit","verify":"[verify passed] commands: done | resume | feedback <text> | run <bash> | quit","done":"[agent says done] commands: done | resume | feedback <text> | run <bash> | quit","chat":"[chat] commands: resume | feedback <text> | quit"}
    p={"control":"control>","verify":"post-verify>","done":"post-verify>","chat":"chat>"}
    if kind=="chat" and say: print(f"\nAgent: {say}"); wf(log,f"chat_say={say}\n",True)
    print("\n"+h[kind]); print("bare text => feedback + resume")
    while 1:
        try:raw=input(p[kind]+" ").strip()
        except KeyboardInterrupt: print("\nstill waiting..."); continue
        if raw in ("resume","r"):
            wf(log,f"status=resume_after_{kind}\n",True); print("action=resume")
            if kind in ("verify","done"): ms.append({"role":"user","content":"Human chose resume. Continue improving; do not finalize yet."})
            return ms,"resume"
        if raw in ("done","d") and kind in ("verify","done"):
            if input("confirm done? [y/N] ").strip().lower() in ("y","yes"): wf(log,"status=done_by_user\n",True); print("action=done"); return ms,"done"
            print("action=cancel_done"); continue
        if raw in ("quit","q","exit") or (kind=="chat" and raw=="done"):
            if kind=="chat" or input("confirm quit? [y/N] ").strip().lower() in ("y","yes"): wf(log,f"status=quit_after_{kind}\n",True); print("action=quit"); return ms,"quit"
            print("action=cancel_quit"); continue
        if raw.startswith("run ") and kind!="chat":
            c=raw[4:].strip(); r=sh(c); out=f"[exit_code]\n{r['code']}\n{r['output']}"+gits()
            wf(log,f"{kind}_run cmd={c}\noutput:\n{out}\n",True); print(out[-1200:]); ms.append({"role":"user","content":f"human_manual_command: {c}\noutput:\n{out}"}); print("action=run_sent"); continue
        fb=raw[9:].strip() if raw.startswith("feedback ") else raw
        if fb:
            wf(log,f"{kind}_feedback={fb}\nstatus=resume_after_{kind}_bare_text\n",True); ms.append({"role":"user","content":f"Human feedback: {fb}"}); print("action=feedback_sent\naction=resume"); return ms,"resume"
        print("empty input ignored")

def main():
    goal=" ".join(sys.argv[1:]).strip() or input("Goal> ").strip(); os.makedirs("logs",exist_ok=True)
    log=f"logs/run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"; ensure_agent(goal); ensure_scratch(True); ensure_sum(True)
    wf("TASK.md",f"# Task Log\n\n## Goal\n- {goal}\n\n## Checklist\n- [ ] Init\n- [ ] Plan\n")
    wf(log,f"# Vibe Runner Log\nstarted_at={datetime.now().isoformat()}\nprovider={P}\nmodel={M}\ngoal={goal}\n\n")
    print(f"log file: {log}"); ft,fs=inv(); wf(log,f"workspace_files:\n{ft}\n",True)
    ms=[{"role":"system","content":PR},{"role":"user","content":f"Goal: {goal}\n\nWorking directory: {os.getcwd()}\n\nContext config:\n- PAGE_LINES={PL}\n- SCRATCH_FILE={SF}\n\nProject summary:\n{psum(goal)}\n\nExisting files:\n{ft}\n\nText previews:\n{previews(fs)}\n\nAgent.md:\n{rf('Agent.md')}\n\nScratch ({SF}):\n{rf(SF)[:8000]}\n\nCurrent TASK.md:\n{rf('TASK.md')}"}]
    wf(SUMF,f"\n## Goal\n- {goal}\n",True); last_a=None; last_s=None
    for step in range(1,S+1):
        ca,cs=rf("Agent.md","(missing)"),rf(SF,"(missing)"); inj=[]
        if ca!=last_a: inj.append(f"Latest Agent.md (always authoritative):\n{clip(ca,MA)}"); last_a=ca
        if cs!=last_s: inj.append(f"Latest Scratch ({SF}):\n{clip(cs,MS)}"); last_s=cs
        if inj: ms.append({"role":"user","content":"\n\n".join(inj)})
        try:r=llm(llm_msgs(ms))
        except KeyboardInterrupt:
            ms,a=con("control",ms,log)
            if a=="quit": print("QUIT"); return
            continue
        ph,cmd,patch=r.get("phase",""),(r.get("cmd") or ""),r.get("task_md_patch"); dc=(r.get("decision") or "direct_execute").strip(); qs=r.get("questions") or []; say=(r.get("say") or "").strip(); notes=(r.get("notes") or "").strip()
        wf(log,f"\n## step {step}\nresponse={json.dumps(r,ensure_ascii=False)}\n",True)
        if ph not in ("init","plan","act","verify","chat","done"): print(f"invalid phase: {ph}"); return
        if dc not in ("direct_execute","ask_user"): print(f"invalid decision: {dc}"); return
        if step==1 and ph!="init": print("step1 must be init"); return
        if patch is None: print("task_md_patch required"); return
        if ph=="done":
            wf("TASK.md",patch); wf(log,"phase=done\n",True); ms.append({"role":"assistant","content":json.dumps(r,ensure_ascii=False)})
            ms,a=con("done",ms,log)
            if a=="done": sum_update(step,ph,cmd,dc,notes); print("DONE (user confirmed)"); return
            if a=="quit": print("QUIT"); return
            continue
        if ph=="chat":
            wf("TASK.md",patch); ms.append({"role":"assistant","content":json.dumps(r,ensure_ascii=False)})
            ms,a=con("chat",ms,log,say or r.get("notes", ""))
            if a=="quit": print("QUIT"); return
            continue
        if dc=="ask_user":
            ans=ask(qs or [r.get("notes") or "Need more information. Please clarify."],log)
            if ans is None:
                ms,a=con("control",ms,log)
                if a=="quit": print("QUIT"); return
                continue
            if ans.lower() in ("quit","q","exit"): print("QUIT"); return
            add(ms,r,f"Human answer: {ans}\n\nLatest Agent.md:\n{clip(rf('Agent.md','(missing)'),MA)}\n\nLatest Scratch ({SF}):\n{clip(rf(SF,'(missing)'),MS)}\n\nCurrent TASK.md:\n{rf('TASK.md')}")
            continue
        if ph=="plan" and cmd.strip(): print("warning: plan step must not execute command, retrying..."); add(ms,r,"Invalid output: phase=plan must have empty cmd. Please emit a valid next JSON step."); continue
        if ph in ("init","act","verify") and not cmd.strip(): print(f"warning: {ph} step missing command, retrying..."); add(ms,r,f"Invalid output: phase={ph} requires non-empty cmd unless decision=ask_user/chat/done. Re-emit valid JSON."); continue
        wf("TASK.md",patch)
        try:rs=sh(cmd) if ph in ("init","act","verify") else {"code":0,"output":"[planning]"}
        except KeyboardInterrupt:
            ms,a=con("control",ms,log)
            if a=="quit": print("QUIT"); return
            continue
        out=f"[exit_code]\n{rs['code']}\n{rs['output']}"+gits(); wf(log,f"phase={ph}\ncmd={cmd}\nexit_code={rs['code']}\noutput:\n{out}\n",True); sum_update(step,ph,cmd,dc,notes)
        print(f"\n== step {step} {ph} ==\ncmd: {cmd or '(none)'}\n{out[-1200:]}")
        if ph=="verify" and cmd.strip() and rs["code"]==0:
            ms,a=con("verify",ms,log)
            if a in ("done","quit"): print("DONE (user confirmed)" if a=="done" else "QUIT"); return
        add(ms,r,f"command_output:\n{out}\n\nExisting files now:\n{inv()[0]}\n\nAgent.md now:\n{clip(rf('Agent.md','(missing)'),MA)}\n\nScratch now ({SF}):\n{clip(rf(SF,'(missing)'),MS)}\n\nCurrent TASK.md:\n{rf('TASK.md')}")
    wf(log,"status=MAX_STEPS exceeded\n",True); print("MAX_STEPS exceeded")

if __name__=="__main__":
    try: main()
    except KeyboardInterrupt: print("\nInterrupted by user")
