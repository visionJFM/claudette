#!/usr/bin/env python3
# Claude Throttle - PreToolUse pacing hook. Principles: skills/throttle/THROTTLE.md
#
# Paces heavy tool calls (web/agents) against account usage curves so background
# and fan-out work yields to interactive headroom:
#   cap5 = 100 - 11*h   (h = hours until 5h window reset; ~45% allowance at window start)
#   cap7 = 100 - 8*d    (d = days until 7d window reset;  ~44% allowance at week start)
# A session multiplier m scales both caps. Throttled when EITHER window is over
# its scaled cap. Small overage: silent sleep. Large overage: deny with a message
# to the user (systemMessage) and instructions to the model (deny reason).
#
# FAIL-OPEN by design: any error, missing/stale snapshot, or non-heavy tool => allow.
# A window whose percentage the snapshot does not carry is UNKNOWN, never 0: that
# window is simply not evaluated (the other one still is): a missing value read as
# 0% used would be a fabricated all-clear.
#
# Reads:  claudette/logs/claude_usage_last.json      (written by hook_statusline.py)
#         claudette/logs/throttle_control.json (written by the /claudette:throttle skill)
# Writes: claudette/logs/throttle_notify.json  (single writer: this hook; rate-limits
#                                               the user-facing message, not the deny)
# Env:    CLAUDE_THROTTLE_M      "off" or a multiplier, set at launch for background jobs
#         CLAUDE_THROTTLE_STATUS alternate snapshot path (tests)

import json, sys, os, time

SLEEP_MAX_S = 55          # overage clearing within this: sleep silently, then allow
STALE_S = 30 * 60         # snapshot older than this: no-op (fail open)
NOTIFY_EVERY_S = 120      # min seconds between user-facing throttle messages
HEAVY_TOOLS = {"WebSearch", "WebFetch", "Agent", "Task", "Workflow"}
SLOPE_5H = 11.0
SLOPE_7D = 8.0


def allow():
    sys.exit(0)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


try:
    data = json.load(sys.stdin)
except Exception:
    allow()

if data.get("tool_name", "") not in HEAVY_TOOLS:
    allow()

now = time.time()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

logs = paths.LOGS
session_id = data.get("session_id", "")

# --- resolve multiplier m (precedence: session override > env > global) ---
m = 1.0
try:
    with open(os.path.join(logs, "throttle_control.json"), "r", encoding="utf-8") as f:
        ctl = json.load(f)
except Exception:
    ctl = {}

def _live(entry):
    return isinstance(entry, dict) and (not entry.get("expires") or float(entry["expires"]) > now)

sess = ctl.get("sessions", {}).get(session_id)
env_m = os.environ.get("CLAUDE_THROTTLE_M", "").strip().lower()
if _live(sess):
    if sess.get("off"):
        allow()
    m = float(sess.get("m", 1.0))
elif env_m:
    if env_m == "off":
        allow()
    try:
        m = float(env_m)
    except ValueError:
        pass
elif _live(ctl.get("global")):
    g = ctl["global"]
    if g.get("off"):
        allow()
    m = float(g.get("m", ctl.get("default_m", 1.0)))
else:
    m = float(ctl.get("default_m", 1.0))
if m <= 0:
    allow()

# --- read usage snapshot ---
status_path = os.environ.get("CLAUDE_THROTTLE_STATUS") or paths.USAGE_LAST
try:
    with open(status_path, "r", encoding="utf-8") as f:
        snap = json.load(f)
except Exception:
    allow()

if now - float(snap.get("ts", 0)) > STALE_S:
    allow()  # stale: fail open, never block interactive work on old data

rl = snap.get("rate_limits", {})


def _num(window, key, unknown=None):
    v = (rl.get(window) or {}).get(key)
    return float(v) if isinstance(v, (int, float)) else unknown


used5 = _num("five_hour", "used_percentage")     # None = not measured, NOT 0%
used7 = _num("seven_day", "used_percentage")
rs5 = _num("five_hour", "resets_at", 0.0)
rs7 = _num("seven_day", "resets_at", 0.0)
if rs5 <= 0 and rs7 <= 0:
    allow()

# --- curves ---
h = max(0.0, (rs5 - now) / 3600.0) if rs5 > 0 else 0.0
d = max(0.0, (rs7 - now) / 86400.0) if rs7 > 0 else 0.0
cap5 = clamp(100.0 - SLOPE_5H * h, 0.0, 100.0)
cap7 = clamp(100.0 - SLOPE_7D * d, 0.0, 100.0)
lim5 = clamp(m * cap5, 0.0, 100.0)
lim7 = clamp(m * cap7, 0.0, 100.0)
over5 = rs5 > 0 and used5 is not None and used5 > lim5
over7 = rs7 > 0 and used7 is not None and used7 > lim7
if not (over5 or over7):
    allow()

# --- wait until the binding cap rises to current usage ---
wait = 0.0
if over5:
    h_ok = (100.0 - used5 / m) / SLOPE_5H  # cap5(h_ok)*m == used5
    wait = max(wait, (h - max(0.0, h_ok)) * 3600.0)
if over7:
    d_ok = (100.0 - used7 / m) / SLOPE_7D
    wait = max(wait, (d - max(0.0, d_ok)) * 86400.0)
wait = max(0.0, wait)

if wait <= SLEEP_MAX_S:
    time.sleep(wait)
    allow()

# --- deny with explanation ---
which = []
if over5:
    which.append("5h %.0f%% used > %.0f%% cap" % (used5, lim5))
if over7:
    which.append("7d %.0f%% used > %.0f%% cap" % (used7, lim7))
detail = ", ".join(which)
resume = time.strftime("%H:%M", time.localtime(now + wait))
if wait > 20 * 3600:
    resume = time.strftime("%a %H:%M", time.localtime(now + wait))
m_note = "" if m == 1.0 else " (m=%.2f)" % m

reason = (
    "THROTTLED: account token usage is above the throttle curve (%s)%s. "
    "Heavy tools (web search/fetch, agents, workflows) are paused until ~%s. "
    "Do NOT retry this or other heavy tools now. Continue with local-only work "
    "(Read/Edit/Bash) if useful, otherwise summarize state and end the turn, telling "
    "the user work is throttled until ~%s and that '/claudette:throttle off' bypasses it."
    % (detail, m_note, resume, resume)
)

msg = None
notify_path = os.path.join(logs, "throttle_notify.json")
try:
    with open(notify_path, "r", encoding="utf-8") as f:
        last = float(json.load(f).get("ts", 0))
except Exception:
    last = 0.0
if now - last > NOTIFY_EVERY_S:
    msg = ("Throttle: %s%s. Heavy tools (web/agents) paused, cap catches up ~%s. "
           "\"/claudette:throttle off\" to bypass." % (detail, m_note, resume))
    try:
        with open(notify_path, "w", encoding="utf-8") as f:
            json.dump({"ts": now}, f)
    except Exception:
        pass

out = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
}
if msg:
    out["systemMessage"] = msg
print(json.dumps(out))
sys.exit(0)
