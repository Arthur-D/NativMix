# Permissions & Hardware Access

## Automatic (Recommended)

For **Arch Linux** and **openSUSE**, the packages include a custom udev rule (`99-nativmix-arduino.rules`) that grants the logged-in user permission via the `uaccess` tag automatically.

To reload rules after a manual install:
```bash
sudo udevadm control --reload-rules && sudo udevadm trigger
```

## Manual Fallback

If the automatic setup fails, add your user to the appropriate groups and restart your session:

| Distro | Command |
| :--- | :--- |
| Arch Linux / CachyOS | `sudo usermod -aG uucp,audio $USER` |
| openSUSE | `sudo usermod -aG dialout,audio $USER` |
| Fedora / Ubuntu / Debian | `sudo usermod -aG dialout,audio $USER` |

> [!IMPORTANT]
> You must log out and log back in for group changes to take effect.
