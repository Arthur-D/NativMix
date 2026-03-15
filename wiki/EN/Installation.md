# Installation

## Supported Operating Systems

| OS | Status | Installation Method |
| :--- | :--- | :--- |
| **Arch Linux / CachyOS** | **Stable** | `paru -S nativmix` (AUR) |
| **openSUSE Tumbleweed** | **Stable** | `zypper install nativmix` (via OBS) |
| **Fedora / Ubuntu / Debian** | *Testing* | Manual build from source |
| **SteamOS / Windows** | *Planned* | Development in progress |

---

## Arch Linux / CachyOS (AUR)

```bash
paru -S nativmix
# or
yay -S nativmix
```

---

## openSUSE Tumbleweed / Slowroll

```bash
sudo zypper addrepo https://download.opensuse.org/repositories/home:/knoelliX/openSUSE_Tumbleweed/ nativmix
sudo zypper refresh
sudo zypper install nativmix
# or
sudo zypper addrepo https://download.opensuse.org/repositories/home:/knoelliX/openSUSE_Slowroll/ nativmix
sudo zypper refresh
sudo zypper install nativmix
```

---

## Fedora / Nobara

```bash
sudo dnf config-manager --add-repo https://download.opensuse.org/repositories/home:/knoelliX/Fedora_43/home:knoelliX.repo
sudo dnf install nativmix
# or
sudo dnf config-manager --add-repo https://download.opensuse.org/repositories/home:/knoelliX/Fedora_42/home:knoelliX.repo
sudo dnf install nativmix
```

---

## Ubuntu 25.10 / 25.04 *(untested)*

```bash
echo 'deb http://download.opensuse.org/repositories/home:/knoelliX/xUbuntu_25.10/ /' | sudo tee /etc/apt/sources.list.d/home:knoelliX.list
curl -fsSL https://download.opensuse.org/repositories/home:/knoelliX/xUbuntu_25.10/Release.key | gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/home_knoelliX.gpg > /dev/null
sudo apt update
sudo apt install nativmix
# or
echo 'deb http://download.opensuse.org/repositories/home:/knoelliX/xUbuntu_25.04/ /' | sudo tee /etc/apt/sources.list.d/home:knoelliX.list
curl -fsSL https://download.opensuse.org/repositories/home:/knoelliX/xUbuntu_25.04/Release.key | gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/home_knoelliX.gpg > /dev/null
sudo apt update
sudo apt install nativmix
```

---

## Debian 13 / 12 / Raspberry Pi OS *(untested)*

```bash
echo 'deb http://download.opensuse.org/repositories/home:/knoelliX/Debian_13/ /' | sudo tee /etc/apt/sources.list.d/home:knoelliX.list
curl -fsSL https://download.opensuse.org/repositories/home:/knoelliX/Debian_13/Release.key | gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/home_knoelliX.gpg > /dev/null
sudo apt update
sudo apt install nativmix
# or
echo 'deb http://download.opensuse.org/repositories/home:/knoelliX/Debian_12/ /' | sudo tee /etc/apt/sources.list.d/home:knoelliX.list
curl -fsSL https://download.opensuse.org/repositories/home:/knoelliX/Debian_12/Release.key | gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/home_knoelliX.gpg > /dev/null
sudo apt update
sudo apt install nativmix
```

---

## Dependencies

`python-pyqt6`, `python-pulsectl`, `python-pyserial`, `python-setproctitle`, `python-mido`, `python-rtmidi`

[![OBS Build Status](https://build.opensuse.org/projects/home:knoelliX/packages/nativmix/badge.svg)](https://build.opensuse.org/package/show/home:knoelliX/nativmix)
