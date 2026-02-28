NativMix is a modern, hardware-based volume mixer for Linux, built with PyQt6. Designed as a contemporary alternative to deej, it connects physical Arduino potentiometers via USB directly to the modern PipeWire audio stack.
Through intelligent /proc/PID analysis, NativMix flawlessly resolves the true names and icons of sandboxed Electron and Chromium apps (like Discord, Spotify, or Chrome) and maps them to your physical sliders. Featuring native XDG Desktop Portal integration, the GUI dynamically adapts to your system theme (dark mode, accent colors) – providing a seamless, fast, and fully Wayland-secure experience.

### System Dependencies (Arch Linux)
```bash
sudo pacman -S python-pipx python-pyqt6 pipewire-pulse python-pyserial
```
