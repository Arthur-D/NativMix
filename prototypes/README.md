# NativMix UI prototype

Open `nativmix-varsom.html` in a browser to explore the proposed interface.
This self-contained English mockup uses labels from the native Qt application.
The window sizes to its channels: strips are 80 CSS pixels wide, or 64 pixels in
**Compact** mode, with 3-pixel horizontal padding. Five channels occupy a
410-pixel-wide window, or 330 pixels in Compact mode. Fader travel is 140 pixels,
with smaller 18-by-10-pixel handles and a 28-pixel-wide interaction area.
The toolbar wraps and the mixer scrolls horizontally on narrower
screens instead of widening the window or stacking the faders.
Faders use rotated range controls to stay vertical in browser engines that do not
support vertical range rendering through CSS writing mode.

Assignments and **Options** open wider, keyboard-accessible popover menus without
widening the strips. Profile actions are separate from the profile selector,
matching the application. The preview-state selector demonstrates loading, audio
errors, empty profiles, and unavailable apps.

All data and interactions are simulated in memory. Reloading resets changes.
Advanced settings are illustrative; this file does not control audio or hardware
and is not integrated into the Python application.

The native Qt mixer already has consolidated assignment and profile menus,
channel options, collapsed advanced settings, and persistent connection status.
This mockup proposes tighter widths and spacing; those visual changes have not
been applied to the Python application.
