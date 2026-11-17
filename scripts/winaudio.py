#!/usr/bin/env python3
"""This process's Windows audio-session volume, using stdlib ctypes only.

HAND-ROLLED on purpose: the usual wrapper (pycaw) pulls in comtypes and psutil
(compiled extensions), which cannot be vendored in a repo that syncs to macOS.
Four calls on COM interfaces frozen since Vista are enough, called directly:

    CoCreateInstance(CLSID_MMDeviceEnumerator)
      -> IMMDeviceEnumerator::GetDefaultAudioEndpoint(eRender, eMultimedia)
         (or EnumAudioEndpoints(eRender, ACTIVE) -> Item(i) for heal())
      -> IMMDevice::Activate(IID_IAudioSessionManager)
      -> IAudioSessionManager::GetSimpleAudioVolume(NULL, FALSE)   # OUR session
      -> ISimpleAudioVolume::GetMasterVolume / GetMute / Set*

GetSimpleAudioVolume hands back the calling process's own session directly, so
no pid enumeration (and no psutil) is needed.

WHAT IT DIAGNOSES. Windows keys per-app volume to the executable path (and the
output device), so ONE slider is shared by every python.exe script on the
machine. Zeroed once, it persists across reboots and silences all Python audio.
Nothing errors: attenuation happens after the app hands off the buffer, so
PortAudio's write still succeeds. This module makes that failure visible.

WHY IT WRITES THE SESSION VOLUME. Voice dictation apps with a "mute other audio
while dictating" option snapshot every active output session's volume, set it to
0, and restore the snapshot afterwards; a hook sound whose process has exited by
then has an expired session, the restore skips it, and 0.0 stays stored for
python.exe. Hence heal(): the hooks raise the slider back before every playback.

NOTE ON SESSION LIFETIME. The session exists while this process has an open
audio stream. Called with no stream open, GetSimpleAudioVolume still returns
the stored per-app value (the number we want) but the row will not be visible
in the mixer UI. For a live check, call while a stream is open.

CONTRACT: every public function returns None on any failure and never raises.
A diagnostic must not be able to break the hook it lives in.
"""
import platform

IS_WINDOWS = platform.system() == "Windows"

# Muted, or quieter than this, counts as broken. 0.2 is well below any level
# someone would deliberately choose and well above float noise.
MIN_AUDIBLE = 0.2

WARNING = ("WARNING: Python sound is muted by the Windows mixer — run "
           "'/claudette doctor' to fix (the slider is shared by every "
           "python.exe script on this machine)")

if IS_WINDOWS:
    import ctypes
    from ctypes import POINTER, byref, c_float, c_int, c_void_p, c_ulong

    _ole32 = ctypes.oledll.ole32

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32),
                    ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16),
                    ("Data4", ctypes.c_ubyte * 8)]

    def _guid(s):
        g = _GUID()
        _ole32.CLSIDFromString(ctypes.c_wchar_p(s), byref(g))
        return g

    CLSID_MMDeviceEnumerator = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
    IID_IMMDeviceEnumerator = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
    IID_IAudioSessionManager = "{BFA971F1-4D5E-40BB-935E-967039BFBEE4}"
    IID_ISimpleAudioVolume = "{87CE5498-68D6-44E5-9215-6DA47EF883D8}"

    CLSCTX_ALL = 23
    eRender, eMultimedia = 0, 1
    DEVICE_STATE_ACTIVE = 1
    COINIT_APARTMENTTHREADED = 2
    RPC_E_CHANGED_MODE = -2147417850  # 0x80010106

    # vtable slots (0-2 are IUnknown: QueryInterface/AddRef/Release)
    _RELEASE = 2
    _MMDE_ENUM_ENDPOINTS = 3
    _MMDE_GET_DEFAULT_ENDPOINT = 4
    _COLL_GET_COUNT = 3
    _COLL_ITEM = 4
    _MMDEVICE_ACTIVATE = 3
    _SESSMGR_GET_SIMPLE_VOLUME = 4
    _SAV_SET_MASTER_VOLUME = 3
    _SAV_GET_MASTER_VOLUME = 4
    _SAV_SET_MUTE = 5
    _SAV_GET_MUTE = 6

    def _method(ptr, index, *argtypes):
        """Bind vtable slot `index` of the COM object at `ptr`."""
        vtable = ctypes.cast(ptr, POINTER(POINTER(c_void_p)))[0]
        proto = ctypes.WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)
        return proto(vtable[index])

    def _release(ptr):
        if ptr:
            try:
                _method(ptr, _RELEASE)(ptr)
            except Exception:
                pass


def _device_volume(device, fn):
    """fn(sav_ptr) for this process's session on one IMMDevice. Raises on COM
    failure; the caller decides whether one bad device sinks the whole call."""
    manager = c_void_p()
    volume = c_void_p()
    try:
        _method(device, _MMDEVICE_ACTIVATE,
                POINTER(_GUID), c_ulong, c_void_p, POINTER(c_void_p))(
            device, byref(_guid(IID_IAudioSessionManager)), CLSCTX_ALL, None,
            byref(manager))
        # NULL session guid + FALSE => the calling process's own session.
        _method(manager, _SESSMGR_GET_SIMPLE_VOLUME,
                c_void_p, c_int, POINTER(c_void_p))(
            manager, None, 0, byref(volume))
        return fn(volume)
    finally:
        for ptr in (volume, manager):
            _release(ptr)


def _with_volume(fn, all_devices=False):
    """Run fn(sav_ptr) with an ISimpleAudioVolume for this process's session.

    Default output device only -> fn's result. all_devices=True -> a list of
    fn's results, one per ACTIVE output device (the slider is stored per
    device, and a routed `audio_device` need not be the default); a device
    that fails is skipped, not fatal. None if anything at all goes wrong (not
    Windows, no audio device, COM refused, unexpected vtable). Never raises.
    """
    if not IS_WINDOWS:
        return None
    enumerator = c_void_p()
    device = c_void_p()
    collection = c_void_p()
    initialized = False
    try:
        try:
            _ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
            initialized = True
        except OSError as e:
            # Another library (PortAudio) already initialized COM on this
            # thread with a different model. Harmless — proceed uninitialized.
            if getattr(e, "winerror", None) != RPC_E_CHANGED_MODE:
                return None

        _ole32.CoCreateInstance(byref(_guid(CLSID_MMDeviceEnumerator)), None,
                                CLSCTX_ALL, byref(_guid(IID_IMMDeviceEnumerator)),
                                byref(enumerator))

        if not all_devices:
            _method(enumerator, _MMDE_GET_DEFAULT_ENDPOINT,
                    c_int, c_int, POINTER(c_void_p))(
                enumerator, eRender, eMultimedia, byref(device))
            return _device_volume(device, fn)

        _method(enumerator, _MMDE_ENUM_ENDPOINTS,
                c_int, c_ulong, POINTER(c_void_p))(
            enumerator, eRender, DEVICE_STATE_ACTIVE, byref(collection))
        count = c_ulong()
        _method(collection, _COLL_GET_COUNT, POINTER(c_ulong))(collection, byref(count))
        results = []
        for i in range(count.value):
            item = c_void_p()
            try:
                _method(collection, _COLL_ITEM, c_ulong, POINTER(c_void_p))(
                    collection, i, byref(item))
                results.append(_device_volume(item, fn))
            except Exception:
                pass
            finally:
                _release(item)
        return results
    except Exception:
        return None
    finally:
        for ptr in (collection, device, enumerator):
            _release(ptr)
        if initialized:
            try:
                _ole32.CoUninitialize()
            except Exception:
                pass


def _read_one(sav):
    level = c_float()
    muted = c_int()
    _method(sav, _SAV_GET_MASTER_VOLUME, POINTER(c_float))(sav, byref(level))
    _method(sav, _SAV_GET_MUTE, POINTER(c_int))(sav, byref(muted))
    return {"volume": float(level.value), "muted": bool(muted.value)}


def read():
    """-> {"volume": float 0..1, "muted": bool} for this process, or None."""
    return _with_volume(_read_one)


def read_all():
    """-> [read()-shaped dict per active output device], or None."""
    return _with_volume(_read_one, all_devices=True)


def is_silenced():
    """-> True if this process is muted or near-inaudible, False if fine,
    None if the state could not be read (never assume broken on failure)."""
    state = read()
    if state is None:
        return None
    return state["muted"] or state["volume"] < MIN_AUDIBLE


def _repair_one(sav):
    level = c_float()
    muted = c_int()
    _method(sav, _SAV_GET_MASTER_VOLUME, POINTER(c_float))(sav, byref(level))
    _method(sav, _SAV_GET_MUTE, POINTER(c_int))(sav, byref(muted))
    before = {"volume": float(level.value), "muted": bool(muted.value)}
    if not (before["muted"] or before["volume"] < MIN_AUDIBLE):
        return {"changed": False, "before": before, "after": before}
    _method(sav, _SAV_SET_MUTE, c_int, POINTER(_GUID))(sav, 0, None)
    _method(sav, _SAV_SET_MASTER_VOLUME, c_float, POINTER(_GUID))(sav, 1.0, None)
    _method(sav, _SAV_GET_MASTER_VOLUME, POINTER(c_float))(sav, byref(level))
    _method(sav, _SAV_GET_MUTE, POINTER(c_int))(sav, byref(muted))
    after = {"volume": float(level.value), "muted": bool(muted.value)}
    return {"changed": True, "before": before, "after": after}


def repair():
    """Unmute and raise to full ONLY if currently silenced (default device).

    -> {"changed": bool, "before": {...}, "after": {...}} or None. Deliberately
    conditional: unconditionally forcing 1.0 would silently overrule a
    deliberate choice (anything from MIN_AUDIBLE up is left alone).
    """
    return _with_volume(_repair_one)


def heal(record=None):
    """repair() on every active output device, before a hook plays a sound.

    -> list of the per-device results that changed ([] = nothing needed it),
    or None on failure. With `record` (a path), each healing call bumps a
    small JSON counter there so --doctor can still say it keeps happening —
    the self-heal must not make the cause invisible again. Several sessions
    can heal at once: the counter is best-effort (a lost increment is fine),
    the file itself is replaced atomically so it is never half-written.
    """
    results = _with_volume(_repair_one, all_devices=True)
    if results is None:
        return None
    healed = [r for r in results if r and r.get("changed")]
    if healed and record:
        _note_heal(record, healed)
    return healed


def _note_heal(path, healed):
    import json, os, time
    tmp = "%s.tmp%d" % (path, os.getpid())
    try:
        try:
            with open(path, encoding="utf-8") as fh:
                prev = json.load(fh)
        except Exception:
            prev = {}
        if not isinstance(prev, dict):
            prev = {}
        now = int(time.time())
        rec = {"ts": now, "first_ts": prev.get("first_ts") or now,
               "count": int(prev.get("count") or 0) + 1,
               "devices": len(healed), "before": healed[0]["before"]}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass


def warn_if_silenced(out=None):
    """Print WARNING to stderr if this process is silenced. -> True if warned.

    Cheap enough to call on every hook run (~10-20ms, no heavy imports). Writes
    to stderr, never stdout: hook stdout is a JSON channel, and one caller
    (PermissionRequest) chains `&& echo {...}` — a stray line there would break
    the permission decision. Same reason nothing here ever exits non-zero.
    """
    import sys
    if is_silenced():
        print(WARNING, file=out or sys.stderr)
        return True
    return False


def set_volume(level, muted=None):
    """Set this process's session volume (0..1) and optionally its mute flag.
    Exposed for testing the detector against a known-bad state. -> True/None."""
    def _set(sav):
        if muted is not None:
            _method(sav, _SAV_SET_MUTE, c_int, POINTER(_GUID))(sav, 1 if muted else 0, None)
        _method(sav, _SAV_SET_MASTER_VOLUME, c_float, POINTER(_GUID))(
            sav, max(0.0, min(1.0, float(level))), None)
        return True
    return _with_volume(_set)


if __name__ == "__main__":
    import json, sys
    if len(sys.argv) > 1 and sys.argv[1] == "--repair":
        print(json.dumps(repair()))
    elif len(sys.argv) > 1 and sys.argv[1] == "--heal":
        print(json.dumps(heal()))
    elif len(sys.argv) > 1 and sys.argv[1] == "--read-all":
        print(json.dumps(read_all()))
    elif len(sys.argv) > 2 and sys.argv[1] == "--set":
        print(json.dumps({"ok": set_volume(float(sys.argv[2]),
                                           muted=(len(sys.argv) > 3 and sys.argv[3] == "mute"))}))
    else:
        print(json.dumps(read()))
