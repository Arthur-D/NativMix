# Flatpak installation

The fork's Flatpak manifest is
[`flatpak/io.github.ArthurD.NativMix.yml`](../flatpak/io.github.ArthurD.NativMix.yml)
and uses the application ID `io.github.ArthurD.NativMix`.

The Flatpak builds `python-rtmidi` against ALSA and uses RtMidi for physical and
virtual MIDI ports. PortMidi is intentionally not bundled: its native polling
path can crash the process if a USB controller is unplugged. Existing
`--device=all` access covers the ALSA sequencer.

The optional **Remote Controller** mode uses direct multicast DNS plus
AppleMIDI/RTP-MIDI and revisioned JSON/TCP mixer synchronization on the local
network. It does not depend on a host `rtpmidid` process or Avahi D-Bus access:
the Flatpak bundles the maintained Python `zeroconf` library and uses the
existing `--share=network` permission. That permission allows network traffic
from inside the sandbox; NativMix opens the remote UDP and TCP sockets only
after the user selects Send or Receive. No additional filesystem, device, or
portal permission is needed for mirrored mixer state.

Send and Receive also request a suspend-only inhibitor through the XDG Desktop
Portal while remote mode is intentionally active, including waiting and
reconnecting. This is enabled by default and configurable in Settings. It does
not inhibit display blanking, display power saving, or screen locking, and needs
no additional Flatpak bus or filesystem permission. If suspend is allowed, USB
input cannot reliably wake the app or replay MIDI/network events received while
userspace was asleep.

## Downloaded release bundle

Each tagged [Arthur-D/NativMix release](https://github.com/Arthur-D/NativMix/releases)
provides a versioned, single-file `.flatpak` bundle. This is the portable option
for immutable distributions such as Bazzite and for systems where a native
package is not appropriate.

After downloading the bundle, install it once for the current user:

```bash
flatpak install --user ./io.github.ArthurD.NativMix-v1.1.0.flatpak
flatpak run io.github.ArthurD.NativMix
```

The exact filename follows the release tag shown on the download page.

A directly downloaded bundle is not a Flatpak repository remote. It therefore
does not receive new releases through `flatpak update`. To upgrade, download the
newer bundle from the fork's release page and run `flatpak install --user` on
that file again, accepting the update. Uninstalling first is normally
unnecessary, but `flatpak uninstall --user io.github.ArthurD.NativMix` followed
by a fresh install is also supported.

### Optional update notifications

Update checks are disabled by default. If you explicitly enable **Check GitHub
for updates** in Settings, NativMix contacts
`api.github.com/repos/Arthur-D/NativMix/releases/latest` once per app start. It
only announces a newer release and can open its release page; it does not
download or execute updates and sends no telemetry. The Flatpak network
permission also supports explicitly enabled local-network remote MIDI; it is
not used for a relay, cloud controller, NAT traversal, or telemetry.

### Trusted-LAN remote mixer

Remote controller discovery uses multicast UDP 5353 and each enabled NativMix
endpoint binds UDP 5004-5005. If a firewall blocks the link, allow those ports
only between the two trusted local machines. Do not add router port-forwarding
or Internet-facing firewall rules.

AppleMIDI/RTP-MIDI is not encrypted or authenticated. The discovered stable ID
helps NativMix reconnect to the laptop the user selected, but it is not a
cryptographic identity and does not make an untrusted network safe. V1 is IPv4
only and supports NativMix Control Change traffic rather than arbitrary MIDI
devices or manual addresses.

Full mixer synchronization is also unencrypted and unauthenticated. It exposes
receiver profile names, application and device labels, mappings, capabilities,
volumes, mutes, and typed edit commands to the LAN. A LAN participant may
observe or spoof this data. Use it only on a trusted local network; never add
router port forwarding or Internet-facing rules.

On the receiver, select **Receive controller** and explicitly enable **Allow
connected laptop to view and edit mixer profiles** before connecting the
discovered sender. On the sender, select **Send controller** and a physical MIDI
device. Once connected, the sender requests a fresh canonical snapshot and
shows **Controlling _receiver_ - _profile_**. Receiver profile and mixer
operations are then available through the mirrored GUI. The receiver remains
the only source of truth: pending sender controls commit only after canonical
publication and acknowledgement, while stale revisions, disconnects, hash
failures, and receiver restarts trigger a fresh snapshot.

The sender's mirrored state is memory-only and does not modify its profile
files, local mappings, or audio backend. Leaving Send mode restores its local
mixer immediately. If the UI shows **Permission disabled**, enable the receiver
checkbox above. If it shows **Version incompatible**, update both systems to
compatible builds. AppleMIDI remains operational when full mixer sync is
unavailable.

## Native packages

The [`nativmix` AUR package](https://aur.archlinux.org/packages/nativmix) is
maintained upstream by [`knoelliX`](https://github.com/knoellix/NativMix), as
are the native packages documented in the
[upstream installation guide](https://github.com/knoellix/NativMix/wiki/EN-Installation).
They follow upstream releases and do not contain Arthur-D fork v1.1.0 features
unless upstream adopts them. Use this fork's downloaded `.flatpak` bundle or a
source/local build when those fork features are required.
The GitHub update-check setting is hidden for native Linux packages to avoid
conflicting with package-manager updates.

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

## v1.1.0 release checklist

1. Merge the release-preparation pull request, then update a clean local `main`:
   `git switch main && git pull --ff-only origin main`.
2. Confirm `python -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"`
   prints `1.1.0`. Create and push the release tag:
   `git tag -a v1.1.0 -m "NativMix v1.1.0"` followed by
   `git push origin v1.1.0`.
3. Watch **Build Windows Installer** and **Build Flatpak Bundle**. The Windows
   workflow creates or reuses the single `v1.1.0` GitHub Release; the Flatpak
   workflow waits for that release and attaches
   `io.github.ArthurD.NativMix-v1.1.0.flatpak`.
4. Verify the release contains exactly one
   `NativMix-1.1.0-Setup.exe` and one
   `io.github.ArthurD.NativMix-v1.1.0.flatpak`, both built from the tag.
5. Download the Flatpak asset, verify its exported version with
   `flatpak info --show-version io.github.ArthurD.NativMix` after installation,
   and smoke-test launch before advertising the bundle.

The Arthur-D fork does not publish the upstream-owned `nativmix` AUR package.
No AUR key, checksum, or publication step belongs in this release process.
