# NativMix

NativMix is a hardware-based volume mixer for Linux, built with PyQt6. It connects physical Arduino potentiometers via USB to PipeWire/PulseAudio, giving you per-app volume control through real faders. Each channel can be mapped to one or more apps, a hardware device, or the system master. **Virtual Sinks** isolate apps in a dedicated PipeWire null-sink so seek-related volume spikes never reach your ears — the hardware fader controls the sink, the app stays at unity gain inside. New streams are caught and muted instantly (Two-Stage Mute-Catch) before metadata is available, then released at the correct fader level. MIDI CC controllers are supported natively alongside the Arduino, with MIDI-Learn and a built-in virtual MIDI port. The GUI follows your system theme automatically via the XDG Desktop Portal and works on KDE, GNOME, and any XDG-compliant desktop including Wayland.

## About this fork

This repository is a fork of
[knoellix/NativMix](https://github.com/knoellix/NativMix). Compared with upstream, its main user-facing
additions are:

| Area | Difference from upstream |
| ---- | ------------------------ |
| Installation and PipeWire | Flatpak packaging and PipeWire-focused compatibility for sandboxed environments such as Bazzite. Native graph discovery uses `pw-dump`; volume writes can use PipeWire's Pulse compatibility API. Flatpak autostart is requested through the XDG Desktop Background portal. |
| Routing ownership | Configured and effective routing-owner modes: *Auto*, *NativMix*, *Easy Effects*, and *None*. The owner can change without restarting. Streams already held by Easy Effects are left in place, and a regular app can pause only NativMix automatic routing from its right-click menu while volume and mute remain active. |
| Channel layout | Channel strips can be reordered per profile from the separator grip, by keyboard, or from its context menu. Visual order does not renumber hardware channels or MIDI bindings. |
| Shared control targets | Multiple controls can target the same regular app, exact hardware sink/source, *System Master*, or *Other Apps*. MIDI-feedback siblings stay synchronized if *Sync position to MIDI controller* is turned on in *Settings*. If it's off, the last control moved wins. |
| Profiles and MIDI | More robust profile reconciliation, dynamic MIDI channels, stable channel identities, and preservation of live MIDI bindings when channels are added, including hybrid and MIDI-only setups. |
| Virtual sinks | Capability-aware V-Sink handling with deduplication and stale-sink cleanup across profiles and routing owners. |

The upstream [installation guide](https://github.com/knoellix/NativMix/wiki/EN-Installation) and
[wiki](https://github.com/knoellix/NativMix/wiki/) remain useful for native packages. The fork's
[release page](https://github.com/Arthur-D/NativMix/releases) also provides downloadable Flatpak bundles for
immutable distributions such as Bazzite and other portable installs. Report fork-specific bugs or behavior in
[Arthur-D/NativMix Issues](https://github.com/Arthur-D/NativMix/issues). See the
[full upstream-to-fork comparison](https://github.com/knoellix/NativMix/compare/main...Arthur-D:main) for the
complete history; this summary intentionally covers capabilities rather than every change.

![NativMix Icon](assets/icon.png)

<div align="center">

| USB MIDI controller |
| ------------------- |
| ![NativMix USB MIDI controller](assets/mixer.jpg) |

| Breeze Theme (Native) | Iridescent Theme |
| --------------------- | ---------------- |
| ![Breeze Theme](assets/Breeze.jpg) | ![Iridescent Theme](assets/Iridescent_Lightly_3.jpg) |

| Settings & mixer (Full UI) |
| ---------------------------- |
| ![NativMix settings and mixer view](assets/nothing.jpg) |

</div>

---

## Status

> MIDI stability data is still being collected — share feedback in [Discussions](https://github.com/Arthur-D/NativMix/discussions).
>
> **Note on "Stable":** Unless otherwise noted, "Stable" means the package installs and no obvious errors appear on first use. Only **Arch Linux / CachyOS** is daily-driven and tested in production.


| OS                       | Status     | Notes                                                                                       |
| ------------------------ | ---------- | ------------------------------------------------------------------------------------------- |
| **Arch Linux / CachyOS** | ✅ Stable   | Upstream-maintained AUR package, daily driver                                                |
| **Ubuntu 25.04 / 25.10** | ✅ Stable   | OBS package, tested on Pop!_OS                                                              |
| **Ubuntu 24.04 / 24.10** | ✅ Stable   | OBS package                                                                                 |
| **Linux Mint 22**        | ✅ Stable   | Uses Ubuntu 24.04 OBS package                                                               |
| **Pop!_OS**              | ✅ Stable   | COSMIC desktop, GUI tested, no log errors                                                   |
| **openSUSE Tumbleweed**  | ✅ Stable   | OBS package, GUI tested, no log errors                                                      |
| **openSUSE Slowroll**    | ❓ Untested | OBS package                                                                                 |
| **Fedora 42 / 43 / 44**  | ⚠️ See note | OBS package; 42/43 core functions tested, 44 untested — PortMidi fallback if RtMidi is unavailable |
| **Debian 12 / 13**       | ✅ Stable   | OBS package — based on Ubuntu compatibility                                                 |
| **Raspberry Pi OS**      | ❓ Untested | OBS package — no Pi test hardware available                                                 |
| **Windows 10 / 11**      | ✅ Stable   | GitHub Release installer — not daily-driven by the maintainer (no V-Sinks, no virtual MIDI) |


> **Windows — feedback welcome!** Quick notes (works / breaks where) belong in [Discussions](https://github.com/Arthur-D/NativMix/discussions). Concrete bugs with repro steps please as an [Issue](https://github.com/Arthur-D/NativMix/issues).


| Desktop Environment | Status   | Notes                                                                                                                                                                                            |
| ------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **KDE Plasma**      | ✅ Stable | Wayland + X11, daily driver                                                                                                                                                                      |
| **COSMIC**          | ✅ Stable | Tested on Pop!_OS                                                                                                                                                                                |
| **GNOME**           | ✅ Stable | Wayland — sluggish system volume via NativMix reported and fixed in v1.0.14 ([#19](https://github.com/knoellix/NativMix/issues/19), thanks [@AdityaHebballe](https://github.com/AdityaHebballe)) |


> **MIDI backend:** NativMix prefers RtMidi on every platform. The Flatpak bundles
> RtMidi for hotplug-safe physical and virtual MIDI. Fedora/Nobara packages may
> use an explicit PortMidi compatibility fallback when `python-rtmidi` is not
> installed; that fallback cannot safely support USB hot-unplug and has no
> virtual MIDI port.
> Fedora 44 is listed as a packaging target but has not been maintainer-tested.
> Quick notes in [Discussions](https://github.com/Arthur-D/NativMix/discussions); bugs as an [Issue](https://github.com/Arthur-D/NativMix/issues).

---

## Installation

→ **[Full Installation Guide](https://github.com/knoellix/NativMix/wiki/EN-Installation)**

Choose the installation path that matches the system:

- **Native packages:** the [`nativmix` AUR package](https://aur.archlinux.org/packages/nativmix)
  and RPM/DEB repositories are maintained upstream by
  [`knoelliX`](https://github.com/knoellix/NativMix) and follow upstream releases.
  They do not include Arthur-D fork features such as those in v1.1.0 unless
  upstream adopts them. For this fork, use its Flatpak release bundle or a
  source/local build.
- **Downloaded Flatpak bundle:** the fork's release assets are the preferred
  portable option, especially on Bazzite and other immutable distributions.
- **AppImage:** the unfinished OBS target was removed. Flatpak is the supported
  portable Linux build for this fork.
- **Developer build:** install from this source tree as described by the
  packaging-specific local build documents; this is not a packaged update channel.

For Bazzite, other immutable distributions, or a portable install, download the
`.flatpak` asset from the matching [fork release](https://github.com/Arthur-D/NativMix/releases), then run:

```bash
flatpak install --user ./io.github.ArthurD.NativMix-v1.1.0.flatpak
```

Use the exact downloaded filename. Reinstall a newer downloaded bundle with the
same command to update it. A single-file GitHub bundle does **not** add a Flatpak
remote, so `flatpak update` cannot discover later releases automatically; download
and install each newer release unless a repository remote is provided in the future.
Flatpak and Windows builds can optionally check the fork's GitHub releases once
per startup. This is disabled by default and GitHub is not contacted until you
enable **Check GitHub for updates** in Settings. Checks only show a release link;
NativMix does not download or execute updates and includes no telemetry. Native
Linux packages continue to use their package manager, so this setting is hidden.
See the [Flatpak installation notes](packaging/FLATPAK.md) for details and local
build instructions.

**Arch Linux / CachyOS:**

```bash
paru -S nativmix
```

[![OBS Build Status](https://build.opensuse.org/projects/home:knoelliX/packages/nativmix/badge.svg)](https://software.opensuse.org/download.html?project=home%3AknoelliX&package=nativmix)

---

## Documentation

- [Wiki (EN)](https://github.com/knoellix/NativMix/wiki/)
- [Wiki (DE)](https://github.com/knoellix/NativMix/wiki/DE-Home)

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/knoellix/NativMix)

---

## Update History

**v1.1.0 - Arthur-D fork release**

- Flatpak/Bazzite and native PipeWire support with live routing ownership
- Shared app, hardware, System Master, and Other Apps controls
- Stable profile/MIDI reconciliation, feedback, and channel preservation
- V-Sink reconciliation, late-stream restoration, portal autostart, adaptive theme, and opt-in updates

→ [Full changelog](CHANGELOG.md)

---

## License

GPL-3.0 – see [LICENSE](LICENSE) for details.
