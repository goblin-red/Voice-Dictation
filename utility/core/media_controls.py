"""Назначения мультимедийных клавиш Мака; обычные клавиши не наблюдаем."""
import queue
import threading
import time

BUTTONS = ('volume_up', 'volume_down', 'play_pause', 'volume_up_double', 'volume_down_double', 'play_pause_double', 'volume_up_hold', 'volume_down_hold', 'play_pause_hold', 'volume_up_hold_release', 'volume_down_hold_release', 'play_pause_hold_release')
DEFAULTS = {'enabled': False, 'source': 'all', 'mac_device': 'all', 'remote_devices': {}, 'volume_up': 'start', 'volume_down': 'commands', 'play_pause': 'send',
            'volume_up_hold_release': 'none', 'volume_down_hold_release': 'none', 'play_pause_hold_release': 'none',
            'volume_up_hold': 'none', 'volume_down_hold': 'none', 'play_pause_hold': 'none',
            'volume_up_double': 'next', 'volume_down_double': 'previous', 'play_pause_double': 'send',
            **{key + '_second': 'none' for key in BUTTONS},
            **{key + '_again': 'none' for key in BUTTONS}, 'volume_up_again': 'recognize_send', 'play_pause_second': 'commands', 'play_pause_double_second': 'commands'}

ACTIONS = {'none', 'start', 'commands', 'mic_off', 'hush', 'send', 'recognize_send', 'clear', 'next', 'previous', 'start_send', 'terminal_mode', 'normal_mode', 'extra1_mode', 'extra2_mode'}
# Modes override gestures without changing the saved ordinary assignments.
MODES = {'terminal': {**{key: 'none' for key in BUTTONS}, 'volume_up': 'next', 'volume_down': 'previous', 'play_pause': 'normal_mode'}}

MODES.update({name: {key: 'none' for key in BUTTONS} for name in ('extra1', 'extra2')})
PROFILE_NAMES = ('normal', 'terminal', 'extra1', 'extra2')

SINGLE_KEYS = ('volume_up', 'volume_down', 'play_pause')
REPEAT_KEYS = {key + '_repeat' for key in SINGLE_KEYS}
GAP_KEYS = {key + '_gap_ms' for key in SINGLE_KEYS}

def validate_profile(mapping, complete=False):
    if not isinstance(mapping, dict) or set(mapping) - (set(BUTTONS) | REPEAT_KEYS | GAP_KEYS):
        raise ValueError('Invalid button assignment')
    if complete and not set(BUTTONS).issubset(mapping):
        raise ValueError('Incomplete headset assignments')
    for key, value in mapping.items():
        if key in GAP_KEYS:
            if type(value) is not int or not 350 <= value <= 86400000:
                raise ValueError('The minimum pause must be between 0.35 seconds and 24 hours')
        elif not isinstance(value, list) or any(not isinstance(a, str) or a not in ACTIONS-{'none'} for a in value) or len(value) != len(set(value)):
            raise ValueError('Invalid headset actions')

def action_list(value):
    values = value if isinstance(value, list) else [value]
    return list(dict.fromkeys(v for v in values if isinstance(v, str) and v != 'none'))

KEYS = {0: 'volume_up', 1: 'volume_down', 16: 'play_pause'}

def settings(cfg):
    opts = {**DEFAULTS, **(cfg.get('media_controls') or {})}
    profiles = opts.get('modes') or {}
    opts['modes'] = {name: {**mapping, **profiles.get(name, {})} for name, mapping in MODES.items()}
    saved = opts.get('profiles') or {}
    opts['profiles'] = {}
    for mode in PROFILE_NAMES:
        mapping = opts if mode == 'normal' else opts['modes'][mode]
        profile = {}
        for key in BUTTONS:
            legacy = action_list(mapping.get(key, 'none'))
            if mode == 'normal' and '_hold' not in key:
                legacy += action_list(opts.get(key + '_second', 'none'))
            profile[key] = action_list(saved.get(mode, {}).get(key, legacy))
        for key in SINGLE_KEYS:
            source = saved.get(mode, {})
            profile[key + '_repeat'] = action_list(source.get(key + '_repeat', []))
            profile[key + '_gap_ms'] = source.get(key + '_gap_ms', 1000)
        opts['profiles'][mode] = profile
    return opts

def public_settings(cfg):
    opts = settings(cfg)
    # Old APKs must not execute the legacy shared profile after per-device setup.
    if 'bindings' in opts:
        opts['source'] = 'configured'
    return opts

def validate_bindings(bindings):
    import re
    if not isinstance(bindings, list) or len(bindings) > 32:
        raise ValueError('Invalid headset list')
    ids, pairs = set(), set()
    for item in bindings:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', item['id']):
            raise ValueError('Invalid headset identifier')
        host, device = item.get('host'), item.get('device')
        if host != 'mac' and (not isinstance(host, str) or not re.fullmatch(r'remote:[a-z0-9]{8,24}', host)):
            raise ValueError('Invalid headset device')
        if not isinstance(device, str) or not device or len(device) > 128 or (host == 'mac' and device != 'all' and not re.fullmatch(r'hid:[0-9a-f]{24}', device)):
            raise ValueError('Invalid button source')
        if item['id'] in ids or (host, device) in pairs:
            raise ValueError('The headset is already added')
        ids.add(item['id']);pairs.add((host,device))
        if not isinstance(item.get('enabled'), bool):
            raise ValueError('Invalid headset state')
        profiles = item.get('profiles')
        if not isinstance(profiles, dict) or set(profiles) != set(PROFILE_NAMES):
            raise ValueError('Four headset modes are required')
        for mapping in profiles.values():
            validate_profile(mapping, complete=True)
    for item in bindings:
        if item['enabled'] and item['device'] == 'all' and any(other['enabled'] and other['host']==item['host'] and other['id']!=item['id'] for other in bindings):
            raise ValueError('Choose specific headsets instead of “All” to assign them separately')

def mac_enabled(opts):
    return opts['enabled'] and opts.get('source', 'all') in ('all', 'mac')

class MediaControls:
    def __init__(self, app):
        self.app = app
        self.stop_event = threading.Event()
        self.pending = queue.Queue(maxsize=16)
        self.status = 'off'
        self.devices = []
        self.hid = None
        self.pressed = set()
        self.down_at = {}
        self.held = set()
        self.hold_context = {}
        self.pulse_runs = {}
        self.double_down = set()
        self.clicks = {}
        self.first_press = {}
        self.generation = 0
        self.mode = 'normal'

    def set_mode(self, mode):
        if mode != 'normal' and mode not in MODES:
            raise ValueError('Unknown button mode')
        if self.mode == mode:
            return
        self.first_press.clear()
        self.mode = mode
        self.app.log.info('button mode: %s', self.mode)
        try:
            self.app.play('buttons_' + ('standard' if mode == 'normal' else mode))
        except Exception:
            self.app.log.exception('button-mode change signal')

    def toggle_terminal_mode(self):
        self.set_mode('terminal')

    def start(self):
        threading.Thread(target=self._listen, daemon=True, name='media-keys').start()
        threading.Thread(target=self._dispatch, daemon=True, name='media-actions').start()

    def stop(self):
        self.stop_event.set()

    def _dispatch(self):
        while not self.stop_event.is_set():
            try:
                signature, generation, gesture, queued_at, assigned_action = self.pending.get(timeout=.2)
                self.app.log.debug('media trace: dequeue gesture=%s generation=%s wait_ms=%.1f', gesture, generation, (time.monotonic() - queued_at) * 1000)
            except queue.Empty:
                continue
            if not mac_enabled(signature) or settings(self.app.cfg) != signature or generation != self.generation:
                self.app.log.debug('media trace: discard gesture=%s enabled=%s settings_changed=%s generation=%s current=%s', gesture, signature['enabled'], settings(self.app.cfg) != signature, generation, self.generation)
                continue
            if signature['mac_device'] != 'all' and (
                    not self.hid or self.hid.disconnected or self.hid.active_id != signature['mac_device']):
                self.app.log.debug('media trace: discard gesture=%s device unavailable', gesture)
                continue
            action = action_list(assigned_action) if assigned_action is not None else list(signature['profiles'][self.mode].get(gesture, []))
            profile = signature['profiles'][self.mode]
            if gesture in SINGLE_KEYS and profile.get(gesture + '_repeat'):
                previous = self.first_press.get(gesture)
                if previous and previous[0] == generation:
                    if (queued_at - previous[1]) * 1000 < profile[gesture + '_gap_ms']:
                        continue
                    action = profile[gesture + '_repeat']
                    self.first_press.pop(gesture, None)
                else:
                    self.first_press[gesture] = (generation, queued_at)
            self.app.log.debug('media trace: mode=%s', self.mode)
            self.app.log.debug('media trace: resolve gesture=%s actions=%s', gesture, action)
            try:
                for step in action:
                    if settings(self.app.cfg) != signature or generation != self.generation:
                        self.app.log.debug('media trace: cancel remaining actions gesture=%s settings/generation changed', gesture)
                        break
                    if step != 'none':
                        started = time.monotonic()
                        self.app.log.debug('media trace: action begin gesture=%s action=%s', gesture, step)
                        self.app.media_action(step)
                        self.app.log.debug('media trace: action end gesture=%s action=%s elapsed_ms=%.1f', gesture, step, (time.monotonic() - started) * 1000)
                    else:
                        self.app.log.debug('media trace: ignored gesture=%s action=none', gesture)
            except Exception:
                self.app.log.exception('media button: action failed')

    def _enqueue(self, gesture, assigned_action=None, signature=None, generation=None, at=None):
        try:
            self.pending.put_nowait((settings(self.app.cfg) if signature is None else signature,
                                    self.generation if generation is None else generation,
                                    gesture, time.monotonic() if at is None else at, assigned_action))
            self.app.log.debug('media trace: enqueue gesture=%s generation=%s queue=%s', gesture, self.generation, self.pending.qsize())
        except queue.Full:
            self.generation += 1  # a lost gesture must not leave a stale toggle
            self.app.log.warning('media keys: queue full')

    def _key(self, device, key, down, repeat=False):
        opts = settings(self.app.cfg)
        now = time.monotonic()
        self.app.log.debug('media trace: key device=%s key=%s down=%s repeat=%s pressed=%s mono=%.6f', device, key, down, repeat, key in self.pressed, now)
        if not mac_enabled(opts) or opts['mac_device'] != device:
            self.app.log.debug('media trace: key ignored enabled=%s selected=%s', opts['enabled'], opts['mac_device'])
            return
        if down:
            if key in self.pressed or repeat:
                self.app.log.debug('media trace: key ignored held/repeat key=%s', key)
                return
            self.pressed.add(key)
            self.down_at[key] = now
            for other in list(self.clicks):
                if other != key:
                    deadline, gesture = self.clicks.pop(other)
                    self._enqueue(gesture, at=deadline - .35)
            previous = self.clicks.pop(key, None)
            if previous and now <= previous[0]:
                self.double_down.add(key)
                self.app.log.debug('media trace: second down key=%s gap_ms=%.1f', key, (now - previous[0] + .35) * 1000)
            elif previous:
                self._enqueue(previous[1], at=previous[0] - .35)
            return
        if key not in self.pressed:
            self.app.log.debug('media trace: unmatched release key=%s', key)
            return
        duration = now - self.down_at.pop(key, now)
        self.pressed.discard(key)
        if device.startswith('hid:') and duration < .03 and key not in self.held:
            self._pulse(key, now - duration, now)
            return
        if key in self.held:
            self.held.discard(key)
            self.double_down.discard(key)
            self.app.log.debug('media trace: hold released key=%s duration_ms=%.1f; no click', key, duration * 1000)
            self._release_hold(key)
        elif duration >= .6:
            self.double_down.discard(key)
            self.app.log.debug('media trace: hold on release key=%s duration_ms=%.1f', key, duration * 1000)
            self._begin_hold(key)
            self._release_hold(key)
        elif key in self.double_down:
            self.double_down.discard(key)
            self.app.log.debug('media trace: double key=%s', key)
            self._enqueue(key + '_double')
        else:
            self.clicks[key] = (now + .35, key)
            self.app.log.debug('media trace: waiting second key=%s window_ms=350', key)

    def _pulse(self, key, started, ended):
        # Some consumer HID controls emit 1/0 pulses (~1 ms) every 175 ms while held.
        # Buffer taps until silence: never execute a double before a possible repeat train ends.
        for other, run in list(self.pulse_runs.items()):
            if other != key and not run['held']:
                self._finish_pulses(other)
        previous = self.pulse_runs.get(key)
        if previous and started - previous['last'] > previous['timeout']:
            self._finish_pulses(key)
            previous = None
        if previous is None:
            timeout = .35
            if key == 'volume_up' and any(d['id'] == self.hid.active_id and d['name'] == 'AB Shutter3'
                                           for d in self.devices):
                profile = settings(self.app.cfg)['profiles'][self.mode]
                if profile.get(key + '_double'):
                    timeout = 1.05
                    if profile.get(key + '_repeat'):
                        timeout = min(timeout, profile[key + '_gap_ms'] / 1000 - .001)
            previous = {'timeout': timeout, 'first': started, 'last': ended, 'count': 0, 'held': False,
                        'gaps': []}
            self.pulse_runs[key] = previous
        if previous['count']:
            previous['timeout'] = .35
            previous['gaps'].append(started - previous['last'])
            previous['gaps'] = previous['gaps'][-4:]
        previous['last'] = ended
        previous['count'] += 1
        gaps = previous['gaps']
        # Require sustained, regular repetition; a single or double can never become a hold.
        regular = len(gaps) >= 3 and all(.10 <= gap <= .25 for gap in gaps) and max(gaps) - min(gaps) <= .065
        if not previous['held'] and regular and started - previous['first'] >= .6:
            previous['held'] = True
            self.app.log.debug('media trace: pulse hold key=%s pulses=%s span_ms=%.1f', key, previous['count'], (started - previous['first']) * 1000)
            self._begin_hold(key)
        else:
            self.app.log.debug('media trace: pulse buffered key=%s count=%s held=%s', key, previous['count'], previous['held'])

    def _finish_pulses(self, key):
        run = self.pulse_runs.pop(key, None)
        if run is None:
            return
        if run['held']:
            self.app.log.debug('media trace: pulse release inferred key=%s silence_ms=350', key)
            self._release_hold(key)
        elif run['count'] == 1:
            self._enqueue(key, at=run['last'])
        elif run['count'] == 2:
            self._enqueue(key + '_double')
        else:
            # A noisy repeat train is not a request to replay dozens of destructive tap actions.
            self.app.log.warning('media trace: ambiguous pulse train ignored key=%s count=%s', key, run['count'])

    def _holds(self, now):
        for key, run in list(self.pulse_runs.items()):
            if now - run['last'] > run['timeout']:
                self._finish_pulses(key)
        for key, started in list(self.down_at.items()):
            if key in self.pressed and key not in self.held and now - started >= .6:
                self.held.add(key)
                self.double_down.discard(key)
                self.app.log.debug('media trace: hold key=%s duration_ms=%.1f', key, (now - started) * 1000)
                self._begin_hold(key)

    def _begin_hold(self, key):
        # Both phases belong to the mode that began the hold, even if phase one changes it.
        signature, generation = settings(self.app.cfg), self.generation
        mapping = signature['profiles'][self.mode]
        self.hold_context[key] = (signature, generation, list(mapping.get(key + '_hold_release', [])))
        self._enqueue(key + '_hold', list(mapping.get(key + '_hold', [])), signature, generation)

    def _release_hold(self, key):
        context = self.hold_context.pop(key, None)
        if context is not None:
            signature, generation, action = context
            self._enqueue(key + '_hold_release', action, signature, generation)

    def _reset_keys(self):
        self.pressed.clear()
        self.down_at.clear()
        self.held.clear()
        self.hold_context.clear()
        self.pulse_runs.clear()
        self.double_down.clear()
        self.clicks.clear()

    def _listen(self):
        import AppKit
        import Quartz
        from CoreFoundation import (CFRunLoopGetCurrent, CFRunLoopAddSource, CFRunLoopRemoveSource,
                                    CFRunLoopRunInMode, kCFRunLoopCommonModes, kCFRunLoopDefaultMode)
        tap = None
        source = None
        def callback(proxy, kind, event, ref):
            if kind in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
                self.app.log.warning('media trace: event tap disabled kind=%s; resetting pending clicks', kind)
                self.generation += 1
                self._reset_keys()
                self.clicks.clear()
                Quartz.CGEventTapEnable(tap, True)
                return event
            try:
                e = AppKit.NSEvent.eventWithCGEvent_(event)
                if e is None or e.subtype() != 8:
                    return event
                data = int(e.data1()); key = KEYS.get((data >> 16) & 65535)
                phase = (data >> 8) & 255
                opts = settings(self.app.cfg)
                if not key or not mac_enabled(opts):
                    return event
                if opts['mac_device'] != 'all':
                    return event
                if phase in (10, 11):
                    self._key('all', key, phase == 10, bool(data & 1))
                    return None
            except Exception:
                self.app.log.exception('media keys: processing an event')
            return event
        hid = None
        try:
            from media_hid import HidDevices
            hid = HidDevices(self._key, self.app.log)
            self.hid = hid
        except Exception:
            self.app.log.exception('HID: enumeration unavailable')
        last_scan = 0
        signature = None
        retry_at = 0
        try:
            while not self.stop_event.is_set():
                opts = settings(self.app.cfg)
                if opts != signature:
                    self.app.log.debug('media trace: settings changed old=%s new=%s pending=%s', signature, opts, self.clicks)
                    self.generation += 1
                    self.clicks.clear()
                    self._reset_keys()
                    self.mode = 'normal'
                    signature = opts
                    retry_at = 0
                now = time.monotonic()
                if hid and hid.disconnected:
                    self.app.log.warning('media trace: device disconnected id=%s pending=%s', hid.active_id, self.clicks)
                    self.generation += 1
                    self.clicks.clear()
                    self._reset_keys()
                    hid.release()
                    hid.disconnected = False
                    self.mode = 'normal'
                    self.status = 'missing'
                    last_scan = 0
                    retry_at = 0
                if hid and now - last_scan >= 2:
                    was_active = hid.active_id
                    self.devices = hid.scan()
                    if was_active and not hid.active_id:
                        self.generation += 1
                        self.clicks.clear()
                        self._reset_keys()
                    last_scan = now
                enabled = mac_enabled(opts) and opts['mac_device'] == 'all'
                if enabled and tap is None:
                    tap = Quartz.CGEventTapCreate(Quartz.kCGHIDEventTap, Quartz.kCGHeadInsertEventTap,
                                                 Quartz.kCGEventTapOptionDefault, 1 << 14, callback, None)
                    if tap is None:
                        self.status = 'permission'
                        self.stop_event.wait(2)
                        continue
                    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
                    CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopCommonModes)
                    Quartz.CGEventTapEnable(tap, True)
                    self.status = 'ready'
                if not enabled and tap is not None:
                    Quartz.CGEventTapEnable(tap, False)
                    CFRunLoopRemoveSource(CFRunLoopGetCurrent(), source, kCFRunLoopCommonModes)
                    Quartz.CFMachPortInvalidate(tap)
                    tap = source = None
                    self._reset_keys()
                if hid:
                    if not mac_enabled(opts) or opts['mac_device'] == 'all':
                        hid.release()
                    elif now >= retry_at:
                        self.status = hid.select(opts['mac_device'])
                        retry_at = now + (2 if self.status != 'ready' else .1)
                elif opts['enabled'] and opts['mac_device'] != 'all':
                    self.status = 'error'
                if mac_enabled(opts) and self.status == 'ready':
                    now = time.monotonic()
                    self._holds(now)
                    for key, (deadline, action) in list(self.clicks.items()):
                        if now >= deadline:
                            self.clicks.pop(key, None)
                            self.app.log.debug('media trace: single key=%s deadline_late_ms=%.1f', key, (now - deadline) * 1000)
                            self._enqueue(action, at=deadline - .35)
                else:
                    self.clicks.clear()
                if not mac_enabled(opts):
                    self.status = 'off'
                CFRunLoopRunInMode(kCFRunLoopDefaultMode, .1, False) if tap or hid else self.stop_event.wait(.2)
        except Exception:
            self.status = 'error'
            self.app.log.exception('media keys: monitor stopped')
        finally:
            if hid:
                hid.close()
            if tap is not None:
                Quartz.CGEventTapEnable(tap, False)
                CFRunLoopRemoveSource(CFRunLoopGetCurrent(), source, kCFRunLoopCommonModes)
                Quartz.CFMachPortInvalidate(tap)
