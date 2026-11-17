"""Play a precomputed wav — the runtime half of apply-time tone synthesis (§9).

Zero required dependencies (§10): winsound on Windows, afplay on macOS,
aplay/paplay on Linux — all default-device. `sounddevice` is used ONLY when the
audio_device parameter is set AND the module is installed; device routing is
the one thing the stdlib cannot do, and its absence is warned about once, at
apply time, never here.

A hook must degrade silently: any failure drops logs/audio_degraded.json for
--doctor to surface, and the marker clears on the next successful playback.

A hook must also never hold the turn open: `Stop` hooks block turn end, and a
wedged audio device can outlast even the harness's kill (the child inherits the
hook's stdout, so the harness keeps waiting on the pipe). So the hook half of this
script plays nothing: it re-execs itself detached (own session, stdio on /dev/null)
and exits in milliseconds. The detached child does the playing, keeps the
degraded-marker contract, and self-destructs at HARD_LIMIT so a wedged device
leaks one short-lived orphan instead of a hung session.

`play_wav()` itself stays synchronous — --preview-tone plays a temp file and
deletes it on return, which a detached play would race.
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import prefs

DEGRADED = paths.log("audio_degraded.json")
HEALED = paths.log("audio_mixer_healed.json")

# Flag the parent passes to the child it spawns; not a user-facing option.
CHILD_FLAG = "--play-detached"
# Longest a detached playback may live. Tones are ~0.25-1 s and the subprocess
# players already carry a 30 s timeout, so reaching this means the player (or
# the timeout's own kill) is wedged on the device.
HARD_LIMIT = 45


def _mark(reason):
    try:
        os.makedirs(paths.LOGS, exist_ok=True)
        with open(DEGRADED, "w", encoding="utf-8") as fh:
            json.dump({"ts": int(time.time()), "reason": reason}, fh)
    except Exception:
        pass


def _clear():
    try:
        os.remove(DEGRADED)
    except OSError:
        pass


def play_via_sounddevice(path, device_keyword):
    """Raw-stream playback (bytes, no numpy) on the first output device whose
    name contains the keyword; system default if none matches."""
    import sounddevice as sd
    import wave
    device = None
    for i, d in enumerate(sd.query_devices()):
        if device_keyword.upper() in d["name"].upper() and d["max_output_channels"] > 0:
            device = i
            break
    with wave.open(path, "rb") as wf:
        sr, ch, width = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())
    dtype = {1: "uint8", 2: "int16", 4: "int32"}[width]
    with sd.RawOutputStream(samplerate=sr, channels=ch, dtype=dtype, device=device) as stream:
        stream.write(frames)


def play_wav(path):
    """Best available playback; True on success, marker dropped on failure."""
    keyword = str(prefs.param("audio_device", "") or "")
    if keyword:
        try:
            play_via_sounddevice(path, keyword)
            return True
        except Exception:
            pass  # module missing or device error — fall back to default device
    system = platform.system()
    try:
        if system == "Windows":
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME)
        elif system == "Darwin":
            subprocess.run(["afplay", path], timeout=30, check=True)
        else:
            for player in (["aplay", "-q"], ["paplay"]):
                if shutil.which(player[0]):
                    subprocess.run(player + [path], timeout=30, check=True)
                    break
            else:
                _mark("no wav player found (install alsa-utils or pulseaudio-utils)")
                return False
        return True
    except Exception as e:
        _mark(f"playback failed: {e}")
        return False


def spawn_detached(path):
    """Re-exec this script as a detached child that does the actual playing.

    stdio goes to /dev/null so nothing of ours holds the hook's pipes open, and
    the child leaves our process group so no one waits on it."""
    cmd = [sys.executable, os.path.abspath(__file__), path, CHILD_FLAG]
    kwargs = {"stdin": subprocess.DEVNULL,
              "stdout": subprocess.DEVNULL,
              "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        # 0x00000008 DETACHED_PROCESS | 0x00000200 CREATE_NEW_PROCESS_GROUP
        kwargs["creationflags"] = 0x00000008 | 0x00000200
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kwargs)
        return True
    except Exception as e:
        _mark(f"could not start playback: {e}")
        return False


def play_detached(path):
    """Child side: play, then die — on our own deadline if the device wedges."""
    def expire():
        _mark(f"playback wedged >{HARD_LIMIT}s — audio device busy or absent")
        os._exit(1)
    guard = threading.Timer(HARD_LIMIT, expire)
    guard.daemon = True
    guard.start()
    if play_wav(path):
        _clear()
    guard.cancel()
    # The players can leave non-daemon helper threads behind (sounddevice does);
    # this hook has nothing left to flush, so leave now rather than maybe never.
    os._exit(0)


def main():
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    argv = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not argv:
        return
    if CHILD_FLAG in flags:
        play_detached(argv[0])
        return
    # Audio kill switch (I8): suspension is deliberate silence — no playback,
    # and no degraded marker for the wavs the suspension itself removed.
    if "audio" in prefs.suspended_zones():
        return
    path = argv[0]
    if not os.path.exists(path):
        _mark(f"missing wav {os.path.basename(path)} — run features_apply.py to regenerate")
        return
    # Windows per-app mixer: a zeroed python.exe slider silences every hook
    # sound with no error anywhere (see winaudio.py). Heal it first — raised
    # here, the detached child's new session inherits the stored level — and
    # warn only if that failed.
    try:
        import winaudio
        winaudio.heal(record=HEALED)
        if "--warn-json" in flags:
            # Only where stdout is free (the Stop entry). NEVER on the
            # PermissionRequest entry, which chains `&& echo {...}` on stdout.
            if winaudio.is_silenced():
                print(json.dumps({"systemMessage": winaudio.WARNING}))
        else:
            winaudio.warn_if_silenced()
    except Exception:
        pass
    spawn_detached(path)


if __name__ == "__main__":
    main()
