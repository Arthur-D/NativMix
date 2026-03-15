# Installation

## Unterstützte Betriebssysteme

| Betriebssystem | Status | Installationsmethode |
| :--- | :--- | :--- |
| **Arch Linux / CachyOS** | **Stabil** | `paru -S nativmix` (AUR) |
| **openSUSE Tumbleweed** | **Stabil** | `zypper install nativmix` (via OBS) |
| **Fedora / Ubuntu / Debian** | *Testing* | Manueller Build aus den Quellen |
| **SteamOS / Windows** | *Geplant* | Entwicklung läuft |

---

## Arch Linux / CachyOS (AUR)

```bash
paru -S nativmix
# oder
yay -S nativmix
```

---

## openSUSE Tumbleweed / Slowroll

```bash
sudo zypper addrepo https://download.opensuse.org/repositories/home:/knoelliX/openSUSE_Tumbleweed/ nativmix
sudo zypper refresh
sudo zypper install nativmix
# oder
sudo zypper addrepo https://download.opensuse.org/repositories/home:/knoelliX/openSUSE_Slowroll/ nativmix
sudo zypper refresh
sudo zypper install nativmix
```

---

## Fedora / Nobara

```bash
sudo dnf config-manager --add-repo https://download.opensuse.org/repositories/home:/knoelliX/Fedora_43/home:knoelliX.repo
sudo dnf install nativmix
# oder
sudo dnf config-manager --add-repo https://download.opensuse.org/repositories/home:/knoelliX/Fedora_42/home:knoelliX.repo
sudo dnf install nativmix
```

---

## Ubuntu 25.10 / 25.04 *(nicht getestet)*

```bash
echo 'deb http://download.opensuse.org/repositories/home:/knoelliX/xUbuntu_25.10/ /' | sudo tee /etc/apt/sources.list.d/home:knoelliX.list
curl -fsSL https://download.opensuse.org/repositories/home:/knoelliX/xUbuntu_25.10/Release.key | gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/home_knoelliX.gpg > /dev/null
sudo apt update
sudo apt install nativmix
# oder
echo 'deb http://download.opensuse.org/repositories/home:/knoelliX/xUbuntu_25.04/ /' | sudo tee /etc/apt/sources.list.d/home:knoelliX.list
curl -fsSL https://download.opensuse.org/repositories/home:/knoelliX/xUbuntu_25.04/Release.key | gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/home_knoelliX.gpg > /dev/null
sudo apt update
sudo apt install nativmix
```

---

## Debian 13 / 12 / Raspberry Pi OS *(nicht getestet)*

```bash
echo 'deb http://download.opensuse.org/repositories/home:/knoelliX/Debian_13/ /' | sudo tee /etc/apt/sources.list.d/home:knoelliX.list
curl -fsSL https://download.opensuse.org/repositories/home:/knoelliX/Debian_13/Release.key | gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/home_knoelliX.gpg > /dev/null
sudo apt update
sudo apt install nativmix
# oder
echo 'deb http://download.opensuse.org/repositories/home:/knoelliX/Debian_12/ /' | sudo tee /etc/apt/sources.list.d/home:knoelliX.list
curl -fsSL https://download.opensuse.org/repositories/home:/knoelliX/Debian_12/Release.key | gpg --dearmor | sudo tee /etc/apt/trusted.gpg.d/home_knoelliX.gpg > /dev/null
sudo apt update
sudo apt install nativmix
```

---

## Abhängigkeiten

`python-pyqt6`, `python-pulsectl`, `python-pyserial`, `python-setproctitle`, `python-mido`, `python-rtmidi`

[![OBS Build Status](https://build.opensuse.org/projects/home:knoelliX/packages/nativmix/badge.svg)](https://build.opensuse.org/package/show/home:knoelliX/nativmix)
