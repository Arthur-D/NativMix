# Flatpak installation

The fork's Flatpak manifest is
[`flatpak/io.github.ArthurD.NativMix.yml`](../flatpak/io.github.ArthurD.NativMix.yml)
and uses the application ID `io.github.ArthurD.NativMix`.

## Downloaded release bundle

Each tagged [Arthur-D/NativMix release](https://github.com/Arthur-D/NativMix/releases)
provides a versioned, single-file `.flatpak` bundle. This is the portable option
for immutable distributions such as Bazzite and for systems where a native
package is not appropriate.

After downloading the bundle, install it once for the current user:

```bash
flatpak install --user ./io.github.ArthurD.NativMix-v1.0.18.flatpak
flatpak run io.github.ArthurD.NativMix
```

The exact filename follows the release tag shown on the download page.

A directly downloaded bundle is not a Flatpak repository remote. It therefore
does not receive new releases through `flatpak update`. To upgrade, download the
newer bundle from the fork's release page and run `flatpak install --user` on
that file again, accepting the update. Uninstalling first is normally
unnecessary, but `flatpak uninstall --user io.github.ArthurD.NativMix` followed
by a fresh install is also supported.

## Native packages

Native AUR, OBS, and other distribution packages remain documented in the
[upstream installation guide](https://github.com/knoellix/NativMix/wiki/EN-Installation).
Those packages follow their distribution's normal package-manager update path;
the fork's downloaded `.flatpak` bundle is a separate installation channel.

## Appearance

The Flatpak follows the desktop portal's light/dark preference and accent color.
When Qt can load a selected widget style inside the sandbox, NativMix leaves it
unchanged. Otherwise it uses a readable Fusion light/dark palette. Sandbox
isolation means host-only widget themes such as Kvantum may not be reproduced
exactly; native packages continue to use the desktop's Qt theme integration.

## Local build

Install the KDE 6.11 runtime and SDK from Flathub, then build and install from
this checkout:

```bash
flatpak remote-add --user --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
flatpak install --user flathub org.kde.Platform//6.11 org.kde.Sdk//6.11
flatpak-builder --user --install --force-clean \
  --install-deps-from=flathub \
  "$HOME/.cache/nativmix-flatpak/build" \
  flatpak/io.github.ArthurD.NativMix.yml
flatpak run io.github.ArthurD.NativMix
```

This local builder install is distinct from the downloaded single-file release
bundle. A future hosted Flatpak repository could provide remote-based updates,
but no such remote is currently provided.

Report fork-specific packaging or runtime problems in
[Arthur-D/NativMix Issues](https://github.com/Arthur-D/NativMix/issues).
