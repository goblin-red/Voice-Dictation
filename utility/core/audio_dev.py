"""Какие микрофон и динамики выбраны в macOS сейчас (CoreAudio).

PortAudio (sounddevice) запоминает устройства при запуске: сменили микрофон в системе (Camo -> встроенный) —
поток продолжает слушать старый. По смене номера устройства Listener переоткрывает поток.
"""

import ctypes
import struct

_CA = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


def _fourcc(code):
    return struct.unpack(">I", code.encode())[0]


SYSTEM_OBJECT = 1
DEFAULT_INPUT = _fourcc("dIn ")
DEFAULT_OUTPUT = _fourcc("dOut")
SCOPE_GLOBAL = _fourcc("glob")


def _default(selector):
    addr = _Address(selector, SCOPE_GLOBAL, 0)
    dev = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(dev))
    err = _CA.AudioObjectGetPropertyData(SYSTEM_OBJECT, ctypes.byref(addr), 0, None,
                                         ctypes.byref(size), ctypes.byref(dev))
    return dev.value if err == 0 else None


_CF = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
_CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
_CF.CFRelease.argtypes = [ctypes.c_void_p]
NAME = _fourcc("lnam")
UTF8 = 0x08000100


def _name(dev):
    """Название устройства: «Микрофон MacBook Pro», «JBL GO 2»."""
    if not dev:
        return ""
    addr = _Address(NAME, SCOPE_GLOBAL, 0)
    ref = ctypes.c_void_p()
    size = ctypes.c_uint32(ctypes.sizeof(ref))
    if _CA.AudioObjectGetPropertyData(dev, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(ref)) or not ref.value:
        return ""
    buf = ctypes.create_string_buffer(512)
    ok = _CF.CFStringGetCString(ref, buf, 512, UTF8)
    _CF.CFRelease(ref)
    return buf.value.decode("utf-8", "replace") if ok else ""


def names():
    """(микрофон, динамики) по умолчанию — названиями."""
    try:
        return _name(_default(DEFAULT_INPUT)), _name(_default(DEFAULT_OUTPUT))
    except Exception:
        return "", ""


def defaults():
    """(номер микрофона, номер динамиков) по умолчанию; None — не удалось узнать."""
    try:
        return _default(DEFAULT_INPUT), _default(DEFAULT_OUTPUT)
    except Exception:
        return None


DEVICE_LIST = _fourcc("dev#")       # kAudioHardwarePropertyDevices


def device_ids():
    """Номера всех звуковых устройств CoreAudio (кортеж) — дёшево, без PortAudio; None — не удалось узнать.
    Список изменился — что-то подключили или отключили (тогда Listener перечитывает PortAudio)."""
    try:
        addr = _Address(DEVICE_LIST, SCOPE_GLOBAL, 0)
        size = ctypes.c_uint32(0)
        if _CA.AudioObjectGetPropertyDataSize(SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size)):
            return None
        items = (ctypes.c_uint32 * (size.value // 4))()
        if _CA.AudioObjectGetPropertyData(SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size), items):
            return None
        return tuple(sorted(items[: size.value // 4]))
    except Exception:
        return None


# ---------- кто сейчас выводит звук в динамики (macOS 14.4+: процессы CoreAudio)

PROCESS_LIST = _fourcc("prs#")      # kAudioHardwarePropertyProcessObjectList
PROCESS_PID = _fourcc("ppid")       # kAudioProcessPropertyPID
RUNNING_OUTPUT = _fourcc("piro")    # kAudioProcessPropertyIsRunningOutput
RUNNING_SOMEWHERE = _fourcc("gone") # kAudioDevicePropertyDeviceIsRunningSomewhere


def _u32(obj, selector):
    addr = _Address(selector, SCOPE_GLOBAL, 0)
    val = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(val))
    err = _CA.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(val))
    return val.value if err == 0 else None


def _processes():
    """Аудио-объекты процессов; None — macOS старше 14.4."""
    addr = _Address(PROCESS_LIST, SCOPE_GLOBAL, 0)
    size = ctypes.c_uint32(0)
    if _CA.AudioObjectGetPropertyDataSize(SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size)):
        return None
    items = (ctypes.c_uint32 * (size.value // 4))()
    if _CA.AudioObjectGetPropertyData(SYSTEM_OBJECT, ctypes.byref(addr), 0, None, ctypes.byref(size), items):
        return None
    return list(items)


def playing(skip=()):
    """Какой-то процесс (кроме процессов skip) сейчас выводит звук — динамики заняты.

    Без списка процессов (старая macOS) — занято ли устройство вывода вообще.
    """
    try:
        procs = _processes()
        if procs is None:
            out = _default(DEFAULT_OUTPUT)
            return bool(out and _u32(out, RUNNING_SOMEWHERE))
        return any(_u32(p, RUNNING_OUTPUT) and _u32(p, PROCESS_PID) not in skip for p in procs)
    except Exception:
        return False


# ---------- куда идёт звук: наушники или динамики (блок «Звук», эхо «авто»)

TRANSPORT = _fourcc("tran")         # kAudioDevicePropertyTransportType
DATA_SOURCE = _fourcc("ssrc")       # kAudioDevicePropertyDataSource (у встроенного выхода: динамики / наушники)
STREAMS = _fourcc("stm#")           # kAudioDevicePropertyStreams
SCOPE_OUTPUT = _fourcc("outp")
# тип подключения устройства (kAudioDeviceTransportType…) → понятное слово
TRANSPORTS = {_fourcc("bltn"): "builtin", _fourcc("blue"): "bluetooth", _fourcc("blea"): "bluetooth",
              _fourcc("usb "): "usb", _fourcc("hdmi"): "hdmi", _fourcc("dprt"): "displayport",
              _fourcc("airp"): "airplay", _fourcc("virt"): "virtual", _fourcc("grup"): "aggregate",
              _fourcc("thun"): "thunderbolt", _fourcc("pci "): "pci", _fourcc("1394"): "firewire",
              _fourcc("eavb"): "avb"}
SRC_SPEAKER = _fourcc("ispk")       # встроенные динамики
SRC_HEADPHONES = _fourcc("hdpn")    # гнездо наушников встроенного выхода


def _u32_scope(obj, selector, scope):
    addr = _Address(selector, scope, 0)
    val = ctypes.c_uint32(0)
    size = ctypes.c_uint32(ctypes.sizeof(val))
    err = _CA.AudioObjectGetPropertyData(obj, ctypes.byref(addr), 0, None, ctypes.byref(size), ctypes.byref(val))
    return val.value if err == 0 else None


def _has_output(dev):
    addr = _Address(STREAMS, SCOPE_OUTPUT, 0)
    size = ctypes.c_uint32(0)
    if _CA.AudioObjectGetPropertyDataSize(dev, ctypes.byref(addr), 0, None, ctypes.byref(size)):
        return False
    return size.value > 0


def output_info(name=None):
    """Выход звука: (название, тип подключения, источник встроенного выхода).
    name — выбранный вручную выход (None — как в macOS). Тип: builtin / bluetooth / usb / hdmi / airplay / … / "";
    источник — "speaker" / "headphones" / "" (только у встроенного). Не удалось узнать — ("", "", "")."""
    try:
        dev = None
        if name:
            for d in device_ids() or ():
                if _name(d) == name and _has_output(d):
                    dev = d
                    break
        if name and dev is None:
            return "", "", ""  # выбранного выхода нет; системный вместо него не подставляем
        if dev is None:
            dev = _default(DEFAULT_OUTPUT)
        if not dev:
            return "", "", ""
        kind = TRANSPORTS.get(_u32(dev, TRANSPORT), "")
        src = _u32_scope(dev, DATA_SOURCE, SCOPE_OUTPUT)
        source = "speaker" if src == SRC_SPEAKER else "headphones" if src == SRC_HEADPHONES else ""
        return _name(dev), kind, source
    except Exception:
        return "", "", ""
