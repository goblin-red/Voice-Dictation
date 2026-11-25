"""Listen-only CGEvent tap: media events only, never text/ordinary keys."""
import json
import time
from pathlib import Path
import Quartz
from CoreFoundation import CFRunLoopGetCurrent, CFRunLoopAddSource, kCFRunLoopCommonModes, CFRunLoopRunInMode, kCFRunLoopDefaultMode
import AppKit
path = Path(__file__).resolve().parents[1] / 'logs' / 'headset-buttons.jsonl'
names = {0:'volume_up',1:'volume_down',7:'mute',16:'play_pause',17:'next',18:'previous',19:'fast_forward',20:'rewind'}
def emit(data):
    data['at']=time.strftime('%Y-%m-%dT%H:%M:%S%z')
    with path.open('a',encoding='utf-8') as f:f.write(json.dumps(data,ensure_ascii=False)+'\n')
    print(json.dumps(data,ensure_ascii=False),flush=True)
def callback(proxy, kind, event, ref):
    if kind in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
        Quartz.CGEventTapEnable(tap, True)
        return event
    try:
        e=AppKit.NSEvent.eventWithCGEvent_(event)
        if e is not None and e.subtype()==8:
            data=int(e.data1()); key=(data>>16)&65535
            emit({'kind':'media_button','method':'hid_eventtap','button':names.get(key,'media_'+str(key)),
                  'phase':{10:'down',11:'up'}.get((data>>8)&255,'other'),'repeat':bool(data&1),'source':'unknown'})
    except Exception as exc:emit({'kind':'error','error':str(exc)})
    return event
AppKit.NSApplication.sharedApplication()
tap=Quartz.CGEventTapCreate(Quartz.kCGHIDEventTap, Quartz.kCGHeadInsertEventTap, Quartz.kCGEventTapOptionListenOnly, 1<<14, callback, None)
if tap is None:
    emit({'kind':'eventtap_unavailable'});raise SystemExit(1)
source=Quartz.CFMachPortCreateRunLoopSource(None,tap,0)
CFRunLoopAddSource(CFRunLoopGetCurrent(),source,kCFRunLoopCommonModes)
Quartz.CGEventTapEnable(tap,True)
emit({'kind':'eventtap_started','duration_seconds':300})
until=time.monotonic()+300
while time.monotonic()<until:CFRunLoopRunInMode(kCFRunLoopDefaultMode,.2,False)
Quartz.CGEventTapEnable(tap,False)
emit({'kind':'eventtap_stopped'})
