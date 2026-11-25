"""Временный журнал только мультимедийных кнопок macOS (без перехвата текста)."""
import json
import time
from pathlib import Path
import AppKit
import ctypes
from Foundation import NSDate, NSRunLoop

path = Path(__file__).resolve().parents[1] / 'logs' / 'headset-buttons.jsonl'
names = {0: 'volume_up', 1: 'volume_down', 7: 'mute', 16: 'play_pause',
         17: 'next', 18: 'previous', 19: 'fast_forward', 20: 'rewind'}
def emit(data):
    data['at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(data, ensure_ascii=False) + '\n')
    print(json.dumps(data, ensure_ascii=False), flush=True)

def event(e):
    if e.subtype() != 8:
        return
    data = int(e.data1())
    key = (data >> 16) & 0xffff
    # Не пишем обычные клавиши; неизвестные media-коды сохраняем числом.
    emit({'kind': 'media_button', 'button': names.get(key, 'media_' + str(key)),
          'phase': {10: 'down', 11: 'up'}.get((data >> 8) & 0xff, 'other'),
          'repeat': bool(data & 1), 'source': 'unknown: macOS media event has no hardware identity'})

AppKit.NSApplication.sharedApplication()
ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
ax.AXIsProcessTrusted.restype = ctypes.c_bool
trusted = bool(ax.AXIsProcessTrusted())
monitor = AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(1 << 14, event)
emit({'kind': 'start', 'accessibility_trusted': trusted, 'monitor_created': monitor is not None,
      'duration_seconds': 300})
try:
    until = time.monotonic() + 300
    while time.monotonic() < until:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.2))
finally:
    if monitor is not None:
        AppKit.NSEvent.removeMonitor_(monitor)
    emit({'kind': 'stop'})
