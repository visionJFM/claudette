#!/usr/bin/env python3
# Claude Code StatusLine - Cross-platform Python version
# Shows context window usage, git info, rate limits

import json, sys, os, subprocess, time, math

# Claude Code sends the payload as UTF-8 and reads the line back as UTF-8,
# whatever the machine's locale, and a pipe carries no encoding label: this
# side has to match. Left to itself, Python on Windows gives a piped stdin and
# stdout the ANSI code page (cp1252): the final write then raises on any glyph
# outside it (the git arrows, the warning sign) and Claude Code shows no status
# line at all, and a non-ASCII cwd is misread so that a real repo renders
# "(no git)". errors="replace": a character that cannot be read or written
# costs one "?" rather than the line. Why here and not in the launch command:
# CLAUDETTE_DESIGN.md "The status line encodes its own output".
for _s in (sys.stdin, sys.stdout):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

data = json.load(sys.stdin)

# Extract values
cwd = data.get("workspace", {}).get("current_dir", "")
model = data.get("model", {}).get("display_name", "")
ver = data.get("version", "")

# Claude account — read from ~/.claude.json
account_email = ""
try:
    with open(os.path.join(os.path.expanduser("~"), ".claude.json"), "r", encoding="utf-8") as f:
        account_email = json.load(f).get("oauthAccount", {}).get("emailAddress", "")
except Exception:
    pass

# Effort level the session really runs at: the payload's own `effort`, else the
# transcript's newest answer. Never the settings files. Why: effort.py.
effort_level = ""
try:
    import effort
    effort_level = effort.current(data)
except Exception:
    pass

# Context window. UNKNOWN is not zero: Claude Code sends a context block with
# zero input tokens and `used_percentage: 0` after it compacts a session —
# including on its own, while the session sits idle — and keeps sending it until
# the next API response. Its own code reads that shape as "no reading"
# (`total_input_tokens === 0 ? null`), so we do too: `?`, not a measured-looking
# `c:0%|0`. Every field is nullable (`current_usage` and `used_percentage` are
# null before a session's first call), so `int(None)` must not be reached.
def _num(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


cw = data.get("context_window")
cw = cw if isinstance(cw, dict) else {}
usage = cw.get("current_usage")
usage = usage if isinstance(usage, dict) else {}
_tok = {k: _num(usage.get(k)) or 0 for k in (
    "input_tokens", "output_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens")}
total = sum(_tok.values())   # displayed count, output included as before
pct = _num(cw.get("used_percentage"))   # logged as received (frozen key)
window_size = _num(cw.get("context_window_size")) or 0
# Known only when Claude Code sent a percentage AND, if it sent token counts,
# they show an input side — the same rule as its own display.
_ctx_known = pct is not None and not (
    isinstance(cw.get("current_usage"), dict)
    and _tok["input_tokens"] + _tok["cache_creation_input_tokens"]
    + _tok["cache_read_input_tokens"] == 0)

# Rate-limit windows. UNKNOWN is not zero: a freshly launched session renders
# before its first API response, so Claude Code sends no `rate_limits` at all
# and every window is absent. A default of 0 would render `s:0%/5h w:0%/0d`,
# indistinguishable from a genuinely idle account. None here, yellow `?` at the
# render site; the next render after the first response has the real numbers.
# Percent and reset time are tracked separately so each `?`
# means exactly its own field. These are the MEASURED values (caps, write guard,
# logs); what the line shows for a window the payload lacks is _shown() below.
rl = data.get("rate_limits", {})


def _rl(window, key):
    v = (rl.get(window) or {}).get(key)
    return int(v) if isinstance(v, (int, float)) else None


session_pct = _rl("five_hour", "used_percentage")
week_pct = _rl("seven_day", "used_percentage")
session_resets = _rl("five_hour", "resets_at") or 0   # 0 = unknown, as before
week_resets = _rl("seven_day", "resets_at") or 0

cost = data.get("cost", {})

# Throttle curve caps (see skills/throttle/THROTTLE.md): allowed cumulative usage
# rises linearly to 100% at each window reset. Decimal hours/days remaining.
_now = time.time()
_h = max(0.0, (session_resets - _now) / 3600.0) if session_resets > 0 else None
_d = max(0.0, (week_resets - _now) / 86400.0) if week_resets > 0 else None
cap5 = round(max(0.0, min(100.0, 100.0 - 11.0 * _h)), 1) if _h is not None else None
cap7 = round(max(0.0, min(100.0, 100.0 - 8.0 * _d)), 1) if _d is not None else None

# ANSI colors
blue, green, magenta, cyan = "\033[34m", "\033[32m", "\033[35m", "\033[36m"
yellow, red, reset, gray = "\033[33m", "\033[31m", "\033[0m", "\033[90m"
# `repos` only. The cwd git segment is magenta and sits two positions to its
# left; at a glance the two read as one thing and the wrong repo gets fixed.
# 256-colour orchid, not bright-magenta (\033[95m),
# because that is a theme palette slot many themes render identical to magenta.
orchid = "\033[38;5;177m"

# One glyph for "not known", everywhere: yellow `?`. A missing number is shown
# only when evidence gives it (a reading this machine measured, or 0% after a
# reset known to have passed — _shown() below), never as a plausible default
# (0%, 5h): a reader cannot tell a fabricated value from a measured one, and the
# fabricated one is always the alarming reading.
UNK = f"{yellow}?{reset}"

# Warning glyph. U+26A0 is Emoji_Presentation=No, so font fallback differs per OS:
#   macOS/Linux: +U+FE0F forces Apple/Noto Color Emoji, and the terminal advances
#                the 2 cells the glyph paints -> nothing to pad.
#   Windows:     Segoe UI Emoji already colors the bare char, but the terminal
#                advances only 1 cell -> one space so it can't clip what follows.
# The pad is inside the constant, so call sites never add spacing of their own.
WARN = "⚠ " if sys.platform == "win32" else "⚠️"

# Directory
dir_name = os.path.basename(cwd)

# Token display — each part `?` on its own when not known, as for usage.
totalK = total // 1000
windowK = window_size // 1000
_tokens_known = _ctx_known and isinstance(cw.get("current_usage"), dict)
tokens_colored = (f"{totalK}k" if total >= 1000 else str(total)) if _tokens_known else ""
tokens_plain = f"/{windowK}k" if window_size else f"/{UNK}"

# Percentage color
pct_color = (red if pct > 70 else (green if pct < 25 else yellow)) if _ctx_known else ""

# Token k color
tokenK_color = red if totalK > 200 else (green if totalK < 50 else yellow)

# Git info: one `git status` for branch, ahead/behind and U/M/S (git_state.py).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
branch = ""
upstream = ""
git_suffix = ""
is_git = False
ahead = behind = 0
behind_unknown = False  # remote ref known to differ, but object not local => no exact count

try:
    import git_state
    _gs = git_state.read(cwd) if cwd else None
except Exception:
    _gs = None
if _gs:
    is_git = True
    branch = _gs["branch"]
    ahead, behind = _gs["ahead"], _gs["behind"]
    uc, mc, sc = _gs["untracked"], _gs["modified"], _gs["staged"]
    if uc > 0: git_suffix += f" {green}U{uc}{reset}"
    if mc > 0: git_suffix += f" {yellow}M{mc}{reset}"
    if sc > 0: git_suffix += f" {cyan}S{sc}{reset}"

# --- upstream staleness (read-only ls-remote cache; git_upstreams.py)
# Verifies the remote-tracking ref against the LIVE remote (TTL'd, no fetch,
# no objects, no prompts) for the cwd repo, plus BOTH repos claudette spans —
# the personal ~/.claude repo (labelled `my`) and the plugin clone
# (`claudette`), each reported separately by the `repos` segment below.
import paths
verified = False      # behind count verified against the live remote
behind_plus = False   # locally behind N AND remote moved further (>= N)
diverged = False      # behind AND ahead: histories parted, pulling would merge them
auth_failed = False   # remote REFUSED the credential (expired token): staleness unknown
nofetch = False       # remote moved, auto-fetch is on, and this repo is not opted in
repo_flags = []       # [(label, state)] for the `repos` segment, in display order

REPO_STATE_TTL = 60   # seconds; see _local_states()


def _local_state(repo):
    """(ahead, dirty) booleans from ONE `git status -b --porcelain` (~37 ms).

    One call rather than rev-list + ls-files + diff: we need booleans, not
    counts, and the branch header carries the ahead marker already.
    GIT_OPTIONAL_LOCKS=0 for the reason given in git_state.py: a background
    status must not take the index lock from the user's own git command.
    Decoded as UTF-8 for the reason given in git_hardened.run()."""
    try:
        r = subprocess.run(["git", "-C", repo, "status", "--porcelain=v1", "-b"],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=5,
                           env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"))
        if r.returncode != 0:
            return False, False
    except Exception:
        return False, False
    lines = r.stdout.splitlines()
    return ("[ahead " in (lines[0] if lines else "")), any(x.strip() for x in lines[1:])


def _local_states(repos):
    """{repo: {ts, ahead, dirty}} — TTL-cached, refreshed per repo.

    Unpushed/uncommitted state is NOT in the upstream cache and needs a real
    `git status`, which is too expensive to run on every render. Several
    sessions render concurrently, so the cache is replaced atomically."""
    path = paths.log("git_local_state.json")
    now, cache, out, changed = time.time(), {}, {}, False
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        pass
    for repo in repos:
        ent = cache.get(repo)
        if not (ent and now - ent.get("ts", 0) < REPO_STATE_TTL):
            _a, _d = _local_state(repo)
            ent = {"ts": now, "ahead": _a, "dirty": _d}
            cache[repo], changed = ent, True
        out[repo] = ent
    if changed:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = "%s.tmp%d" % (path, os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp, path)
        except Exception:
            pass
    return out


try:
    from git_upstreams import check_upstream
    _res = check_upstream(cwd) if is_git else None
    try:
        import git_autofetch  # background fetch when the remote moved; never blocks
        git_autofetch.maybe_spawn(_res)
        nofetch = git_autofetch.not_fetched(_res)
    except Exception:
        pass
    if _res and _res.get("upstream"):
        auth_failed = _res.get("error") == "auth-failed"
        if _res.get("verified_behind") is not None:
            behind = _res["verified_behind"]
            verified = True
            if _res.get("diverged"):
                diverged = True
                ahead = _res.get("verified_ahead") or ahead
        elif _res.get("moved_unknown"):
            if behind > 0:
                behind_plus = True
            else:
                behind_unknown = True
    # Display order is FIXED — `my` before `claudette`, whatever fired: a segment
    # whose items move around has to be re-read every time instead of recognised
    # by position, and a version suffix belongs to `claudette`, the last one.
    # No cwd dedup: when cwd IS one of these repos the branch segment carries the
    # same fact, but dropping the entry would make the segment's shape depend on
    # where the session stands (often the AGENT's cd, not the user's). Saying it
    # twice beats a gap.
    # Flat layout: the two paths are one repo, and that repo IS claudette — one
    # entry, no consent gate (the gate exists for a ~/.claude that may be the
    # user's own repo, unrelated to claudette).
    _watch = []
    if paths.CLAUDE_DIR != paths.ROOT:
        _watch.append(("my", paths.CLAUDE_DIR))
    _watch.append(("claudette", paths.ROOT))
    _states, _probe = {}, []
    for _label, _repo in _watch:
        if _label == "my":
            import prefs
            if not prefs.enabled("my_root_repo_sync"):
                continue  # personal repo is out of claudette's scope unless consented
        _cl = check_upstream(_repo)
        try:
            git_autofetch.maybe_spawn(_cl)
        except Exception:
            pass
        if _cl and _cl.get("error") == "auth-failed":
            _states[_label] = "auth"   # outranks behind/ahead: the sync is BLOCKED
        elif _cl and _cl.get("diverged"):
            _states[_label] = "diverged"
        elif _cl and ((_cl.get("verified_behind") or 0) > 0 or _cl.get("moved_unknown")):
            _states[_label] = "behind"
        else:
            _probe.append((_label, _repo))  # not behind — does it hold local work?
    if _probe:
        _st = _local_states([r for _, r in _probe])
        for _label, _repo in _probe:
            _e = _st.get(_repo) or {}
            if _e.get("ahead") or _e.get("dirty"):
                _states[_label] = "ahead"
    repo_flags = [(l, _states[l]) for l, _ in _watch if l in _states]
except Exception:
    pass

# Upstream segment. Ahead is informational (cyan); behind is a WARNING state:
# red + triangle, since it means the local repo is stale vs the remote.
#   ⚠ ↓N  exact, verified against live remote   ⚠ ↓N+ at least N, remote moved further
#   ⚠ ↓?  remote moved, count unknown (the `?` is YELLOW — the unknown glyph,
#         shared with the usage segment; the arrow keeps the red warning)
#         ↓N   local-only info (remote unreachable)
#   ⚠ ⇅A/B  parted histories after a rewrite — never render this as plain "behind":
#           the fix is reset/force-push, and a pull here merges the abandoned side back
#   ↓? nofetch  auto-fetch is on but this repo is not opted in: the one place the
#         user sees it (a SessionStart hook's output reaches Claude, not the
#         user). Shown only while the remote has moved; the repo opts in with
#         `git_auto_fetch_default: true` in its .claudette(.local).json.
#   ⚠ auth  the remote REFUSED the credential (expired/revoked token). Rendered
#           because the alternative is a clean-looking segment on a checkout
#           that can no longer learn it is stale; `--doctor` names the repo and
#           the fix. Any ↓N shown beside it is local-only, not verified.
if diverged:
    upstream += f"{WARN}{red}⇅{ahead}/{behind}{reset}"
else:
    if ahead > 0:
        upstream += f" {cyan}↑{ahead}{reset}"
    if verified and behind > 0:
        upstream += f"{WARN}{red}↓{behind}{reset}"
    elif behind_plus:
        upstream += f"{WARN}{red}↓{behind}+{reset}"
    elif behind_unknown:
        upstream += f"{WARN}{red}↓{reset}{UNK}"
    if nofetch and (behind_unknown or behind_plus):
        upstream += f" {yellow}nofetch{reset}"
    elif behind > 0:
        upstream += f" {cyan}↓{behind}{reset}"
if auth_failed:
    upstream += f"{WARN}{red}auth{reset}"

# --- What the usage segment SHOWS for a window the payload does not measure
# Claude Code drops a window from `rate_limits` at its reset and sends it again
# only with the session's next API response, so an idle line would read `s:?/?`.
# Once the window's reset is KNOWN to have passed, its real value is 0% until
# someone uses the account again, and a reading of the new window that another
# session on this machine already took is better still. In order: the payload's reading of a live window; else the highest
# reading of the live window this machine holds (this session's previous render,
# `claude_usage_last.json` of the same account), with its reset time; else 0%
# with reset `?` when any known reset of that window has passed; else `?` — no
# evidence, no number. DISPLAY ONLY: the measured variables above
# keep feeding the caps, the write guard and the logs. What was observed:
# CLAUDETTE_DESIGN.md "At a window's reset".
_sid = data.get("session_id", "")
_prev = None   # this session's previous render, logs/sessions/<id>.json
if _sid:
    try:
        with open(os.path.join(paths.USAGE_SESSIONS, _sid + ".json"),
                  encoding="utf-8") as _f:
            _prev = json.load(_f)
    except Exception:
        _prev = None
_machine_rl = {}   # last measured on this machine, same account only
try:
    with open(paths.USAGE_LAST, encoding="utf-8") as _f:
        _m = json.load(_f)
    if isinstance(_m, dict) and _m.get("account") == account_email:
        _machine_rl = _m.get("rate_limits") or {}
except Exception:
    pass
# Every window this session has seen, each as last received. The payload's own
# `rate_limits` loses a window at its reset, so after one redraw the session file
# alone no longer knows that reset; this carries it. Older files: rate_limits.
_rl_seen_prev = {}
if isinstance(_prev, dict):
    _rl_seen_prev = _prev.get("rate_limits_seen")
    if not isinstance(_rl_seen_prev, dict):
        _rl_seen_prev = _prev.get("rate_limits")
    if not isinstance(_rl_seen_prev, dict):
        _rl_seen_prev = {}
_rl_seen = dict(_rl_seen_prev)
for _k, _v in (rl.items() if isinstance(rl, dict) else ()):
    if isinstance(_v, dict) and _num(_v.get("resets_at")) is not None:
        _rl_seen[_k] = _v


def _win(block, window):
    w = block.get(window) if isinstance(block, dict) else None
    if not isinstance(w, dict):
        return None, None
    return _num(w.get("used_percentage")), _num(w.get("resets_at"))


def _shown(window, pct, resets):
    """(pct, resets_at) to display for `window`; None = `?`."""
    now = time.time()
    if pct is not None and (resets is None or resets > now):
        return pct, resets          # measured (reset may be unknown: `s:12%/?`)
    live, passed = [], False
    for p, r in ((pct, resets), _win(_rl_seen_prev, window), _win(_machine_rl, window)):
        if r is None:
            continue
        if r <= now:
            passed = True
        elif p is not None:
            live.append((p, r))
    if live:   # one live window at a time, and usage never falls inside it
        return max(p for p, _ in live), max(r for _, r in live)
    if passed:
        return 0, None
    return None, (resets if resets is not None and resets > now else None)


session_show, session_show_resets = _shown("five_hour", session_pct, _rl("five_hour", "resets_at"))
week_show, week_show_resets = _shown("seven_day", week_pct, _rl("seven_day", "resets_at"))

# Rate limit color helper
def rl_color(val):
    return red if val > 70 else (green if val < 30 else yellow)

session_color = rl_color(session_show) if session_show is not None else ""

# Session time remaining
session_duration = None   # None = reset time not known -> `?`
if session_show_resets:
    now = time.time()
    remaining_min = (session_show_resets - now) / 60
    if remaining_min > 60:
        session_duration = f"{remaining_min / 60:.0f}h"
    elif remaining_min > 0:
        session_duration = f"{int(remaining_min)}m"
    else:
        session_duration = "0m"

# Days until week reset
days_left = None   # None = reset time not known -> `?`
if week_show_resets:
    now = time.time()
    remaining_sec = week_show_resets - now
    days_left = math.ceil(remaining_sec / 86400)
    if days_left < 0:
        days_left = 0

# Week color: projected usage
def week_proj_color(pct, dl):
    denom = 8 - dl
    projected = pct * 7 / denom if denom > 0 else 0
    return red if projected > 100 else (green if projected < 50 else yellow)

week_color = week_proj_color(week_show, days_left or 0) if week_show is not None else ""

# Segment separator
sep = "  "

# --- scoped model limits (e.g. Fable weekly) — NOT in the statusline stdin;
# pulled from the /usage OAuth endpoint by claudette/claude_limits.py, which
# caches to logs/claude_limits_last.json (adaptive TTL, detached refresh — get_limits()
# never blocks the render). Rendered as w:8%(F:15%)/5d only when the account
# has such a limit; dropped if the cache is >1h stale. Color: the pool being
# consumed by THIS session's model gets the rl_color; the idle pool is gray
# (visible-but-dim = "unused capacity you could switch to").
# When the cache reports a failure instead, renders a dim (auth?) hint so a
# never-authorized feature is discoverable.
scoped_limits = None   # normalized list, logged to claude_usage_last.json below
scoped_seg = ""
on_scoped_model = False
try:
    from claude_limits import get_limits
    _lim = get_limits()
    if _lim and _lim.get("scoped") and (time.time() - (_lim.get("ts") or 0) < 3600):
        scoped_limits = _lim["scoped"]
        _parts = []
        for _s in scoped_limits:
            _name = str(_s.get("name") or "?")
            _spct = int(_s.get("pct") or 0)
            # Its week has ended: the cache holds last week's figure until the
            # next refresh lands (seconds, unless it fails) — shown as 0%.
            if isinstance(_s.get("resets_at"), (int, float)) and _s["resets_at"] <= time.time():
                _spct = 0
            _on = _name.lower() in model.lower()
            on_scoped_model = on_scoped_model or _on
            # Same projection coloring as the shared weekly (not absolute
            # rl_color — that's for the 5h session); own reset date if known.
            _dl = days_left or 0   # shared weekly reset may be unknown; scoped has its own
            if _s.get("resets_at"):
                _dl = max(0, math.ceil((_s["resets_at"] - time.time()) / 86400))
            _c = week_proj_color(_spct, _dl) if _on else gray
            _parts.append(f"{gray}{_name[:1].upper()}:{reset}{_c}{_spct}%{reset}")
        scoped_seg = f"{gray}({reset}" + " ".join(_parts) + f"{gray}){reset}"
    elif _lim and not _lim.get("ok") and not os.path.exists(
            paths.log("scoped_hint_off")):
        # Configured-but-broken (usually: Keychain never authorized). A silently
        # missing segment is undiscoverable — users can't ask about a feature they
        # don't know exists. Dim hint instead; `--doctor` names the fix.
        # Suppress for good: touch logs/scoped_hint_off
        scoped_seg = f"{gray}(auth?){reset}"
except Exception:
    pass
# On a scoped model, the shared weekly % is not the pool being consumed: plain.
_week_color = "" if on_scoped_model else week_color

# Stats split: ctx is session-scoped, s/w rate limits are account-global
_c_pct = f"{pct_color}{pct}%" if _ctx_known else UNK
_c_tok = f"{tokenK_color}{tokens_colored}{reset}" if _tokens_known else UNK
ctx_stat = f"c:{_c_pct}{gray}|{_c_tok}{tokens_plain}"
# Prompt-cache minutes left, appended: `(60m)`, grey parentheses, the number red
# under 10 min (so a 5-minute cache is always red), green otherwise. Computed at
# each render only: the line is not redrawn while a session sits idle, so the
# number stays at its value of the last render. Nothing appended before the
# session's first cache write.
# Why the transcript and not the payload: cache_ttl.py.
try:
    import cache_ttl
    _cache = cache_ttl.state(data.get("transcript_path"), (data.get("model") or {}).get("id"))
    if _cache:
        _cache_m = cache_ttl.minutes_left(_cache)
        ctx_stat += f"{gray}({red if _cache_m < 10 else green}{_cache_m}m{gray}){reset}"
except Exception:
    pass
_s_pct = f"{session_color}{session_show}%{reset}" if session_show is not None else UNK
_w_pct = f"{_week_color}{week_show}%{reset}" if week_show is not None else UNK
_s_left = session_duration if session_duration else UNK
_w_left = f"{days_left}d" if days_left is not None else UNK
glob_stat = f"s:{_s_pct}/{_s_left}{sep}w:{_w_pct}{scoped_seg}/{_w_left}"

# --- named segments (contract §12). Everything above is ALWAYS computed —
# segment configuration affects RENDERING ONLY, never the snapshot below,
# which the throttle, the watchdog and the done report all consume.
git_seg = ""
if branch:
    git_content = branch
    if upstream:
        git_content += f"{reset}{upstream}"
    if git_suffix:
        git_content += git_suffix
    # Parens (and branch name) turn red when stale vs remote; magenta otherwise.
    repo_stale = (verified and behind > 0) or behind_unknown or behind_plus or auth_failed
    paren_color = red if repo_stale else magenta
    git_seg = f"{paren_color}({git_content}{paren_color}){reset}"
elif not is_git:
    git_seg = f"{magenta}(no git){reset}"

# `auth` reuses WARN, the git segment's triangle, rather than a bare U+26A0:
# without the U+FE0F emoji-presentation selector the glyph renders text-narrow
# and is unreadable at a glance next to the label. The two segments must warn
# with the SAME triangle, one glyph rather than two weights of one.
_ARROWS = {"behind": "↓", "ahead": "↑", "diverged": "⇅", "auth": WARN}
_repo_parts = [f"{red if _s in ('diverged', 'auth') else orchid}{_ARROWS[_s]}{orchid}{_l}{reset}"
               for _l, _s in repo_flags]
repos_seg = (f"{orchid}({reset}" + " ".join(_repo_parts) + f"{orchid}){reset}"
             if _repo_parts else "")

model_display = f"{model} {effort_level}" if effort_level else model
_home = os.path.expanduser("~")
_full = (("~" + cwd[len(_home):]) if cwd.startswith(_home) else cwd).replace("\\", "/")
_stems = [p for p in cwd.replace("\\", "/").split("/") if p]
# One reading for every clock segment. A fixed table, not `%a`: that follows
# the locale (`sam.` on a French one) and the rest of the line is English.
_lt = time.localtime()
_DOW = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
segments = {
    "folder_stem": f"/{dir_name}",
    "folder_2stems": "/" + "/".join(_stems[-2:]),
    "folder_full": _full,
    "git": git_seg,
    "model": f"{cyan}{model_display}{reset}",
    "context": ctx_stat,
    "usage": glob_stat,
    # Claudette's own two repos: what needs syncing, in claudette's own colour.
    #   (↑my)             the personal ~/.claude repo holds local work
    #   (↓claudette)      the plugin clone is behind its remote
    #   (↑my ↓claudette)  both — order fixed, `my` always first
    # Magenta, NOT red: red belongs to the repo you are working IN (the git
    # segment). This is housekeeping — for anyone but the author claudette is
    # read-only, so "behind" is the normal state between upstream pushes, and a
    # warning there would be a false alarm nearly every time it fired.
    # Two exceptions are red, both because `/claudette update` cannot fix them:
    # ⇅ parted histories, where the obvious action (`/claudette pull`) is the
    # WRONG one (the fix is reset/force-push), and ⚠ a refused credential —
    # an expired token means "behind" can no longer be observed at all.
    # Empty (thus dropped) when both repos are clean and current.
    "repos": repos_seg,
    # Alias of `repos` so an existing statusline_segments list naming
    # `old_claudette` keeps rendering. The order is de-duplicated below, so
    # naming both never renders the segment twice.
    "old_claudette": repos_seg,
    "clock_hms": f"{gray}{time.strftime('%H:%M:%S', _lt)}{reset}",
    # Weekday in front: Claude Code redraws only on events, so a line can sit
    # unchanged for days and a bare `07:00:04` reads as this morning's.
    "clock_dhms": f"{gray}{_DOW[_lt.tm_wday]} {time.strftime('%H:%M:%S', _lt)}{reset}",
    "clock_ymdhms": f"{gray}{time.strftime('%Y-%m-%d %H:%M:%S', _lt)}{reset}",
    "claude_version": f"{gray}v{ver}{reset}" if ver else "",
    "account": f"{gray}{account_email}{reset}" if account_email else "",
}
_DEFAULT_ORDER = ["folder_stem", "git", "model", "context", "usage",
                  "repos", "clock_hms", "claude_version", "account"]
_SEG_ALIASES = {"old_claudette": "repos"}   # old name -> current segment
try:
    import prefs
    _order = prefs.param("statusline_segments", _DEFAULT_ORDER) or _DEFAULT_ORDER
    _custom = str(prefs.param("statusline_custom", "") or "")
    sep = str(prefs.param("statusline_separator", sep))
except Exception:
    _order, _custom = _DEFAULT_ORDER, ""
# Resolve renamed segments, then drop repeats — a configured order naming both
# an old name and its current one must still render the segment once.
_seen = set()
_order = [n for n in (_SEG_ALIASES.get(x, x) for x in _order)
          if not (n in _seen or _seen.add(n))]
output = sep.join(segments[n] for n in _order if segments.get(n))

# Canonical split lines for the snapshot — composed from fixed segments, NOT
# the user's order, because line_session/line_global are read by other features.
session_part = sep.join(x for x in (segments["folder_stem"], segments["git"],
                                    segments["model"], segments["context"],
                                    segments["claude_version"]) if x)

# statusline_custom (contract §12): the user's wrapper script, imported
# IN-PROCESS (a subprocess per render would cost more than the whole line),
# called with one dict, replacing the rendered line only when it returns a
# non-empty string. Any exception falls back to our line.
if _custom:
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("claudette_statusline_custom",
                                             os.path.expanduser(_custom))
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _res = _mod.get_statusline({
            "segments": segments, "order": list(_order), "separator": sep,
            "line": output, "data": data, "scoped_limits": scoped_limits,
        })
        if isinstance(_res, str) and _res.strip():
            output = _res
    except Exception:
        pass

# --- usage logging (see ~/.claude/CLAUDE.md + CLAUDETTE_LOGS.md) ---
# Single data source (this status line) => single writer. claude_usage_last.json is
# atomically overwritten on each render that MEASURED a window (see the guard on the
# write below — an unmeasured render must not erase what another one measured).
# Individual files in logs/claude_usage_snapshots/ log every render that saw something new for
# its session (see _saw_new below); growth policy (keep/drop/summarize) is TODO per
# CLAUDETTE_LOGS.md.
# The block runs after the render so the plain-text line can be logged too.
try:
    import re as _re
    _logs = paths.LOGS
    _status = paths.USAGE_SNAPSHOTS
    os.makedirs(_status, exist_ok=True)
    _cost = cost.get("total_cost_usd")
    # A snapshot is a READING: the chart plots it at its `ts` and counts it as
    # that machine working. A render that follows no API call repeats the
    # session's last stdin values, so logging it re-stamps an old number with a
    # new time (typically the first render of a turn after an idle hour, drawn
    # before the turn's first response). So log a render only when this session's cost moved (an API call happened since its previous
    # render), its rate_limits changed, or there is no previous render to
    # compare with — a new session, a null cost, a pruned session file. The
    # comparison uses the session file as read before the render (_prev).
    _saw_new = (not isinstance(_prev, dict) or _cost is None
                or _prev.get("cost_usd") != _cost
                or _prev.get("rate_limits", rl) != rl)
    _snap = json.dumps({
        # --- FROZEN KEYS (read by other agents; additive-only contract:
        #     never rename/remove/restructure these — only add sibling keys) ---
        "ts": int(time.time()),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ctx_pct": pct,
        "rate_limits": rl,
        # --- usage-snapshot Layer A (schema contract in CLAUDETTE_LOGS.md) ---
        "ctx_tokens": {
            "input": _tok["input_tokens"],
            "output": _tok["output_tokens"],
            "cache_creation": _tok["cache_creation_input_tokens"],
            "cache_read": _tok["cache_read_input_tokens"],
            "total": total,
        },
        "ctx_window": window_size,
        "cost_usd": cost.get("total_cost_usd"),  # this-session $ (null if absent)
        # --- throttle (see skills/throttle/THROTTLE.md) ---
        "session_id": data.get("session_id", ""),  # writer of the per-session fields above
        "model": model,  # display name of the writing session's model
        "caps": {  # throttle curve caps: 100-11h (5h) / 100-8d (7d), null if resets unknown
            "five_hour": cap5,
            "seven_day": cap7,
            "h_remaining": round(_h, 2) if _h is not None else None,
            "d_remaining": round(_d, 2) if _d is not None else None,
        },
        # --- scoped model limits, e.g. Fable weekly;
        #     claudette/claude_limits.py cache, null if unavailable/stale) ---
        "scoped_limits": scoped_limits,
        # --- status keyword hook (see hook_status_prompt.py) ---
        "line": _re.sub(r"\x1b\[[0-9;]*m", "", output),  # rendered status line, ANSI stripped
        # --- session/global split (see hook_status_prompt.py) ---
        # global-only segment (s/w rate limits + render time + account whose
        # limits these are) — valid across sessions
        "line_global": _re.sub(r"\x1b\[[0-9;]*m", "", glob_stat) + "  " + time.strftime("%H:%M:%S")
                       + (("  " + account_email) if account_email else ""),
        # --- whose limits these are; _shown() borrows a
        #     reading from this file only for the same account ---
        "account": account_email,
    })
    _cur = paths.USAGE_LAST
    # A render that measured NOTHING must not overwrite a file that did. This is
    # the account-wide "latest usage" every other session reads, and its `ts` is
    # what the throttle's staleness guard and the background-job extrapolation
    # (`used + 10*age_h`) take for the measurement time. A first-render session
    # sends no `rate_limits` at all (see the parse block at the top); writing it
    # would replace real numbers with an empty block stamped with a CURRENT ts,
    # a snapshot that reads as a *fresh zero* no staleness check can see.
    # Holding the file back loses nothing: it carries no session-scoped fact that
    # `logs/sessions/<id>.json` below does not already carry.
    # The snapshot folder still logs the empty first render — that folder is a
    # log of what each render SAW that was new (_saw_new above), this file is the
    # last thing anyone MEASURED.
    # NOT covered: a PARTIAL payload (one window present) passes this guard and
    # overwrites the window it omits; see CLAUDETTE_LOGS.md and the "Still open"
    # list in CLAUDETTE_DESIGN.md.
    if session_pct is not None or week_pct is not None or not os.path.exists(_cur):
        _tmp = _cur + ".tmp"
        with open(_tmp, "w", encoding="utf-8") as _f:
            _f.write(_snap)
        os.replace(_tmp, _cur)  # atomic on Windows + POSIX
    if _saw_new:
        _indiv = os.path.join(_status, "status_%d_%d.json" % (int(time.time() * 1000), os.getpid()))
        with open(_indiv, "w", encoding="utf-8") as _f:
            _f.write(_snap)
    # Per-session snapshot: session-scoped segment only (dir/git/model/ctx/ver),
    # so hook_status_prompt.py can show THIS session's line even when another session
    # overwrote claude_usage_last.json since. Rewritten on EVERY render, so its `ts`
    # stays "last render"; `rate_limits` is what the next render
    # compares with. Pruned after 7 days.
    if _sid:
        _sessions = paths.USAGE_SESSIONS
        os.makedirs(_sessions, exist_ok=True)
        _sess_snap = json.dumps({
            "ts": int(time.time()),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "session_id": _sid,
            "model": model,
            "ctx_pct": pct,
            "cost_usd": cost.get("total_cost_usd"),
            "line_session": _re.sub(r"\x1b\[[0-9;]*m", "", session_part),
            "rate_limits": rl,
            # each window as last received, kept when the
            # payload drops it — what _shown() reads a passed reset from
            "rate_limits_seen": _rl_seen,
        })
        _sess_cur = os.path.join(_sessions, _sid + ".json")
        _sess_tmp = _sess_cur + ".tmp"
        with open(_sess_tmp, "w", encoding="utf-8") as _f:
            _f.write(_sess_snap)
        os.replace(_sess_tmp, _sess_cur)
        _cutoff = time.time() - 7 * 86400
        for _fn in os.listdir(_sessions):
            _fp = os.path.join(_sessions, _fn)
            try:
                if os.path.getmtime(_fp) < _cutoff:
                    os.remove(_fp)
            except OSError:
                pass
except Exception:
    pass

sys.stdout.write(output)
