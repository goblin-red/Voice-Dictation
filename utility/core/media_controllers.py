"""Independent input state machines for concurrently connected controllers."""
import copy
import threading
import media_controls

class ControllerApp:
    def __init__(self, app, binding_id=None):
        self.owner, self.binding_id, self.controller = app, binding_id, None

    @property
    def cfg(self):
        raw = self.owner.cfg.get('media_controls') or {}
        controls = media_controls.settings(self.owner.cfg)
        bindings = raw.get('bindings')
        if self.binding_id is None:
            controls['enabled'] = controls['enabled'] and bindings is None
        else:
            selected = next((b for b in bindings or [] if b['id'] == self.binding_id), None)
            controls['enabled'] = bool(controls['enabled'] and selected and selected['enabled'])
            if selected:
                controls.update(source='mac', mac_device=selected['device'], profiles=copy.deepcopy(selected['profiles']))
        controls.pop('bindings', None)
        # A device changing its bindings must not cancel another device's pending gesture.
        controls.pop('remote_devices', None)
        return {'media_controls': controls}

    def __getattr__(self, key):
        return getattr(self.owner, key)

    def media_action(self, action):
        modes = {'normal_mode':'normal','terminal_mode':'terminal','extra1_mode':'extra1','extra2_mode':'extra2'}
        if action in modes:
            return self.controller.set_mode(modes[action])
        with self.owner.media_keys.action_lock:
            return self.owner.media_action(action)

class ControllerManager:
    def __init__(self, app):
        self.app, self.action_lock = app, threading.RLock()
        self.stop_event = threading.Event()
        proxy = ControllerApp(app)
        self.legacy = media_controls.MediaControls(proxy);proxy.controller = self.legacy
        self.children = {}

    @property
    def devices(self): return self.legacy.devices
    @property
    def mode(self): return self.legacy.mode
    @property
    def status(self):
        if not media_controls.settings(self.app.cfg)['enabled']: return 'off'
        if 'bindings' not in (self.app.cfg.get('media_controls') or {}): return self.legacy.status
        states = [c.status for c in list(self.children.values())]
        return 'ready' if 'ready' in states else (states[0] if states else 'off')
    @property
    def controller_states(self):
        return {key: {'mode': c.mode, 'status': c.status} for key,c in list(self.children.items())}

    def set_mode(self, mode):
        self.legacy.set_mode(mode)
    def toggle_terminal_mode(self): self.set_mode('terminal')
    def start(self):
        self.legacy.start()
        threading.Thread(target=self._run,daemon=True,name='controller-manager').start()
    def stop(self):
        self.stop_event.set();self.legacy.stop()
        for child in list(self.children.values()): child.stop()
    def _run(self):
        while not self.stop_event.is_set():
            bindings = (self.app.cfg.get('media_controls') or {}).get('bindings') or []
            wanted = {b['id'] for b in bindings if b.get('host')=='mac'}
            for ident in list(self.children):
                if ident not in wanted: self.children.pop(ident).stop()
            for ident in wanted:
                if ident not in self.children:
                    proxy=ControllerApp(self.app,ident);child=media_controls.MediaControls(proxy);proxy.controller=child
                    self.children[ident]=child;child.start()
            self.stop_event.wait(.2)
