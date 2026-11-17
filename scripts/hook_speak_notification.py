"""Speak a Claude Code notification out loud — zero required dependencies (§10).

macOS: native `say`. Windows: PowerShell System.Speech (SAPI, already installed). Linux: espeak or spd-say if present.
The notification text is NOT ours: it reaches PowerShell through a temp file,
never quoted into a command line (injection surface). Failures degrade
silently and drop logs/speak_degraded.json for --doctor; the marker clears on
the next successful speech.

`audible_speak_rate` is words-per-minute (frozen unit). SAPI takes −10…10 on a
roughly exponential scale (±10 ≈ 3× speed), mapped best-effort so higher
always means faster.

`audio_lead_ms` (the same global param the tones pad themselves with) is spoken
as a leading pause, so wireless headphones that were asleep do not eat the first
word — a SAPI break on Windows, a compensated `[[slnc N]]` on macOS (see
build_say_args). espeak/spd-say have no equivalent worth the risk, so on Linux
the first word can still clip.
"""
import sys
import json
import math
import pathlib
import re
import tempfile
import time
import os
import platform
import shutil
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths
import prefs

DEGRADED = paths.log("speak_degraded.json")


def _mark(reason):
    try:
        os.makedirs(paths.LOGS, exist_ok=True)
        with open(DEGRADED, "w", encoding="utf-8") as fh:
            json.dump({"ts": int(time.time()), "reason": str(reason)[:200]}, fh)
    except Exception:
        pass


def _clear():
    try:
        os.remove(DEGRADED)
    except OSError:
        pass


def wpm_to_sapi(wpm):
    """Words-per-minute -> SAPI's -10..10, clipped. Exponential fit: 180 wpm
    is rate 0 and +-10 is roughly 3x faster/slower."""
    try:
        return max(-10, min(10, round(10 * math.log(max(1, int(wpm)) / 180.0) / math.log(3))))
    except Exception:
        return 0

# `say`'s [[slnc N]] overlaps the engine's own startup instead of preceding it, a
# constant ~280 ms short (measured on one voice). The param's contract is ms of
# REAL pre-roll, so the mac path adds the shortfall back rather than letting
# ear-tuned values become backend-specific.
SAY_SLNC_ONSET_MS = 280


def build_say_args(text, rate, lead_ms):
    """argv for macOS `say`. lead_ms > 0 prepends [[slnc]] — say's embedded
    command syntax, so a text already containing [[ is interpreted by say
    either way; nothing new is escaped here. lead_ms 0 leaves the text
    untouched."""
    if lead_ms > 0:
        text = "[[slnc %d]] " % (int(lead_ms) + SAY_SLNC_ONSET_MS) + text
    return ["say", "-r", str(rate), text]


def speak(text, voice, rate, volume):
    """Speak on whatever backend this OS has. Returns '' on success, an error
    string on failure — the hook turns that into the degraded marker, the
    interactive preview (features_apply.py --preview-speak) prints it instead.
    Off Windows only `rate` (and the global audio_lead_ms) matters: voice and
    volume are SAPI concepts."""
    system = platform.system()
    if system == "Darwin":
        # macOS: native 'say'; rate is words-per-minute natively.
        lead = max(0, int(prefs.param("audio_lead_ms", 500) or 0))
        try:
            subprocess.run(build_say_args(text, rate, lead), timeout=30)
            return ""
        except Exception as e:
            return f"say failed: {e}"

    if system == "Windows":
        return speak_windows(text, voice, rate, volume)

    # Linux: espeak speaks wpm natively; spd-say takes -100..100.
    try:
        if shutil.which("espeak"):
            subprocess.run(["espeak", "-s", str(rate), "-a",
                            str(max(0, min(200, volume * 2))), text], timeout=30)
        elif shutil.which("spd-say"):
            subprocess.run(["spd-say", "-w", text], timeout=30)
        else:
            return "no TTS found (install espeak or speech-dispatcher)"
        return ""
    except Exception as e:
        return f"speech failed: {e}"


def session_label(session_id):
    """Speakable session name, or '' — only for sessions the user renamed.

    Claude Code writes ~/.claude/sessions/<pid>.json per live session; the file
    is keyed by pid, not session id, so we scan (a handful of tiny files).
    Auto-generated names carry nameSource='derived' (cwd-N)
    and are noise out loud; user renames have no nameSource.
    """
    if not session_id:
        return ''
    d = pathlib.Path.home() / '.claude' / 'sessions'
    try:
        files = list(d.glob('*.json'))
    except Exception:
        return ''
    for f in files:
        try:
            s = json.loads(f.read_text())
        except Exception:
            continue
        if s.get('sessionId') != session_id:
            continue
        if s.get('nameSource') == 'derived':
            return ''
        return re.sub(r'[^0-9A-Za-z]+', ' ', s.get('name', '')).strip()
    return ''

def has_real_prompt(transcript_path):
    """True unless the session has never received an actual user prompt.

    /clear starts a fresh session id and a fresh transcript, so the idle-prompt
    notification 60s later announces a session that holds nothing but the
    /clear itself — the user is sitting right there. Same for a session opened
    and left alone. Everything but a genuine prompt is skipped: non-user rows,
    isMeta rows (caveats, system reminders) and slash-command markers, which
    are user rows whose content is a <command-name>/<local-command…> wrapper.

    Any doubt speaks: unreadable file, no path, bad json on the deciding row.
    Missing a notification is worse than one redundant 'idle prompt'.
    """
    if not transcript_path:
        return True
    try:
        with open(transcript_path, encoding='utf-8') as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get('type') != 'user' or row.get('isMeta'):
                    continue
                content = row.get('message', {}).get('content')
                if isinstance(content, str):
                    if content.lstrip().startswith(('<command-name>', '<local-command')):
                        continue
                    if not content.strip():
                        continue
                return True   # real prompt, or tool results implying one
    except Exception:
        return True
    return False

def classify(notification_type):
    """The three consent classes of the audible_speak_* split: permission
    prompts, idle prompts, and `other` as the catch-all for anything new."""
    if 'permission' in notification_type:
        return 'permission'
    if 'idle' in notification_type:
        return 'idle'
    return 'other'

def main():
    # Audio kill switch (I8): suspension is deliberate silence.
    if "audio" in prefs.suspended_zones():
        return
    argv = list(sys.argv[1:])
    only = None
    if '--only' in argv:
        i = argv.index('--only')
        only = argv[i + 1] if i + 1 < len(argv) else None
        del argv[i:i + 2]
    voice  = int(argv[0])   if len(argv) > 0 else 0
    rate   = int(argv[1])   if len(argv) > 1 else 200
    volume = int(argv[2])   if len(argv) > 2 else 100

    try:
        data = json.load(sys.stdin)
    except Exception:
        return

    notification_type = data.get('notification_type', '')
    if not notification_type:
        return

    # One hook entry per audible_speak_* feature; each entry speaks only its
    # own class, so switching a feature off physically removes that class.
    if only and classify(notification_type) != only:
        return

    # Idle only. A permission request happens mid-turn, so its prompt row is
    # already on disk in principle - but if the write lags the notification,
    # suppressing it would silence the one notification that blocks progress.
    if 'idle' in notification_type and not has_real_prompt(data.get('transcript_path', '')):
        return

    # Make it more speakable: replace underscores with spaces
    text = notification_type.replace('_', ' ')

    # Tell sessions apart out loud: "idle prompt, starmap scan"
    label = session_label(data.get('session_id', ''))
    if label:
        text += ', ' + label

    err = speak(text, voice, rate, volume)
    if err:
        _mark(err)
    else:
        _clear()

def speak_windows(text, voice, rate, volume):
    """PowerShell System.Speech — SAPI without any pip module. Text travels via
    a temp file; only integers and tempfile-generated paths enter the command
    string. With audio_device set and sounddevice installed, SAPI renders to a
    wav and playback is routed; otherwise SAPI speaks to the default device.
    Same python.exe mixer slider as the tone hooks — healed first, warned
    about only if that failed. (Unrouted, SAPI speaks as powershell.exe: a
    different slider, not healed here.)
    Returns '' on success, an error string on failure (see speak()).

    `audio_lead_ms` becomes a leading SSML break (see the lead-in rationale in
    tone_synth.py)."""
    try:
        import winaudio
        from hook_play_wav import HEALED
        winaudio.heal(record=HEALED)
        winaudio.warn_if_silenced()
    except Exception:
        pass

    keyword = str(prefs.param("audio_device", "") or "")
    route = False
    if keyword:
        try:
            import sounddevice  # noqa: F401 — routing only exists through it
            route = True
        except Exception:
            route = False  # warned at apply time; default device is the fallback

    txt = tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w", encoding="utf-8")
    txt.write(text)
    txt.close()
    wav_path = ""
    if route:
        wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        wav.close()
        wav_path = wav.name

    ps = [
        "Add-Type -AssemblyName System.Speech",
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
        "$v = @($s.GetInstalledVoices() | Where-Object {$_.Enabled})",
        "if ($v.Count -gt 0) { $s.SelectVoice($v[[math]::Min(%d, $v.Count-1)].VoiceInfo.Name) }" % max(0, voice),
        "$s.Rate = %d" % wpm_to_sapi(rate),
        "$s.Volume = %d" % max(0, min(100, volume)),
        "$t = [IO.File]::ReadAllText('%s', [Text.Encoding]::UTF8)" % txt.name,
    ]
    if wav_path:
        ps.append("$s.SetOutputToWaveFile('%s')" % wav_path)

    lead = max(0, int(prefs.param("audio_lead_ms", 500) or 0))
    if lead:
        # SAPI's Speak() does not parse SSML, so the break goes through
        # PromptBuilder, which emits `<break time=.../>` itself — no hand-built
        # XML, and nothing to escape in text we did not write. Built on the
        # SELECTED voice's culture: a PromptBuilder left to default to the
        # machine culture emits a different xml:lang and SAPI may swap the voice
        # out from under SelectVoice. Any failure falls through to plain Speak —
        # a hook whose whole job is not to miss the notification cannot afford a
        # strict-XML path with no exit.
        ps.append("try { $p = New-Object System.Speech.Synthesis.PromptBuilder($s.Voice.Culture); "
                  "$p.AppendBreak([TimeSpan]::FromMilliseconds(%d)); $p.AppendText($t); "
                  "$s.Speak($p) } catch { $s.Speak($t) }" % lead)
    else:
        ps.append("$s.Speak($t)")
    ps.append("$s.Dispose()")

    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "; ".join(ps)],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return f"PowerShell TTS failed: {(r.stderr or '').strip()[:120]}"
        if wav_path:
            from hook_play_wav import play_via_sounddevice
            play_via_sounddevice(wav_path, keyword)
        return ""
    except Exception as e:
        return f"speech failed: {e}"
    finally:
        os.unlink(txt.name)
        if wav_path:
            try:
                os.unlink(wav_path)
            except OSError:
                pass

if __name__ == '__main__':
    main()
