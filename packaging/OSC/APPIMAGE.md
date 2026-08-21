# OBS AppImage packaging

This directory can build an AppImage alongside the existing RPM and DEB
packages on the Open Build Service (OBS). The recipe follows the
[OBS AppImage packaging model](https://docs.appimage.org/packaging-guide/hosted-services/opensuse-build-service.html):
OBS first builds the native `nativmix` RPM, then `appimage.yml` extracts that
package and its resolved RPM dependencies into an AppDir.

The recipe is build material, not a claim that this fork currently publishes
or supports a tested AppImage download. Native packages provide the most
complete integration. The fork's downloadable Flatpak bundle is the preferred
portable option, especially on Bazzite and other immutable systems.

## Source and identity

`_service` has two independent jobs:

- `tar_scm`, `recompress`, `set_version`, and `download_url` produce the same
  source inputs used by the existing RPM and DEB builds.
- `appimage` reads `appimage.yml` only for an AppImage repository.

The Git service consumes `https://github.com/Arthur-D/NativMix.git` at the
immutable current-main commit recorded in `_service`. The fork does not
currently publish the local upstream-derived version tags to its GitHub remote,
so the service also records the existing package version explicitly instead of
referencing a nonexistent tag. Update `revision` and `versionformat` together
as part of the normal release process; never point a reproducible release build
at a moving branch. `filename` remains `nativmix`, matching `Source0` in the RPM
spec and `DEBTRANSFORM-TAR` in `nativmix.dsc`.

The AppImage intentionally uses `nativmix.desktop`, `Icon=nativmix`, and the
`nativmix` executable from the native package. Those names agree with the Qt
desktop file name and `StartupWMClass`. They are separate from the Flatpak ID
`io.github.ArthurD.NativMix`; the AppImage recipe does not install or overwrite
the Flatpak desktop, icon, or metainfo files. The GPL license is included from
the RPM at `/usr/share/licenses/nativmix/LICENSE`.

## OBS project setup

Add an AppImage repository to the OBS project metadata. Replace `FORK_PROJECT`
and the RPM repository name with the project that builds this fork:

```xml
<repository name="AppImage">
  <path project="FORK_PROJECT" repository="openSUSE_Tumbleweed"/>
  <path project="OBS:AppImage" repository="AppImage"/>
  <arch>x86_64</arch>
</repository>
```

The first path must expose the `nativmix` RPM built from this package. The
second supplies the OBS AppImage toolchain. Keep the existing native
repositories unchanged; the added `<service name="appimage"/>` does not replace
the source services used by RPM or DEB generation.

With `osc`, `obs-service-tar_scm`, and `obs-service-appimage` installed and
configured for the target project, a local OBS worker can exercise the same
recipe:

```bash
cd packaging/OSC
osc service run
osc build AppImage x86_64
```

The exact repository and architecture arguments must match the OBS project
metadata. OBS builds are network-isolated after source services run, so all
runtime content must come from the declared RPM ingredient and its repository
dependencies.

## Runtime limitations

An AppImage is a mounted application filesystem, not a complete Linux system
and not a hardware sandbox. Validate all of the following before publishing an
artifact:

- **Audio:** the host must run PipeWire or PulseAudio and expose a usable Pulse
  socket and/or PipeWire socket to the process. NativMix's native PipeWire tools
  (`pw-dump`, `pw-cli`, and `wpctl`) and Pulse fallback (`pactl`) may still need
  compatible host packages. The AppImage cannot provide or start the host audio
  daemon.
- **Arduino/USB serial:** the process sees host devices directly, but the user
  still needs permission for the serial device. The udev rule contained inside
  an AppImage is not installed on the host. Install the native package's rule or
  configure equivalent host permissions; never run NativMix as root.
- **MIDI:** ALSA sequencer devices and any controller must be available to the
  user. Bundling Python MIDI libraries does not create host kernel devices.
  Builds that use portmidi retain its limitation: no NativMix virtual MIDI port.
- **Qt and desktop integration:** OBS can bundle Qt libraries and platform
  plugins from RPM dependencies, but Wayland/X11, graphics drivers, desktop
  portals, notification hosts, and theme engines remain host interfaces.
  Host-only Qt styles may be unavailable or ABI-incompatible, so appearance can
  differ from a native package.
- **Autostart:** the systemd user unit packaged inside the AppImage is not
  installed into the host's unit search path. The in-app fallback may record a
  temporary AppImage mount path, so do not rely on its autostart toggle. Create
  a user desktop entry that executes the AppImage's stable absolute path, or use
  a trusted AppImage integration tool.

These host requirements mean an AppImage is not guaranteed to work on every
distribution merely because it launches. Prefer a native package when
available, and prefer the fork's Flatpak bundle for a maintained portable
installation path.

## Publication checklist

1. Run the source services and confirm the generated archive name matches the
   version substituted into the RPM spec and Debian metadata.
2. Build the native RPM and DEB repositories to confirm AppImage enablement did
   not change their source inputs.
3. Build the AppImage and inspect it for the launcher, desktop file, icon,
   license, Python modules, Qt platform plugins, and declared shared libraries.
4. Test launch, PipeWire/Pulse control, Arduino serial access, physical MIDI,
   Wayland and X11 startup, notifications, and clean shutdown on representative
   hosts. Record exactly which distributions were tested.
5. Publish only after documenting remaining host packages and permissions.

Report fork-specific packaging problems in
[Arthur-D/NativMix Issues](https://github.com/Arthur-D/NativMix/issues).
