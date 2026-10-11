# NativMix UI prototype

Open `nativmix-varsom.html` in a browser to explore the proposed interface.
This self-contained English mockup uses labels from the native Qt application.
The mixer fills the window and scales with it. Strips are at least 80 CSS pixels
wide, or 64 pixels in **Compact** mode, and widen only with the control scale (up to
104, Compact: 83 pixels), staying left-aligned so unused width is not padding. Fader travel grows from 140 to 560 pixels with the available
height, and handles, track, and level text scale up to 1.6 times. The scale beside
each fader becomes more granular as it grows: labels every 25, 20, 10, or 5 at the largest sizes, with
minor ticks every 5 when they stay legible. At the minimum size, the toolbar wraps
and the mixer scrolls instead of shrinking or stacking the faders.
Faders use rotated range controls to stay vertical in browser engines that do not
support vertical range rendering through CSS writing mode.

Channel actions (**+ Add Channel**, **Edit Channel**, **Delete Selected**),
feedback, connection status, and the preview selectors sit in a narrow vertical
column to the left of the first strip, so the faders get the full window height.
Below about 480 px wide, that column moves back under the mixer.

Assignments and **Options** open wider, keyboard-accessible popover menus without
widening the strips. Profile actions are separate from the profile selector,
matching the application. The preview-state selector demonstrates loading, audio
errors, empty profiles, and unavailable apps.

Remote control follows the current application: **Send** and **Receive** are
independent checkboxes that can run together, and **Local mixer** / **Remote mixer**
chooses which mixer the window shows and local MIDI hardware controls. The mockup
adds a compact **Local | Remote** switch to the toolbar while Send is on, tints the
remote mixer, names the remote desktop in the title bar, and shows remote and
sleep-prevention status in the status line. The **Desktop receiver** preview
selector simulates a receiver that allows or disables mixer editing; when editing
is disabled, the remote mixer stays unavailable.

All data and interactions are simulated in memory. Reloading resets changes.
Advanced settings are illustrative; this file does not control audio or hardware
and is not integrated into the Python application.

The native Qt mixer already has consolidated assignment and profile menus,
channel options, collapsed advanced settings, and persistent connection status.
This mockup proposes tighter widths and spacing; those visual changes have not
been applied to the Python application.
