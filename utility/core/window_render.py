"""Redraw the main window on the Cocoa thread without overriding WebKit layers."""
import threading


def install(window, log):
    from PyObjCTools import AppHelper

    pending = threading.Event()
    revealed = threading.Event()

    def redraw(*_):
        if pending.is_set():
            return
        pending.set()

        def apply():
            try:
                native = window.native
                view = native.contentView() if native else None
                if view is None:
                    return
                native.setOpaque_(True)
                view.setNeedsLayout_(True)
                view.layoutSubtreeIfNeeded()
                view.setNeedsDisplay_(True)
                for child in view.subviews():
                    child.setNeedsDisplay_(True)
                native.displayIfNeeded()
                log.debug("Window native redraw, layer=%s", view.wantsLayer())
            except Exception as exc:
                log.warning("Window layer could not be updated: %s", exc)
            finally:
                pending.clear()

        AppHelper.callAfter(apply)

    window.events.loaded += redraw
    window.events.shown += redraw
    window.events.resized += redraw
    window.events.restored += redraw
    def ready():
        def reveal():
            if revealed.is_set():
                redraw()
                return
            native = window.native
            if native is None:
                AppHelper.callLater(0.1, reveal)
                return
            revealed.set()
            # WebKit is attached and application data has arrived before first presentation.
            view = native.contentView()
            view.setNeedsLayout_(True)
            view.layoutSubtreeIfNeeded()
            native.makeKeyAndOrderFront_(None)
            native.displayIfNeeded()
            log.info("Window shown after the interface signaled readiness")
            redraw()
        AppHelper.callAfter(reveal)

    # A failed settings request must not leave the application permanently hidden.
    def fallback(*_):
        AppHelper.callLater(8, ready)
    window.events.loaded += fallback
    return ready
