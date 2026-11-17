#!/usr/bin/env python3
"""No-prompt hardening for git calls that talk to a remote nobody is watching.

Shared by git_upstreams.py (ls-remote) and git_autofetch.py (fetch): both run
from a status line render or its background child, where a prompt is a hang
and a dialog is a window on a machine nobody is looking at.
"""
import os
import subprocess

# Hardening, passed per invocation and never written to git config.
# GIT_TERMINAL_PROMPT=0 gags the terminal prompt only. With no GIT_ASKPASS and no
# core.askPass, Git for Windows falls through to the bundled git-askpass.exe GUI:
# a dialog nobody sees, which hangs the call until the timeout. An EMPTY
# GIT_ASKPASS closes that door - git skips every askpass helper when the variable
# is set and empty - so the call dies on "terminal prompts disabled" (in AUTH_RE)
# in milliseconds. credential.interactive=false refuses the credential helper's
# own UI without disabling the helper: a cached token still answers.
HARD_ENV = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ASKPASS": "",
    "SSH_ASKPASS": "",
    "SSH_ASKPASS_REQUIRE": "never",
    "GCM_INTERACTIVE": "never",
    "GIT_SSH_COMMAND": "ssh -oBatchMode=yes",
}
HARD_ARGS = ["-c", "credential.interactive=false"]


def kill_tree(proc):
    """Kill git and its children: git-remote-https and the credential helper
    outlive a killed parent and can keep a dialog on screen. Takes the tree."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=5)
        else:
            proc.kill()
    except Exception:
        pass


def run(repo, args, timeout, extra_config=()):
    """(ok, stdout, stderr) of `git -C repo <hardening> <extra_config> <args>`.
    extra_config: flat ["-c", "k=v", ...] placed before the subcommand."""
    env = dict(os.environ)
    env.update(HARD_ENV)
    try:
        # Git writes UTF-8 whatever the locale: decode as UTF-8 (non-ASCII repo
        # path, non-ASCII branch name), errors="replace" so a stray byte costs
        # one character, never the answer (CLAUDETTE_DESIGN.md "Git's output is
        # decoded as UTF-8").
        p = subprocess.Popen(["git", "-C", repo] + HARD_ARGS + list(extra_config) + list(args),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace", env=env)
    except Exception as e:
        return False, "", str(e)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(p)
        try:
            p.communicate(timeout=5)
        except Exception:
            pass
        return False, "", "timed out after %ds (process tree killed)" % timeout
    except Exception as e:
        return False, "", str(e)
    return (p.returncode == 0), (out or "").strip(), (err or "").strip()
