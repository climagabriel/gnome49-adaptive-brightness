#!/usr/bin/env python3
"""
Adaptive screen brightness for GNOME 49 — works around the gsd-power regression
where ambient_percentage_old is never bootstrapped (see gsd-power-manager.c:2974
short-circuit). We subscribe to net.hadess.SensorProxy on the system bus and
drive org.gnome.Shell.Brightness.SetAutoBrightnessTarget on the session bus.

Target is a 0..1 fraction of the panel's [min, max] backlight range; gnome-shell
clamps and forwards it to mutter, which writes /sys/class/backlight/<panel>.
"""
import math
import signal
import sys
from gi.repository import Gio, GLib

# Curve: lux -> brightness fraction in [MIN_PCT, 1.0].
# Log-scaled so the dim end gets fine control. Tunable.
MIN_PCT = 0.20       # floor so the panel is never invisible in pitch dark
LUX_FULL = 300.0     # >= this lux -> full brightness
LUX_FLOOR = 1.0      # < this lux -> MIN_PCT

# EWMA time constant (microseconds). Lower = snappier, higher = smoother.
TIME_CONSTANT_US = 5_000_000  # 5s

# Hysteresis: don't bother resending if target moved less than this fraction.
EPS = 0.01


def lux_to_target(lux: float) -> float:
    if lux <= LUX_FLOOR:
        return MIN_PCT
    if lux >= LUX_FULL:
        return 1.0
    # log-linear ramp from (LUX_FLOOR, MIN_PCT) to (LUX_FULL, 1.0)
    t = math.log(lux / LUX_FLOOR) / math.log(LUX_FULL / LUX_FLOOR)
    return MIN_PCT + t * (1.0 - MIN_PCT)


class AdaptiveBrightness:
    def __init__(self):
        self.sysbus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        self.sessionbus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.last_time_us = 0
        self.ewma = -1.0
        self.last_sent = -1.0

        self.sensor = Gio.DBusProxy.new_sync(
            self.sysbus, Gio.DBusProxyFlags.NONE, None,
            "net.hadess.SensorProxy", "/net/hadess/SensorProxy",
            "net.hadess.SensorProxy", None,
        )
        self.shell_brightness = Gio.DBusProxy.new_sync(
            self.sessionbus, Gio.DBusProxyFlags.NONE, None,
            "org.gnome.Shell", "/org/gnome/Shell/Brightness",
            "org.gnome.Shell.Brightness", None,
        )

        has_ctrl = self.shell_brightness.get_cached_property("HasBrightnessControl")
        if not has_ctrl or not has_ctrl.get_boolean():
            print("org.gnome.Shell.Brightness reports no brightness control; exiting",
                  file=sys.stderr)
            sys.exit(2)

        self.sensor.call_sync("ClaimLight", None,
                              Gio.DBusCallFlags.NONE, -1, None)
        self.sensor.connect("g-properties-changed", self._on_props_changed)
        # Prime with the current cached value.
        self._maybe_update()

    def _on_props_changed(self, _proxy, _changed, _invalidated):
        self._maybe_update()

    def _maybe_update(self):
        val = self.sensor.get_cached_property("LightLevel")
        has = self.sensor.get_cached_property("HasAmbientLight")
        if val is None or has is None or not has.get_boolean():
            return
        lux = val.get_double()
        if lux <= 0.0:
            return

        target = lux_to_target(lux)
        now = GLib.get_monotonic_time()
        if self.ewma < 0.0 or self.last_time_us == 0:
            self.ewma = target
        else:
            dt = now - self.last_time_us
            alpha = 1.0 - math.exp(-dt / TIME_CONSTANT_US)
            self.ewma = alpha * target + (1.0 - alpha) * self.ewma
        self.last_time_us = now

        if self.last_sent < 0 or abs(self.ewma - self.last_sent) >= EPS:
            self.shell_brightness.call(
                "SetAutoBrightnessTarget",
                GLib.Variant("(d)", (self.ewma,)),
                Gio.DBusCallFlags.NONE, -1, None, None, None,
            )
            self.last_sent = self.ewma

    def release(self):
        try:
            self.sensor.call_sync("ReleaseLight", None,
                                  Gio.DBusCallFlags.NONE, -1, None)
        except GLib.Error:
            pass


def main():
    ab = AdaptiveBrightness()
    loop = GLib.MainLoop()

    def stop(*_):
        ab.release()
        loop.quit()

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, stop)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, stop)
    loop.run()


if __name__ == "__main__":
    main()
