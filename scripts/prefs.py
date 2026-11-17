#!/usr/bin/env python3
"""Feature registry + per-machine preferences.

Two files, one tracked and one not:
  `features_def.json`  ROOT, tracked   — what claudette CAN do: every feature's
                       id, group, description, recommended setting, dependencies,
                       tunable parameters, and the config payloads it owns.
  `prefs.local.json`   ROOT, untracked — what THIS machine has agreed to, in two
                       sections: `"features": {"<id>": true|false}` (consent
                       answers) and `"params": {"<name>": value}` (parameter
                       overrides). Written by `/claudette update` and
                       `/claudette prefs`. Legacy flat files (`cft_<id>` +
                       bare params at top level) are folded into the sections
                       on read and rewritten on first write.

**An absent feature key means OFF, and unanswered.** Nothing claudette can do is
applied to a machine before someone has seen its description and said yes; the
update flow lists what is unanswered and asks. `default` in the registry is a
recommendation shown in that list, never an implicit activation. The three
states — on / off / never answered — are what make "recall it on every update"
possible, so do not collapse them.
"""
import json
import os

import paths

DEFS_PATH = os.path.join(paths.ROOT, "features_def.json")
LOCAL_PATH = os.path.join(paths.ROOT, "prefs.local.json")

_cache = {}


def _read(path):
    """Parsed JSON, or {} for any failure. Every caller is a hook or a status
    line: a half-written prefs file must degrade quietly, never crash."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    _cache[path] = (mtime, data)
    return data


def defs():
    return _read(DEFS_PATH)


def local():
    return _read(LOCAL_PATH)


def features():
    """{id: definition} in registry order — the order CLAUDE.md lines and the
    numbered lists are emitted in. A feature with `recommend_by` gets the
    `default` / `recommend_note` of the state its fact is observed in, so every
    reader of the recommendation sees the same one (CLAUDETTE_REGISTRY.md)."""
    feats = defs().get("features") or {}
    if not any(isinstance(f, dict) and f.get("recommend_by") for f in feats.values()):
        return feats
    out = {}
    for fid, f in feats.items():
        rb = f.get("recommend_by") if isinstance(f, dict) else None
        alt = rb.get(observed(rb.get("fact"))) if isinstance(rb, dict) else None
        if isinstance(alt, dict):
            f = dict(f)
            f.update({k: alt[k] for k in ("default", "recommend_note") if k in alt})
        out[fid] = f
    return out


# Facts a `recommend_by` can name: fact -> reader of its observed state. A reader
# only reads what its own writer left in logs/ - never scans, since features()
# runs inside hooks and the status line.
def _idle_compaction_state():
    data = _read(os.path.join(paths.LOGS, "idle_compaction_seen.json"))
    st = data.get("state")
    if st == "seen":
        # Sticky only as long as it is true: a sighting older than the window
        # reads as unseen even if no refresh has rewritten the file since.
        last = data.get("last_seen")
        import time
        if not isinstance(last, (int, float)) or time.time() - last >= 30 * 86400:
            return "unseen"
    return st if st in ("seen", "unseen", "off") else None


_FACTS = {"idle_compaction": _idle_compaction_state}


def observed(fact):
    """The observed state of `fact` ("seen", "unseen", "off"...), or None when
    nothing has been observed yet - the registry's own default then stands."""
    reader = _FACTS.get(fact)
    try:
        return reader() if reader else None
    except Exception:
        return None


def groups():
    return defs().get("groups") or {}


def _fold(data):
    """(feature_answers, param_overrides) from prefs data, folding any legacy
    flat keys (`cft_<id>` toggles + bare params at top level)
    so hooks keep working on a machine that has not run migrate() yet.
    Sectioned values win over a flat duplicate."""
    feats = dict(data.get("features") or {})
    pars = dict(data.get("params") or {})
    for k, v in data.items():
        if k in ("features", "params", "suspended", "deferred"):
            continue
        if k.startswith("cft_"):
            feats.setdefault(k[4:], v)
        else:
            pars.setdefault(k, v)
    return feats, pars


def feature_answers():
    """{id: true|false} — every feature this machine has ever decided about."""
    return _fold(local())[0]


def param_overrides():
    """{name: value} — this machine's parameter overrides only (no defaults)."""
    return _fold(local())[1]


def enabled(feature_id):
    """True only if this machine has explicitly switched the feature on.
    This is the ANSWER; whether the feature currently takes effect is
    effective() — suspension is a separate axis and never edits answers.

    An answer still stored under an id the feature was renamed from counts
    until the next apply moves it (migrate step 4b; a yes wins, as there).
    Hooks and pull.sh run the new code as soon as it is on disk, before any
    apply, and would read a renamed feature as off until then."""
    answers = feature_answers()
    if feature_id in answers:
        return answers[feature_id] is True
    return any(answers.get(old) is True for old in renamed_from(feature_id))


def zone(feature_id):
    """The kill-switch zone covering this feature (registry `zone` field)."""
    return (features().get(feature_id) or {}).get("zone") or ""


def zones():
    """{zone_id: description} — the registry's kill-switch zones."""
    return defs().get("zones") or {}


def suspended_zones():
    """Zones whose effect this machine has suspended (kill switch, I8).
    Stored in prefs.local.json `"suspended"`, NEVER in the answers."""
    got = local().get("suspended") or []
    return [z for z in got if isinstance(z, str)]


def effective(feature_id):
    """Does the feature currently take effect: answered on AND its zone is
    not suspended. Every consumer that produces or gates an artifact
    (assemble, tones, hooks at runtime) asks this, not enabled()."""
    return enabled(feature_id) and zone(feature_id) not in suspended_zones()


def deferred_features():
    """Unanswered features the user has explicitly deferred (`--feat id=defer`)
    — an acknowledgment, not an answer: update still offers them, only the
    doctor's never-answered warning is silenced."""
    got = local().get("deferred") or []
    return [f for f in got if isinstance(f, str)]


def answered(feature_id):
    """Has anyone ever decided about this feature on this machine?"""
    return feature_id in feature_answers()


def available(feature_id):
    """Is the feature's substrate present on this machine (I4)? A feature
    whose substrate is absent is never offered — a decision that could not
    have an effect must not be asked for."""
    sub = (features().get(feature_id) or {}).get("substrate") or ""
    return not sub or os.path.exists(os.path.expanduser(sub))


def unanswered():
    """Ids present in the registry that this machine has never decided about
    AND could act on — the list `/claudette update` presents after a pull.
    Substrate-missing features are excluded outright: not offered, not
    counted, not warned about (I4)."""
    return [fid for fid in features() if not answered(fid) and available(fid)]


def orphans():
    """Answered feature ids that no longer exist in the registry. An id a live
    feature lists in `renamed_from` is not one: it waits for the next apply to
    carry it over; reporting it as an orphan would send the reader to
    _RETIRED_FEATURES, which would drop the answer instead."""
    feats = features()
    pending = {old for fid in feats for old in renamed_from(fid)}
    return [k for k in feature_answers() if k not in feats and k not in pending]


def param_orphans():
    """Parameter overrides whose parameter no longer exists in the registry."""
    return [k for k in param_overrides() if k not in param_defaults()]


def recommended(feature_id):
    return bool((features().get(feature_id) or {}).get("default"))


def recommendation(feature_id):
    """The registry's recommendation as stored: True, False, or None.
    None means NO recommendation — the feature must be answered explicitly
    and --accept-defaults refuses to answer it."""
    return (features().get(feature_id) or {}).get("default")


def renamed_from(feature_id):
    """Old id(s) whose stored answer carries over to this feature — a string
    or list in the registry, always a list here."""
    old = (features().get(feature_id) or {}).get("renamed_from") or []
    return [old] if isinstance(old, str) else list(old)


def missing_requirements(feature_id, pending=None):
    """Ids this feature needs that would NOT be on. `pending` overrides the
    stored state, so the update/prefs flow can validate a whole proposed set
    before writing any of it."""
    state = dict(pending or {})
    out = []
    for req in (features().get(feature_id) or {}).get("requires") or []:
        on = state[req] if req in state else enabled(req)
        if not on:
            out.append(req)
    return out


def param(name, default=None):
    """Machine override, else the owning feature's default, else the global
    default, else `default`."""
    loc = param_overrides()
    if name in loc:
        return loc[name]
    for fdef in features().values():
        p = fdef.get("params") or {}
        if name in p:
            return p[name]
    return (defs().get("params") or {}).get(name, default)


def param_defaults():
    """{name: default} for every parameter in the registry, global and
    per-feature."""
    out = dict(defs().get("params") or {})
    for fdef in features().values():
        out.update(fdef.get("params") or {})
    return out


def write(features=None, params=None, remove_features=(), remove_params=(),
          suspended=None, deferred=None):
    """Patch prefs.local.json (creating it): update/drop feature answers and
    parameter overrides in their sections. Sole programmatic writer of that
    file; any legacy flat keys are folded into the sections on the way through,
    so every write leaves the file in the current shape. Sorted output keeps
    hand-edits and diffs stable. `suspended` (zones) and `deferred` (feature
    ids) replace their lists when given (None keeps them) — both live beside
    the answers, never in them. Answering a feature always clears its
    deferral: the acknowledgment served its purpose."""
    feats, pars = _fold(local())
    feats.update(features or {})
    pars.update(params or {})
    for key in remove_features:
        feats.pop(key, None)
    for key in remove_params:
        pars.pop(key, None)
    susp = suspended_zones() if suspended is None else sorted(set(suspended))
    defs_ = deferred_features() if deferred is None else sorted(set(deferred))
    defs_ = [f for f in defs_ if f not in feats]
    data = {"features": feats, "params": pars}
    if susp:
        data["suspended"] = susp
    if defs_:
        data["deferred"] = defs_
    os.makedirs(os.path.dirname(LOCAL_PATH), exist_ok=True)
    tmp = LOCAL_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, LOCAL_PATH)
    _cache.pop(LOCAL_PATH, None)
    return data
