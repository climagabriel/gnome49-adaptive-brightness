# gnome49-adaptive-brightness

A small Python userspace daemon that restores adaptive screen brightness on
GNOME 49, which ships with a regression in `gsd-power` that prevents the
built-in adaptive-brightness algorithm from ever applying a value.

## The bug

In `gnome-settings-daemon` 48.x, the ambient-light algorithm in
`plugins/power/gsd-power-manager.c` bootstrapped its `ambient_percentage_old`
state from the current screen brightness via `gsd_backlight_get_brightness()`,
and kept it updated through backlight-change callbacks.

For 49.0, screen-brightness ownership was moved out of `gsd-power` into
`gnome-shell` (`org.gnome.Shell.Brightness.SetAutoBrightnessTarget`), and those
three init/update paths were removed without replacement. The
`org.gnome.Shell.Brightness` interface exposes no `Brightness` property and no
`BrightnessChanged` signal, so `gsd-power` has no way to learn the current
brightness. `ambient_percentage_old` is initialised to `-1.0` and stays there
forever.

The runtime effect (with `G_MESSAGES_DEBUG=all`):

    Read last absolute light level: 1.301000
    Renormalizing light level from old light percentage: -1.0%

repeats on every ambient update, and the next-stage log line
`Setting brightness from ambient %.1f%%` never fires. The algorithm
short-circuits at line 2974:

    if (manager->ambient_accumulator < 0.f)
        goto out;

`ambient_accumulator` can only become non-negative *after* a successful apply,
which can only happen *after* `ambient_percentage_old` is positive — a
chicken-and-egg deadlock with no bootstrap.

The rest of the pipeline (HID ALS → `iio-sensor-proxy` → `gsd-power` event
delivery → `gnome-shell` `SetAutoBrightnessTarget` → `mutter` →
`/sys/class/backlight/...`) is healthy; only the algorithm step is broken.

## What this daemon does

Replaces just the broken algorithm step:

1. Claims `net.hadess.SensorProxy::Light` and subscribes to `LightLevel`
   `PropertiesChanged` on the system bus.
2. Maps lux to a brightness fraction using a log curve
   (1 lux → `MIN_PCT`, 1000+ lux → 100%).
3. Applies EWMA smoothing (`TIME_CONSTANT_US`, default 5 s) and 1% hysteresis.
4. Calls `org.gnome.Shell.Brightness.SetAutoBrightnessTarget(d)` on the
   session bus, which `gnome-shell` forwards to `mutter`.

Approximately 110 lines of Python, depends only on `python3-gi` (GLib/Gio,
already present on any GNOME system).

## Install

    install -Dm755 adaptive-brightness.py ~/.local/bin/adaptive-brightness.py
    install -Dm644 adaptive-brightness.service ~/.config/systemd/user/adaptive-brightness.service

    # disable gsd-power's broken ambient handler so the two don't fight
    gsettings set org.gnome.settings-daemon.plugins.power ambient-enabled false

    systemctl --user daemon-reload
    systemctl --user enable --now adaptive-brightness.service

The OSD brightness slider and `Fn` brightness keys keep working as normal —
`mutter` still owns the hardware write path.

## Uninstall

    systemctl --user disable --now adaptive-brightness.service
    rm ~/.config/systemd/user/adaptive-brightness.service
    rm ~/.local/bin/adaptive-brightness.py
    gsettings set org.gnome.settings-daemon.plugins.power ambient-enabled true

## Tuning

Top of `adaptive-brightness.py`:

| Knob              | Default       | Meaning                                  |
|-------------------|---------------|------------------------------------------|
| `MIN_PCT`         | `0.05`        | Floor target so the panel never goes black in pitch dark |
| `LUX_FLOOR`       | `1.0`         | At or below this lux, target == `MIN_PCT` |
| `LUX_FULL`        | `1000.0`      | At or above this lux, target == 100%      |
| `TIME_CONSTANT_US`| `5_000_000`   | EWMA time constant (µs); higher = smoother |
| `EPS`             | `0.01`        | Don't resend if target moved less than this |

Restart after editing: `systemctl --user restart adaptive-brightness.service`.

## Known limitations

* No user-offset preservation: manually moving the OSD slider sticks only until
  the next ambient update. Adding offset preservation would require subscribing
  to `org.gnome.Mutter.DisplayConfig` `Backlight` `PropertiesChanged`,
  distinguishing user writes from our own, and tracking
  `user_value − ambient_target` as an offset. Not implemented.
* Single-panel only (uses whichever connector `gnome-shell` advertises). Not
  tested on multi-panel setups.

## Status

Workaround pending upstream fix. The bug is present in
`gnome-settings-daemon` 49.0; the `main` branch has refactored that file
further (`ambient_percentage_old` no longer exists), so the fix may land in a
subsequent release.
