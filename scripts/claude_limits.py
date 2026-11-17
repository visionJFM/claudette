#!/usr/bin/env python3
"""Cloud usage limits fetcher (scoped model limits, e.g. Fable weekly).

Pulls https://api.anthropic.com/api/oauth/usage — the same private endpoint
Claude Code's /usage screen uses — and caches a normalized snapshot to
logs/claude_limits_last.json. The statusline stdin only carries five_hour/seven_day
buckets; model-scoped weekly limits (Fable) exist ONLY behind this endpoint.

Design (see CLAUDETTE_LOGS.md):
- claude_limits_last.json single writer: the `refresh` CLI run of this script.
- claude_limits_request.json single writer class: get_limits() callers
  (statusline renders) — spawn-guard timestamps, atomic overwrite.
- get_limits() NEVER blocks the caller on the network: it returns the cache
  and, when stale, spawns a detached `refresh` subprocess.
- Adaptive TTL: 300s normally, 60s when any bucket >= 80% or the API reports
  severity != normal (reliability matters most near the limit). Failed
  attempts also back off 60s.
- Credentials are READ-ONLY: file on Windows/Linux, Keychain on macOS.
  NEVER touch the refreshToken — rotation would race Claude Code's own
  refresh and could invalidate the user's session.
- macOS Keychain access can raise a system prompt, which would be confusing
  coming from a detached background process. So it is GATED behind
  logs/keychain_ok.json, written by the interactive `features_apply.py` probe (which
  states the intent first and asks the user to click "Always Allow").
  Background calls use a 3s timeout: a pending prompt reads as a failure
  (fail-soft), never a hung render or a surprise popup.
- Kill switch: create logs/claude_limits_off (empty file) to disable.
- Endpoint is UNDOCUMENTED — treat every field as optional; on any failure
  keep the last good data (stale-if-error) and record the error.
"""

import json, os, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

LOGS = paths.LOGS
CACHE = paths.LIMITS_LAST
MARKER = paths.LIMITS_REQUEST
KEYCHAIN_OK = paths.log("keychain_ok.json")  # written by the features_apply.py probe
OFF = paths.log("claude_limits_off")
URL = "https://api.anthropic.com/api/oauth/usage"

TTL_NORMAL = 300
TTL_HOT = 60          # any bucket >= HOT_PCT, severity != normal, or last attempt failed
HOT_PCT = 80
SPAWN_GUARD = 30      # min seconds between refresh spawns
HTTP_TIMEOUT = 10     # refresh runs detached; never blocks a render


def _atomic_write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj))
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _get_access_token():
    """Return (token, error). File first (Windows/Linux/newer macOS), then
    macOS Keychain — but only after the interactive features_apply.py probe wrote
    KEYCHAIN_OK (see module docstring: no surprise popups from background)."""
    creds = _read_json(os.path.join(paths.CLAUDE_DIR, ".credentials.json"))
    if creds is None and sys.platform == "darwin":
        if not os.path.exists(KEYCHAIN_OK):
            # Name the exact command: only `features_apply.py --auth` runs the probe.
            return None, "keychain not authorized — run features_apply.py --auth"
        try:
            r = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=3)
            if r.returncode == 0 and r.stdout.strip():
                creds = json.loads(r.stdout.strip())
            else:
                return None, "keychain denied/locked (rc=%s) — re-run features_apply.py --auth" % r.returncode
        except subprocess.TimeoutExpired:
            return None, "keychain timeout (ACL prompt pending?) — re-run features_apply.py --auth"
        except Exception as e:
            return None, "keychain: %s" % e
    oauth = (creds or {}).get("claudeAiOauth") or {}
    tok = oauth.get("accessToken")
    if not tok:
        return None, "no credentials"
    exp = oauth.get("expiresAt")  # ms epoch; Claude Code refreshes it while running
    if exp and exp / 1000 < time.time():
        return tok, "token expired (stale until a Claude Code session refreshes it)"
    return tok, None


def _iso_to_epoch(s):
    if not s:
        return None
    try:
        from datetime import datetime
        # Python 3.9 rejects a `Z` suffix; same guard as profile_skill.py / profile_turn.py.
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


_SEV_ORDER = {"normal": 0, "warning": 1, "critical": 2}


def _normalize(raw):
    """Normalized snapshot. FROZEN KEYS (additive-only contract, like
    claude_usage_last.json): ts, iso, ok, scoped[].{name,pct,resets_at}."""
    now = int(time.time())
    session_pct = weekly_pct = None
    scoped = []
    sev_max = "normal"
    for lim in raw.get("limits") or []:
        sev = lim.get("severity") or "normal"
        if _SEV_ORDER.get(sev, 3) > _SEV_ORDER.get(sev_max, 3):
            sev_max = sev
        kind = lim.get("kind")
        if kind == "session":
            session_pct = lim.get("percent")
        elif kind == "weekly_all":
            weekly_pct = lim.get("percent")
        elif kind == "weekly_scoped":
            scope = lim.get("scope") or {}
            name = ((scope.get("model") or {}).get("display_name")
                    or scope.get("surface") or "?")
            scoped.append({
                "name": name,
                "pct": lim.get("percent"),
                "resets_at": _iso_to_epoch(lim.get("resets_at")),
                "severity": sev,
                "active": bool(lim.get("is_active")),
            })
    extra = raw.get("extra_usage") or {}
    return {
        "ts": now,  # last SUCCESSFUL fetch
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "ok": True,
        "error": None,
        "attempt_ts": now,
        "session_pct": session_pct,
        "weekly_pct": weekly_pct,
        "scoped": scoped,
        "severity_max": sev_max,
        "spend_percent": (raw.get("spend") or {}).get("percent"),
        "extra_usage": {
            "is_enabled": extra.get("is_enabled"),
            "monthly_limit": extra.get("monthly_limit"),
            "used_credits": extra.get("used_credits"),
            "spend_limit_reached": extra.get("spend_limit_reached"),
        },
        "raw": raw,
    }


def refresh():
    """Fetch + rewrite the cache. On failure keep previous good data,
    stamp attempt_ts/error (stale-if-error)."""
    prev = _read_json(CACHE) or {}

    def fail(msg):
        prev["ok"] = False
        prev["error"] = str(msg)[:300]
        prev["attempt_ts"] = int(time.time())
        _atomic_write(CACHE, prev)
        return 1

    tok, err = _get_access_token()
    if not tok:
        return fail(err)
    import urllib.request
    req = urllib.request.Request(URL, headers={
        "Authorization": "Bearer %s" % tok,
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            raw = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return fail(e)
    try:
        _atomic_write(CACHE, _normalize(raw))
    except Exception as e:
        return fail("normalize: %s" % e)
    return 0


def _ttl(cache):
    if not cache or not cache.get("ok"):
        return TTL_HOT
    pcts = [cache.get("session_pct") or 0, cache.get("weekly_pct") or 0]
    pcts += [s.get("pct") or 0 for s in cache.get("scoped") or []]
    if max(pcts) >= HOT_PCT or (cache.get("severity_max") or "normal") != "normal":
        return TTL_HOT
    return TTL_NORMAL


def _spawn_refresh():
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x08000000  # DETACHED | NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "refresh"], **kwargs)


def get_limits():
    """Non-blocking read for the statusline: return the cached snapshot
    (may be stale or None) and kick off a detached refresh when due."""
    if os.path.exists(OFF):
        return None
    cache = _read_json(CACHE)
    now = time.time()
    last = max((cache or {}).get("attempt_ts") or 0, (cache or {}).get("ts") or 0)
    if now - last > _ttl(cache):
        marker = _read_json(MARKER) or {}
        if now - (marker.get("ts") or 0) > SPAWN_GUARD:
            try:
                _atomic_write(MARKER, {"ts": int(now), "pid": os.getpid()})
                _spawn_refresh()
            except Exception:
                pass
    return cache


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "refresh":
        sys.exit(refresh())
    # default: print current cache (spawning a refresh if stale)
    print(json.dumps(get_limits(), indent=2))
