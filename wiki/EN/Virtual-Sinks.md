# Virtual Sinks (V-Sinks)

NativMix utilizes dedicated software output devices (Virtual Sinks) in PipeWire to solve a common Linux audio issue: **Audio Spikes.**

## The Problem: Volume Spikes

Many applications (like web browsers or media players) momentarily reset their internal stream volume to 100% when seeking, fast-forwarding via keyboard, or recovering from a "hanging" stream. This often causes painful, full-volume spikes before PipeWire or a standard mixer can re-apply the correct fader level.

## The Solution: Isolation via V-Sinks

By creating a Virtual Sink as an intermediary, NativMix decouples the application from the physical output:

- **Signal Flow**: `App (fixed at 100% Unity Gain) → V-Sink (controlled by hardware fader) → Physical Output`
- **Persistence**: Since the app always plays at 100% inside its own isolated tunnel, any seek-related reset has **zero impact** on the actual output volume.
- **Hardware Precision**: The physical slider controls the volume of the *Virtual Sink* itself — a rock-solid volume ceiling the application cannot bypass.

## Features & Constraints

| Feature | Description |
| :--- | :--- |
| **Safe On/Off** | Disabling a V-Sink automatically sets the app to the current fader volume first, then lets PipeWire rescue the stream without pausing playback. |
| **Live App Assignment** | Assigning an already-running app to a channel with an active V-Sink routes it in immediately — no need to recreate the sink or restart the app. |
| **Creation Lock** | While a new V-Sink is being built, slider input for that channel is temporarily suppressed. Once PipeWire has settled (~50 ms) and the real fader volume has been applied, the lock is released. This prevents stray slider ticks from accidentally writing to the system sink instead of the new V-Sink. |
| **Isolation Rules** | The *System Master* and *Other Apps* channels cannot be routed through V-Sinks (would cause feedback loops or unpredictable system volume behavior). |

## How to Enable

1. Open the settings panel for any channel.
2. Enable the **V-Sink** toggle.
3. NativMix creates the virtual sink and routes the assigned app(s) automatically.

> [!TIP]
> Use the **Panic Button** in settings to immediately evacuate all apps from V-Sinks and destroy them — useful if something gets stuck.
