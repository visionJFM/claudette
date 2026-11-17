"""Stdlib-only tone synthesis, run at APPLY time — never in a hook.

The synthesis runs once per parameter change, here, into `generated/<feature>.wav`;
the hook that plays it is hook_play_wav.py. Waveform: per-note sine with an 8 ms
fade, 25% max amplitude, 50 ms of tail silence.

LEAD-IN SILENCE (`audio_lead_ms`, global param). Wireless headphones power their
receiver down when idle, so the first notes of a short cue are never heard. Silence
is real PCM: it opens the stream and is clocked out like any other samples, so the
wake-up window is spent on samples nobody needs to hear. The price is the same
latency on EVERY cue, wired or not, hence a parameter, tunable by ear:

    features_apply.py --param audio_lead_ms=NN --preview-tone <feature>

SYNTH_VERSION participates in the input hash features_apply.py keeps in
generated/tones.json — bump it when the waveform math changes so every
machine regenerates.
"""
import array
import math
import wave

SYNTH_VERSION = 2
SR = 44100
NOTE_NAMES = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def note_to_freq(note):
    name = note[:-1]
    octave = int(note[-1])
    semitone = NOTE_NAMES[name[0].upper()]
    if len(name) > 1:
        if name[1] == "#":
            semitone += 1
        elif name[1] == "b":
            semitone -= 1
    midi = (octave + 1) * 12 + semitone
    return 440 * 2 ** ((midi - 69) / 12)


def synth_wav(path, notes_csv, total_ms, volume, lead_ms=0):
    """Write a 16-bit mono wav of the note sequence. `total_ms` is the total
    across the whole sequence, split evenly per note (dur_ms = total_ms // len(notes)).

    `lead_ms` of silence goes BEFORE the first note and is not part of
    `total_ms`: changing the lead never shortens the cue."""
    notes = [n.strip() for n in str(notes_csv).split(",") if n.strip()]
    if not notes:
        notes = ["A4"]
    dur_ms = max(1, int(total_ms) // len(notes))
    gain = max(0, min(100, int(volume))) / 100 * 0.25
    fade = int(SR * 0.008)
    samples = array.array("h")
    samples.extend([0] * int(SR * max(0, int(lead_ms)) / 1000))
    for note in notes:
        freq = note_to_freq(note)
        n = int(SR * dur_ms / 1000)
        for i in range(n):
            v = math.sin(2 * math.pi * freq * i / SR)
            w = 1.0
            if i < fade:
                w = i / fade
            elif i > n - fade:
                w = (n - i) / fade
            samples.append(int(v * w * gain * 32767))
    samples.extend([0] * int(SR * 0.05))
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(samples.tobytes())
