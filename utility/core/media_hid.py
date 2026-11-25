"""Per-device consumer controls. All IOKit operations run on the media thread.

Seize only dedicated consumer devices, never keyboard/mouse interfaces. No
fallback to the global event tap when a selected device disappears or is busy.
"""
import ctypes as C
import hashlib
import json

P = C.c_void_p
U = C.c_uint32
L = C.c_long
REMOVAL_CALLBACK = C.CFUNCTYPE(None, P, C.c_int32, P)
VALUE_CALLBACK = C.CFUNCTYPE(None, P, C.c_int32, P, P)
USAGES = {0xE9: 'volume_up', 0xEA: 'volume_down', 0xCD: 'play_pause'}


class HidDevices:
    def __init__(self, on_key, log):
        self.log, self.on_key = log, on_key
        self.cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.io = C.CDLL('/System/Library/Frameworks/IOKit.framework/IOKit')
        def bind(lib, name, result, *args):
            f = getattr(lib, name); f.restype = result; f.argtypes = args
        for name, result, args in [
            ('CFRelease', None, [P]), ('CFRetain', P, [P]),
            ('CFStringCreateWithCString', P, [P, C.c_char_p, U]),
            ('CFStringGetCString', C.c_bool, [P, P, L, U]),
            ('CFGetTypeID', C.c_ulong, [P]), ('CFStringGetTypeID', C.c_ulong, []),
            ('CFNumberGetTypeID', C.c_ulong, []), ('CFNumberGetValue', C.c_bool, [P, C.c_int, P]),
            ('CFSetGetCount', L, [P]), ('CFSetGetValues', None, [P, P]),
            ('CFArrayGetCount', L, [P]), ('CFArrayGetValueAtIndex', P, [P, L]),
            ('CFRunLoopGetCurrent', P, [])]:
            bind(self.cf, name, result, *args)
        for name, result, args in [
            ('IOHIDManagerCreate', P, [P, U]), ('IOHIDManagerSetDeviceMatching', None, [P, P]),
            ('IOHIDManagerScheduleWithRunLoop', None, [P, P, P]),
            ('IOHIDManagerUnscheduleFromRunLoop', None, [P, P, P]),
            ('IOHIDManagerCopyDevices', P, [P]),
            ('IOHIDDeviceGetProperty', P, [P, P]), ('IOHIDDeviceGetService', U, [P]),
            ('IORegistryEntryGetRegistryEntryID', C.c_int32, [U, C.POINTER(C.c_uint64)]),
            ('IOHIDDeviceCopyMatchingElements', P, [P, P, U]),
            ('IOHIDElementGetType', U, [P]), ('IOHIDElementGetUsagePage', U, [P]),
            ('IOHIDElementGetUsage', U, [P]), ('IOHIDValueGetElement', P, [P]),
            ('IOHIDValueGetIntegerValue', L, [P]),
            ('IOHIDDeviceOpen', C.c_int32, [P, U]), ('IOHIDDeviceClose', C.c_int32, [P, U]),
            ('IOHIDDeviceRegisterInputValueCallback', None, [P, VALUE_CALLBACK, P]),
            ('IOHIDDeviceRegisterRemovalCallback', None, [P, REMOVAL_CALLBACK, P]),
            ('IOHIDDeviceScheduleWithRunLoop', None, [P, P, P]),
            ('IOHIDDeviceUnscheduleFromRunLoop', None, [P, P, P])]:
            bind(self.io, name, result, *args)
        self.mode = P.in_dll(self.cf, 'kCFRunLoopDefaultMode').value
        self.loop = self.cf.CFRunLoopGetCurrent()
        self.manager = self.io.IOHIDManagerCreate(None, 0)
        if not self.manager:
            raise RuntimeError('IOHIDManagerCreate failed')
        self.handles = {}
        self.active = None
        self.active_id = None
        self.callback = VALUE_CALLBACK(self._value)
        self.removal_callback = REMOVAL_CALLBACK(self._removed)
        self.disconnected = False
        self.io.IOHIDManagerSetDeviceMatching(self.manager, None)
        self.io.IOHIDManagerScheduleWithRunLoop(self.manager, self.loop, self.mode)

    def prop(self, dev, key):
        ref = self.cf.CFStringCreateWithCString(None, key.encode(), 0x08000100)
        try:
            value = self.io.IOHIDDeviceGetProperty(dev, ref)
            if not value: return None
            kind = self.cf.CFGetTypeID(value)
            if kind == self.cf.CFStringGetTypeID():
                buf = C.create_string_buffer(2048)
                if self.cf.CFStringGetCString(value, buf, len(buf), 0x08000100):
                    return buf.value.decode('utf-8', 'replace')
            if kind == self.cf.CFNumberGetTypeID():
                number = C.c_int64()
                if self.cf.CFNumberGetValue(value, 4, C.byref(number)): return number.value
        finally:
            self.cf.CFRelease(ref)
        return None

    def scan(self):
        refs = self.io.IOHIDManagerCopyDevices(self.manager)
        handles, rows = {}, []
        try:
            count = self.cf.CFSetGetCount(refs) if refs else 0
            array = (P * count)()
            if count: self.cf.CFSetGetValues(refs, array)
            for dev in array:
                # Only consumer-control interfaces: seizing a whole keyboard
                # would also swallow ordinary typing.
                if self.prop(dev, 'PrimaryUsagePage') != 12: continue
                elements = self.io.IOHIDDeviceCopyMatchingElements(dev, None, 0)
                keys, unsafe = set(), False
                try:
                    for i in range(self.cf.CFArrayGetCount(elements) if elements else 0):
                        e = self.cf.CFArrayGetValueAtIndex(elements, i)
                        if self.io.IOHIDElementGetType(e) not in (1, 2, 3, 4): continue
                        page, usage = self.io.IOHIDElementGetUsagePage(e), self.io.IOHIDElementGetUsage(e)
                        if page in (1, 7, 9): unsafe = True
                        if page == 12 and usage in USAGES: keys.add(USAGES[usage])
                finally:
                    if elements: self.cf.CFRelease(elements)
                if unsafe or not keys: continue
                identity = [self.prop(dev, k) for k in ('Transport', 'VendorID', 'ProductID', 'LocationID', 'SerialNumber', 'Product')]
                # No serial: the USB port distinguishes identical headsets.
                # Registry ID disambiguates devices with neither port nor serial.
                if not identity[3] and not identity[4]:
                    rid = C.c_uint64()
                    self.io.IORegistryEntryGetRegistryEntryID(self.io.IOHIDDeviceGetService(dev), C.byref(rid))
                    identity.append(rid.value)
                ident = 'hid:' + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:24]
                if ident in handles: continue
                handles[ident] = self.cf.CFRetain(dev)
                rows.append({'id': ident, 'name': identity[5] or 'Headset', 'transport': identity[0] or '',
                             'buttons': sorted(keys)})
        finally:
            if refs: self.cf.CFRelease(refs)
        for dev in self.handles.values(): self.cf.CFRelease(dev)
        self.handles = handles
        if self.active_id and self.active_id not in handles:
            self.release()
        return sorted(rows, key=lambda r: (r['name'], r['id']))

    def select(self, ident):
        if self.active_id == ident and self.active: return 'ready'
        self.release()
        dev = self.handles.get(ident)
        if not dev: return 'missing'
        result = self.io.IOHIDDeviceOpen(dev, 1)  # kIOHIDOptionsTypeSeizeDevice
        if result:
            self.log.warning('HID: cannot seize %s: 0x%08x', ident, result & 0xffffffff)
            return 'hid_permission'
        self.disconnected = False
        self.active = self.cf.CFRetain(dev)
        self.active_id = ident
        self.io.IOHIDDeviceRegisterRemovalCallback(dev, self.removal_callback, None)
        self.io.IOHIDDeviceRegisterInputValueCallback(dev, self.callback, None)
        self.io.IOHIDDeviceScheduleWithRunLoop(dev, self.loop, self.mode)
        return 'ready'

    def _removed(self, context, result, sender):
        if sender == self.active:
            self.log.warning('media trace: HID removed device=%s result=%s', self.active_id, result)
            self.disconnected = True

    def _value(self, context, result, sender, value):
        if result or not value or not self.active or self.disconnected or sender != self.active:
            self.log.debug('media trace: HID ignored result=%s value=%s active=%s disconnected=%s sender_matches=%s', result, bool(value), bool(self.active), self.disconnected, sender == self.active)
            return
        try:
            e = self.io.IOHIDValueGetElement(value)
            if self.io.IOHIDElementGetUsagePage(e) != 12: return
            key = USAGES.get(self.io.IOHIDElementGetUsage(e))
            raw = self.io.IOHIDValueGetIntegerValue(value)
            self.log.debug('media trace: HID device=%s usage=%s key=%s value=%s', self.active_id, self.io.IOHIDElementGetUsage(e), key, raw)
            if key: self.on_key(self.active_id, key, bool(raw))
        except Exception:
            self.log.exception('HID input callback')

    def release(self):
        if not self.active: return
        dev, self.active = self.active, None
        self.active_id = None
        self.io.IOHIDDeviceUnscheduleFromRunLoop(dev, self.loop, self.mode)
        self.io.IOHIDDeviceClose(dev, 0)
        self.cf.CFRelease(dev)

    def close(self):
        self.release()
        self.io.IOHIDManagerUnscheduleFromRunLoop(self.manager, self.loop, self.mode)
        for dev in self.handles.values(): self.cf.CFRelease(dev)
        self.handles.clear()
        self.cf.CFRelease(self.manager)
