#!/usr/bin/env python3
"""Mask credential-shaped text before claudette shows it or writes it to a log.

One masker for every writer: `.proc` (dot_proc.py) shows command lines with it, and the
run logs (`logs/config_runs/`) are written through it by log_run.sh, pull.sh and
features_apply.py. Why: CLAUDETTE_DESIGN.md "Run logs are masked, and pull failures quote git".

    redact.py --header <section> <arg>...   print a run-log section header, each arg masked
    redact.py --tee <log>                   copy stdin to stdout unchanged, masked to <log>
    redact.py --selftest                    print "ok" when masking works (the callers'
                                            preflight: a filter that dies would SIGPIPE
                                            the command it logs)

A false positive here only hides a value, never stops anything, so the net is wider than
the push guard's (leak_patterns.re), which must stay narrow because its false positive
stops a push. Kept free of claudette imports: it starts in a pipe on every logged command.
"""
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# `key=value` / `key: value` for secret-like keys (a URL's query string, a CLI flag).
_KV = re.compile(r"(?i)((?:token|secret|password|passwd|pwd|api[-_]?key|auth\w*|"
                 r"credential\w*)[\"']?\s*[=:]\s*[\"']?)[^\s\"'&;,]+")
# An HTTP auth scheme followed by its credential.
_SCHEME = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}")
# git's `http.extraHeader=<Name>: <value>`, whatever the header is called: the value runs
# to the end of the argument (or of the line, or to a closing quote).
_EXTRAHEADER = re.compile(r"(?i)(extraheader\s*=\s*[\"']?[^:\"'\s=]+:\s*)[^\"'\r\n]+")
# https://user:secret@host
_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@")


def _leak_re():
    """The push guard's own patterns (one source). Each pattern names a secret by its
    PREFIX and minimum length; the rest of the token goes too, or "***" would sit next
    to the key's tail."""
    pats = []
    try:
        with open(os.path.join(HERE, "leak_patterns.re"), encoding="utf-8") as f:
            pats = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    except OSError:
        pass
    try:
        return re.compile("|".join("(?:%s)[A-Za-z0-9_+/=-]*" % p for p in pats)) if pats else None
    except re.error:
        return None


def mask(s, _cache={}):
    """Header and scheme first: "Authorization: Bearer <tok>" would otherwise lose only
    the word "Bearer" to the key=value rule and keep the token."""
    if "re" not in _cache:
        _cache["re"] = _leak_re()
    s = _EXTRAHEADER.sub(lambda m: m.group(1) + "***", s)
    s = _SCHEME.sub(lambda m: m.group(1) + " ***", s)
    s = _USERINFO.sub(lambda m: m.group(1) + "***@", s)
    s = _KV.sub(lambda m: m.group(1) + "***", s)
    if _cache["re"] is not None:
        s = _cache["re"].sub("***", s)
    return s


def mask_bytes(b):
    """Bytes in, bytes out, never raising on an encoding: surrogateescape round-trips any
    byte that is not UTF-8 (a Windows console code page) unchanged."""
    return mask(b.decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape")


def header(section, args):
    """Masked arg by arg, because joining first loses where a value ends."""
    return "== %s == %s  %s" % (section, time.strftime("%Y-%m-%dT%H:%M:%S"),
                                " ".join(mask(a) for a in args))


def tee(log_path):
    """The command's output reaches stdout byte for byte; the log gets it masked.

    Never exits early while stdin is open: the command on the other side of the pipe
    would die of SIGPIPE, and logging must never be the reason a command did not run."""
    out = sys.stdout.buffer
    try:
        fh = open(log_path, "ab")
    except OSError:
        fh = None
    for raw in iter(sys.stdin.buffer.readline, b""):
        try:
            out.write(raw)
            out.flush()
        except Exception:
            pass
        if fh is None:
            continue
        try:
            line = mask_bytes(raw)
        except Exception:
            line = b"[claudette: a line was not logged, masking failed]\n"
        try:
            fh.write(line)
            fh.flush()
        except Exception:
            pass
    if fh is not None:
        try:
            fh.close()
        except Exception:
            pass
    return 0


def scrub_logs(folder, skip=None, quiet_s=600):
    """Mask run logs written before this masker existed, which may hold a push's command
    line as typed. Idempotent: a masked file masks to itself and is left
    alone, so this costs one read per log (20 at most) on each run that opens one.

    Leaves alone `skip` (this run's own log) and any log written in the last `quiet_s`
    seconds: another run may still be appending to it, and replacing the file would send
    that run's remaining lines to an unlinked file on macOS. A later run scrubs them."""
    try:
        names = [n for n in os.listdir(folder) if n.endswith(".log")]
    except OSError:
        return 0
    n = 0
    now = time.time()
    for name in names:
        path = os.path.join(folder, name)
        try:
            if skip and os.path.normcase(os.path.abspath(path)) == \
                    os.path.normcase(os.path.abspath(skip)):
                continue
            if now - os.path.getmtime(path) < quiet_s:
                continue
            with open(path, "rb") as f:
                raw = f.read()
            masked = b"".join(mask_bytes(ln) for ln in raw.splitlines(True))
            if masked != raw:
                tmp = path + ".tmp%d" % os.getpid()
                with open(tmp, "wb") as f:
                    f.write(masked)
                os.replace(tmp, path)
                n += 1
        except Exception:
            pass
    return n


def main(argv):
    if argv[:1] == ["--selftest"]:
        ok = "abcdefghijklmnop" not in mask("http.extraHeader=Authorization: Bearer abcdefghijklmnop")
        print("ok" if ok else "masking failed")
        return 0 if ok else 1
    if len(argv) >= 2 and argv[0] == "--header":
        # Bytes, not print(): a piped stdout takes the console code page on Windows
        # when no UTF-8 variable is set, and an argument holding "→" would raise.
        sys.stdout.buffer.write((header(argv[1], argv[2:]) + "\n")
                                .encode("utf-8", "surrogateescape"))
        sys.stdout.flush()
        return 0
    if len(argv) == 2 and argv[0] == "--tee":
        return tee(argv[1])
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
