#!/usr/bin/env python3
"""The usage chart's automatic update, run outside the conversation that triggers it.

Keeps the page read, file listing and publish out of the transcript the user reads
(skills/chart/DESIGN.md, "The chart runner"). Only an agent can publish: a hook has no
tools, and a headless `claude -p` run has no Artifact tool. A background session
(`claude --bg`) has it. So:

  launch  run by the done recipe in the probe's message. Takes this machine's lock, runs
          `usage_chart.prepare()` into RUN_DIR and, when there is something to send,
          starts the detached `run` child and returns at once. Prints the chart line.
  run     the child. Starts one background session in RUN_DIR with Artifact and Read as
          its only tools and no customizations (`--safe-mode`), waits for it, confirms the publish
          from its transcript, removes the session, keeps its transcript as
          logs/chart_runner_last.jsonl, records the outcome in logs/chart_runner.json and
          releases the lock.
  status  the last outcome, one JSON line.

Nothing here raises to its caller: a chart that could not update is one line in a report.
"""
import argparse, glob, json, os, re, shutil, subprocess, sys, tempfile, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths                                                  # noqa: E402

RUN_DIR = os.path.join(tempfile.gettempdir(), "claudette-chart-runner")
PLAN = os.path.join(RUN_DIR, "plan.json")
LOCK = paths.log("chart_runner_lock.json")
STATE = paths.log("chart_runner.json")
LAST_TRANSCRIPT = paths.log("chart_runner_last.jsonl")
MODEL = "haiku"         # the job is mechanical
RUN_TIMEOUT = 300           # a run publishes in well under a minute; past this it is stuck
POLL_S = 5
# A lock older than this outlived its run (a killed child). It must exceed the longest a
# live run can take: the poll deadline, one `agents` call that started just before it and
# the `rm` (60 s each), the transcript move.
LOCK_MAX = RUN_TIMEOUT + 300
BG_ID = re.compile(r"backgrounded\W+([0-9a-f]{6,})")
NOTHING_NEW = "nothing the chart shows has changed"


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _write(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(obj, f)
        os.replace(tmp, path)
    except Exception:
        pass


def _ago(seconds):
    s = max(0, int(seconds))
    return "%ds" % s if s < 90 else "%dm" % round(s / 60.0)


# --- the lock: one update per machine at a time ------------------------------------

def take_lock(now=None):
    """None when this process now holds the lock, else the holder's record. Created with
    O_EXCL so two sessions launching in the same second cannot both pass. Age, not a pid
    probe, decides staleness: on Windows `os.kill(pid, 0)` sends CTRL_C_EVENT."""
    now = time.time() if now is None else now
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            held = _read(LOCK)
            try:
                ts = float(held.get("ts") or os.path.getmtime(LOCK))
            except OSError:
                continue                                  # released meanwhile: retry
            if now - ts < LOCK_MAX:
                return dict(held, ts=ts)
            try:
                os.unlink(LOCK)
            except OSError:
                pass
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"ts": int(now), "pid": os.getpid()}, f)
        return None
    return {"ts": now}


def release_lock():
    try:
        os.unlink(LOCK)
    except OSError:
        pass


# --- launch ------------------------------------------------------------------------

def _previous_failure(now):
    st = _read(STATE)
    if st.get("state") == "failed":
        return " (the last background update failed: %s)" % st.get("reason", "unknown")
    if st.get("state") == "running" and now - float(st.get("ts") or 0) > LOCK_MAX:
        # the child died before writing its outcome: an import error, a killed process
        return " (the last background update never reported back)"
    return ""


def launch(now=None):
    """The chart line for the done report, and at most one detached child."""
    now = time.time() if now is None else now
    try:
        import prefs
        if not prefs.effective("chart_autoupdate"):
            return "Usage chart_autoupdate is off"
    except Exception:
        pass
    held = take_lock(now)
    if held:
        return ("Usage chart: an update is already running (started %s ago)"
                % _ago(now - held["ts"]))
    try:
        claude = shutil.which("claude")
        if not claude:
            release_lock()
            return ("Usage chart: not updated, no `claude` command on PATH here; "
                    "/claudette:chart does it")
        import usage_chart
        os.makedirs(RUN_DIR, exist_ok=True)
        out = usage_chart.prepare(RUN_DIR)
        if not out.get("publish"):
            release_lock()
            if out.get("needs"):
                line = ("Usage chart: this machine joins the chart once by hand, "
                        "type /claudette:chart")
            elif NOTHING_NEW in (out.get("skipped") or ""):
                line = "Usage chart: nothing new"
            else:
                line = "Usage chart: not updated, %s" % out.get("skipped")
            return line + _previous_failure(now)
        _write(PLAN, dict(out, claude=claude, launched=int(now)))
        _write(STATE, {"ts": int(now), "state": "running", "rows": out.get("rows"),
                       "windows": out.get("windows")})
        _spawn([sys.executable, os.path.abspath(__file__), "run"])
        return ("Usage chart: update handed to a background session (%s readings, "
                "%s windows)" % (out.get("rows"), out.get("windows")))
    except Exception as e:
        release_lock()
        return "Usage chart: not updated, %s: %s" % (type(e).__name__, e)


def _spawn(args):
    """Detached, no window, outliving the Bash call that launched it. A hidden console
    rather than DETACHED_PROCESS, for the reason in git_autofetch._spawn: every console
    program the child starts (claude.exe here) would otherwise get a window of its own."""
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True, cwd=RUN_DIR)
    if os.name == "nt":
        base = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(args, creationflags=base | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
        except OSError:
            subprocess.Popen(args, creationflags=base, **kw)
    else:
        subprocess.Popen(args, start_new_session=True, **kw)


# --- run: the detached child ---------------------------------------------------------

def prompt(plan):
    """What the background session is told. It sees nothing else: no CLAUDE.md, no skill,
    no hook, Artifact and Read as its only tools."""
    pub = plan["publish"]
    url = pub["url"]
    return "\n".join([
        "You are claudette's chart runner: publish one update of the user's own usage chart, "
        "then stop. Nobody is watching this session and nothing else is wanted from you.",
        "1. In ONE message, two Artifact calls on url %s: action \"read\" with no path, and "
        "action \"list\" with scope \"files\"." % url,
        "2. If the page you read has a chart-index script whose renderer is NEWER than %d, "
        "publish nothing and reply exactly: STALE. A page with no chart-index at all is an "
        "older page: carry on." % plan.get("renderer", 0),
        "3. Otherwise make ONE Artifact publish with exactly these fields and no others: "
        "url %s, file_path %s, files %s."
        % (json.dumps(url), json.dumps(pub["file_path"]), json.dumps(pub["files"])),
        "4. If that publish is refused because files changed since you saw them: Artifact "
        "read with path each file it names, read the page again with no path and redo "
        "step 2, then publish the same once more. Refused twice, or denied: reply FAILED "
        "followed by the refusal's first sentence.",
        "5. Once a publish succeeds, reply exactly: DONE.",
        "Do not open the artifact, pass force, add capabilities, an icon or a description, "
        "or call any other action or tool.",
    ])


def bg_command(plan):
    # The variadic options (--tools, --allowedTools, --disallowedTools) take every word
    # up to the next option, so the prompt must follow a single-value option: placed
    # after one of them it is swallowed as a tool name and the session waits with an
    # empty prompt. Read is there because a publish reads the files it sends, and
    # --tools denies every tool it does not name. --safe-mode: no CLAUDE.md, skills,
    # hooks or MCP servers in the throwaway session; --allowedTools: no approval
    # prompt a session nobody watches could stall on.
    return [plan["claude"], "--bg", "--safe-mode",
            "--tools", "Artifact", "Read", "--allowedTools", "Artifact", "Read",
            "--disallowedTools", "advisor",
            "--model", MODEL, prompt(plan)]


def _quiet():
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _claude(claude, *args, timeout=60):
    p = subprocess.run([claude] + list(args), capture_output=True, timeout=timeout,
                       cwd=RUN_DIR, **_quiet())
    return p.returncode, ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace")


def session_state(claude, bg):
    """(state, sessionId) of background session `bg` from `claude agents --json --all`;
    ('gone', None) when it is not listed. States seen on 2.1.303: `done` (its turn ended),
    `blocked` (waiting on input nobody will give)."""
    rc, out = _claude(claude, "agents", "--json", "--all")
    try:
        rows = json.loads(out[out.index("["):])
    except ValueError:
        return "unknown", None
    for r in rows:
        if isinstance(r, dict) and r.get("id") == bg:
            return r.get("state") or "unknown", r.get("sessionId")
    return "gone", None


def outcome(transcript):
    """(published, last reply) from the session's transcript. Published means an Artifact
    publish whose result says so; the reply alone is never trusted for that."""
    calls, published, reply = {}, False, ""
    try:
        with open(transcript, encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                for b in ((row.get("message") or {}).get("content") or []):
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use" and b.get("name") == "Artifact":
                        inp = b.get("input") or {}
                        if inp.get("file_path") and inp.get("action", "publish") == "publish":
                            calls[b.get("id")] = True
                    elif b.get("type") == "tool_result" and b.get("tool_use_id") in calls:
                        c = b.get("content")
                        text = c if isinstance(c, str) else " ".join(
                            x.get("text", "") for x in (c or []) if isinstance(x, dict))
                        if not b.get("is_error") and text.lstrip().startswith("Published "):
                            published = True
                    elif b.get("type") == "text" and row.get("type") == "assistant":
                        reply = (b.get("text") or "").strip()
    except OSError:
        pass
    return published, reply


def usage(transcript):
    """Token totals of the session, one count per API response (a response is written as
    one row per content block, all carrying the same usage)."""
    seen, tot, model = set(), {}, None
    try:
        with open(transcript, encoding="utf-8") as f:
            for line in f:
                m = (json.loads(line).get("message") or {})
                u, mid = m.get("usage"), m.get("id")
                if not u or mid in seen:
                    continue
                seen.add(mid)
                model = m.get("model") or model
                for k in ("input_tokens", "cache_creation_input_tokens",
                          "cache_read_input_tokens", "output_tokens"):
                    tot[k] = tot.get(k, 0) + int(u.get(k) or 0)
    except (OSError, ValueError):
        pass
    return dict(tot, model=model, responses=len(seen)) if seen else None


def _keep_transcript(sid):
    """Move the session's transcript out of ~/.claude/projects, where it would show as a
    session of its own in every list (`.sess`, `/resume`), to the one file this runner
    keeps for diagnosis. Returns that path, or None."""
    if not sid:
        return None
    found = glob.glob(os.path.join(paths.CLAUDE_DIR, "projects", "*", sid + ".jsonl"))
    if not found:
        return None
    for _ in range(5):                    # the session's process may still hold it
        try:
            shutil.move(found[0], LAST_TRANSCRIPT)
            break
        except OSError:
            time.sleep(1)
    else:
        return found[0]
    folder = os.path.dirname(found[0])
    for extra in (os.path.join(folder, sid),):           # per-session sidecar folder
        shutil.rmtree(extra, ignore_errors=True)
    try:
        os.rmdir(folder)                                 # only when nothing else is there
    except OSError:
        pass
    return LAST_TRANSCRIPT


def run():
    plan = _read(PLAN)
    t0 = time.time()
    res = {"ts": int(t0), "state": "failed", "rows": plan.get("rows"),
           "windows": plan.get("windows"), "model": MODEL}
    claude, bg, sid = plan.get("claude"), None, None
    try:
        if not plan.get("publish") or not claude:
            res["reason"] = "no plan to run"
            return
        p = subprocess.run(bg_command(plan), capture_output=True, timeout=120, cwd=RUN_DIR,
                           **_quiet())
        out = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", "replace")
        m = BG_ID.search(out)
        if not m:
            res["reason"] = "the background session did not start: %s" % (
                " ".join(out.split())[-200:] or "exit %d" % p.returncode)
            return
        bg = res["id"] = m.group(1)
        state = "unknown"
        while time.time() - t0 < RUN_TIMEOUT:
            time.sleep(POLL_S)
            state, sid = session_state(claude, bg)
            if state in ("done", "blocked", "gone"):
                break
        res["session_state"] = state
        _claude(claude, "rm", bg)
        bg = None
        kept = _keep_transcript(sid)
        published, reply = outcome(kept) if kept else (False, "")
        res["reply"] = reply[:300]
        res["usage"] = usage(kept) if kept else None
        if published:
            import usage_chart
            usage_chart.cmd_pushed(None)
            res["state"], res["reason"] = "ok", None
        elif reply:
            res["reason"] = reply[:200]
        elif state == "blocked":
            res["reason"] = "the background session waited on input nobody can give"
        elif state not in ("done", "gone"):
            res["reason"] = "no answer within %ds" % RUN_TIMEOUT
        else:
            res["reason"] = "no publish in the session's transcript"
    except Exception as e:
        res["reason"] = "%s: %s" % (type(e).__name__, e)
    finally:
        if bg:
            try:
                _claude(claude, "rm", bg)
            except Exception:
                pass
        res["secs"] = int(time.time() - t0)
        _write(STATE, res)
        release_lock()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("launch", help="the done recipe's call: the chart line, maybe a child")
    sub.add_parser("run", help="the detached child (launch starts it)")
    sub.add_parser("status", help="the last outcome, one JSON line")
    a = ap.parse_args()
    if a.cmd == "launch":
        print(launch())
    elif a.cmd == "run":
        run()
    else:
        print(json.dumps(_read(STATE)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
