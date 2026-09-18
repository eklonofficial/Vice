"""Win32 glue, through ctypes so Windows needs no compiled dependency.

Imported only on Windows, and only from the functions that need it, so Linux
never loads ctypes.windll. Every function here degrades to "could not" (None,
False, an empty list) instead of raising: the callers are desktop niceties,
and none of them is worth taking the daemon down over.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Optional

log = logging.getLogger("vice.win32")

if sys.platform != "win32":  # pragma: no cover - guarded by every caller
    raise ImportError("vice.win32 is only available on Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)

HGLOBAL = wintypes.HANDLE
LRESULT = ctypes.c_ssize_t

# ── Prototypes ───────────────────────────────────────────────────────────────
# Declared explicitly: ctypes assumes int for every return value, which
# truncates handles and pointers on 64-bit Windows.

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = HGLOBAL
kernel32.GlobalLock.argtypes = [HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalFree.argtypes = [HGLOBAL]
kernel32.GlobalFree.restype = HGLOBAL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
user32.MessageBoxW.restype = ctypes.c_int
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HMONITOR

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL

ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.restype = None


# ── Known folders ────────────────────────────────────────────────────────────

class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def parse(cls, text: str) -> "GUID":
        hexes = text.strip("{}").replace("-", "")
        data4 = bytes.fromhex(hexes[16:])
        return cls(
            int(hexes[0:8], 16), int(hexes[8:12], 16), int(hexes[12:16], 16),
            (ctypes.c_ubyte * 8)(*data4),
        )


# Resolved through the shell rather than joined onto the home directory, so a
# Videos folder that OneDrive or the user has moved is still found.
_KNOWN_FOLDERS = {
    "videos": "{18989B1D-99B5-455B-841C-AB7C74E4DDFC}",
    "pictures": "{33E28130-4E1E-4676-835A-98395C3BC3BB}",
}

shell32.SHGetKnownFolderPath.argtypes = [
    ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p),
]
shell32.SHGetKnownFolderPath.restype = ctypes.HRESULT


def known_folder(name: str) -> Optional[Path]:
    guid_text = _KNOWN_FOLDERS.get(name)
    if not guid_text:
        return None
    out = ctypes.c_wchar_p()
    try:
        shell32.SHGetKnownFolderPath(ctypes.byref(GUID.parse(guid_text)), 0, None, ctypes.byref(out))
    except OSError as exc:
        log.debug("SHGetKnownFolderPath(%s) failed: %s", name, exc)
        return None
    try:
        return Path(out.value) if out.value else None
    finally:
        ole32.CoTaskMemFree(out)


# ── Clipboard ────────────────────────────────────────────────────────────────

CF_UNICODETEXT = 13
CF_HDROP = 15
CF_DIB = 8
GMEM_MOVEABLE = 0x0002


def _global_from_bytes(data: bytes):
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        raise OSError("GlobalAlloc failed")
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        raise OSError("GlobalLock failed")
    try:
        ctypes.memmove(ptr, data, len(data))
    finally:
        kernel32.GlobalUnlock(handle)
    return handle


def _set_clipboard(entries: list[tuple[int, bytes]]) -> bool:
    """Replace the clipboard with every (format, bytes) pair at once.

    The clipboard is a shared lock another app may be holding for a moment,
    so opening it is retried briefly before giving up.
    """
    import time

    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.02)
    else:
        log.debug("Could not open the clipboard: error %d", ctypes.get_last_error())
        return False
    try:
        user32.EmptyClipboard()
        for fmt, data in entries:
            handle = _global_from_bytes(data)
            # On success the clipboard owns the memory; only free on failure.
            if not user32.SetClipboardData(fmt, handle):
                kernel32.GlobalFree(handle)
                log.debug("SetClipboardData(%d) failed: error %d", fmt, ctypes.get_last_error())
                return False
        return True
    except OSError as exc:
        log.debug("Clipboard write failed: %s", exc)
        return False
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> bool:
    return _set_clipboard([(CF_UNICODETEXT, (text or "").encode("utf-16-le") + b"\0\0")])


def hdrop_payload(paths: list[str]) -> bytes:
    """A DROPFILES block: a 20-byte header, then NUL-separated UTF-16 paths
    ending in a double NUL. This is what Explorer puts on the clipboard for a
    copied file, and what Discord and browsers read on paste."""
    header = (20).to_bytes(4, "little")      # pFiles: offset of the list
    header += (0).to_bytes(4, "little") * 2  # pt
    header += (0).to_bytes(4, "little")      # fNC
    header += (1).to_bytes(4, "little")      # fWide
    body = "".join(str(p) + "\0" for p in paths) + "\0"
    return header + body.encode("utf-16-le")


def set_clipboard_files(paths: list[str]) -> bool:
    return _set_clipboard([(CF_HDROP, hdrop_payload(paths))])


def set_clipboard_image(png: Optional[bytes], dib: Optional[bytes]) -> bool:
    """Offer the image both ways: "PNG" keeps transparency for apps that read
    it (browsers, Discord), CF_DIB is what everything else understands."""
    entries: list[tuple[int, bytes]] = []
    if dib:
        entries.append((CF_DIB, dib))
    if png:
        fmt = user32.RegisterClipboardFormatW("PNG")
        if fmt:
            entries.append((fmt, png))
    return bool(entries) and _set_clipboard(entries)


# ── Windows and dialogs ──────────────────────────────────────────────────────

MB_OK = 0x0
MB_ICONERROR = 0x10
MB_SETFOREGROUND = 0x10000


def message_box(title: str, text: str) -> None:
    user32.MessageBoxW(None, text, title, MB_OK | MB_ICONERROR | MB_SETFOREGROUND)


SW_RESTORE = 9


def raise_window(title: str) -> bool:
    hwnd = user32.FindWindowW(None, title)
    if not hwnd:
        return False
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    return bool(user32.SetForegroundWindow(hwnd))


def _window_text(hwnd, getter) -> str:
    buf = ctypes.create_unicode_buffer(256)
    getter(hwnd, buf, len(buf))
    return buf.value


def window_pid(hwnd) -> int:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def foreground_window() -> Optional[dict]:
    """{"hwnd", "pid", "class", "title"} for the focused window, or None."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    return {
        "hwnd": hwnd,
        "pid": window_pid(hwnd),
        "class": _window_text(hwnd, user32.GetClassNameW),
        "title": _window_text(hwnd, user32.GetWindowTextW),
    }


def visible_windows(cap: int = 64) -> list[dict]:
    """Visible, titled top-level windows, in Z order."""
    found: list[dict] = []

    @WNDENUMPROC
    def _collect(hwnd, _lparam):
        if len(found) >= cap:
            return False
        if user32.IsWindowVisible(hwnd):
            title = _window_text(hwnd, user32.GetWindowTextW)
            if title:
                found.append({
                    "hwnd": hwnd,
                    "pid": window_pid(hwnd),
                    "class": _window_text(hwnd, user32.GetClassNameW),
                    "title": title,
                })
        return True

    user32.EnumWindows(_collect, 0)
    return found


_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
_dpi_aware = False


def enable_dpi_awareness() -> None:
    """Switch this process to physical pixels.

    Without it Windows scales some coordinates and not others: on a 150%
    panel DXGI reported the panel 1707 wide while the monitor beside it began
    at x=2560, so the pointer could never be matched to a monitor. Once per
    process, and before any window exists, is when this may be called.
    """
    global _dpi_aware
    if _dpi_aware:
        return
    _dpi_aware = True
    try:
        user32.SetProcessDpiAwarenessContext(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    except (AttributeError, OSError) as exc:  # older than Windows 10 1703
        log.debug("Could not enable DPI awareness: %s", exc)


def cursor_pos() -> Optional[tuple[int, int]]:
    enable_dpi_awareness()
    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        return None
    return point.x, point.y


# ── DXGI outputs ─────────────────────────────────────────────────────────────
#
# ddagrab names its capture target by (adapter, output) index in DXGI's own
# enumeration, which is not the order of EnumDisplayMonitors and not the
# "Display 1/2" numbering in Settings. On a hybrid laptop the built-in panel
# hangs off the iGPU and an external monitor may hang off the dGPU, so both
# indices matter. The only way to get them right is to ask DXGI, which means
# COM vtable calls by hand.

class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _DXGI_ADAPTER_DESC1(ctypes.Structure):
    _fields_ = [
        ("Description", wintypes.WCHAR * 128),
        ("VendorId", wintypes.UINT),
        ("DeviceId", wintypes.UINT),
        ("SubSysId", wintypes.UINT),
        ("Revision", wintypes.UINT),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", _LUID),
        ("Flags", wintypes.UINT),
    ]


class _DXGI_OUTPUT_DESC(ctypes.Structure):
    _fields_ = [
        ("DeviceName", wintypes.WCHAR * 32),
        ("DesktopCoordinates", wintypes.RECT),
        ("AttachedToDesktop", wintypes.BOOL),
        ("Rotation", wintypes.UINT),
        ("Monitor", wintypes.HMONITOR),
    ]


_IID_IDXGIFactory1 = "{770aae78-f26f-4dba-a829-253c83d1b387}"
_DXGI_ERROR_NOT_FOUND = 0x887A0002
_DXGI_ADAPTER_FLAG_SOFTWARE = 0x2

# Vtable slots. IUnknown is 0-2, IDXGIObject 3-6.
_SLOT_RELEASE = 2
_SLOT_FACTORY1_ENUMADAPTERS1 = 12
_SLOT_ADAPTER_ENUMOUTPUTS = 7
_SLOT_ADAPTER1_GETDESC1 = 10
_SLOT_OUTPUT_GETDESC = 7


def _vcall(obj: ctypes.c_void_p, slot: int, restype, *args_and_types):
    """Call slot `slot` of a COM object's vtable."""
    argtypes = [ctypes.c_void_p] + [t for t, _ in args_and_types]
    values = [obj] + [v for _, v in args_and_types]
    vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    fn = ctypes.WINFUNCTYPE(restype, *argtypes)(vtable[slot])
    return fn(*values)


def _release(obj: ctypes.c_void_p) -> None:
    if obj:
        _vcall(obj, _SLOT_RELEASE, wintypes.ULONG)


VENDOR_NAMES = {0x10DE: "nvidia", 0x8086: "intel", 0x1002: "amd", 0x1022: "amd"}


def dxgi_outputs() -> list[dict]:
    """Every desktop-attached output, as ddagrab would address it.

    Each entry: adapter (index), output (index within the adapter), vendor
    ("nvidia"/"intel"/"amd"/"other"), gpu (adapter description), device
    (\\\\.\\DISPLAY1), x, y, width, height, primary.
    """
    enable_dpi_awareness()
    try:
        dxgi = ctypes.WinDLL("dxgi")
    except OSError as exc:
        log.debug("dxgi.dll unavailable: %s", exc)
        return []
    dxgi.CreateDXGIFactory1.argtypes = [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    dxgi.CreateDXGIFactory1.restype = ctypes.HRESULT

    factory = ctypes.c_void_p()
    try:
        dxgi.CreateDXGIFactory1(ctypes.byref(GUID.parse(_IID_IDXGIFactory1)), ctypes.byref(factory))
    except OSError as exc:
        log.debug("CreateDXGIFactory1 failed: %s", exc)
        return []

    outputs: list[dict] = []
    try:
        a = 0
        while True:
            adapter = ctypes.c_void_p()
            hr = _vcall(factory, _SLOT_FACTORY1_ENUMADAPTERS1, ctypes.c_long,
                        (wintypes.UINT, a), (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(adapter)))
            if hr & 0xFFFFFFFF == _DXGI_ERROR_NOT_FOUND or hr < 0:
                break
            try:
                desc = _DXGI_ADAPTER_DESC1()
                _vcall(adapter, _SLOT_ADAPTER1_GETDESC1, ctypes.c_long,
                       (ctypes.POINTER(_DXGI_ADAPTER_DESC1), ctypes.byref(desc)))
                if desc.Flags & _DXGI_ADAPTER_FLAG_SOFTWARE:
                    a += 1
                    continue
                o = 0
                while True:
                    output = ctypes.c_void_p()
                    hr = _vcall(adapter, _SLOT_ADAPTER_ENUMOUTPUTS, ctypes.c_long,
                                (wintypes.UINT, o), (ctypes.POINTER(ctypes.c_void_p), ctypes.byref(output)))
                    if hr & 0xFFFFFFFF == _DXGI_ERROR_NOT_FOUND or hr < 0:
                        break
                    try:
                        odesc = _DXGI_OUTPUT_DESC()
                        _vcall(output, _SLOT_OUTPUT_GETDESC, ctypes.c_long,
                               (ctypes.POINTER(_DXGI_OUTPUT_DESC), ctypes.byref(odesc)))
                        if odesc.AttachedToDesktop:
                            r = odesc.DesktopCoordinates
                            outputs.append({
                                "adapter": a,
                                "output": o,
                                "vendor": VENDOR_NAMES.get(desc.VendorId, "other"),
                                "gpu": desc.Description,
                                "device": odesc.DeviceName,
                                "x": r.left,
                                "y": r.top,
                                "width": r.right - r.left,
                                "height": r.bottom - r.top,
                                "primary": r.left == 0 and r.top == 0,
                            })
                    finally:
                        _release(output)
                    o += 1
            finally:
                _release(adapter)
            a += 1
    except OSError as exc:
        log.debug("DXGI enumeration failed: %s", exc)
    finally:
        _release(factory)
    return outputs


def gpu_vendors() -> list[str]:
    """Vendors of every hardware adapter, in DXGI order, for encoder choice."""
    seen: list[str] = []
    for out in dxgi_outputs():
        if out["vendor"] not in seen:
            seen.append(out["vendor"])
    return seen


# ── Login autostart ──────────────────────────────────────────────────────────

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE = "Vice"


def autostart_command() -> Optional[str]:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, AUTOSTART_VALUE)
            return str(value)
    except OSError:
        return None


def set_autostart(command: Optional[str]) -> None:
    """Register `command` to run at login, or remove it when None."""
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
        if command is None:
            try:
                winreg.DeleteValue(key, AUTOSTART_VALUE)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, AUTOSTART_VALUE, 0, winreg.REG_SZ, command)


def pythonw_executable() -> str:
    """pythonw.exe next to the running interpreter, so background launches
    get no console window. Falls back to python.exe if there is none."""
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)

