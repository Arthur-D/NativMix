Name:           nativmix
Version:        0
Release:        0
Summary:        Hardware-based PipeWire volume & MIDI mixer for Wayland/X11
License:        GPL-3.0-or-later
URL:            https://github.com/knoelliX/NativMix

Source0:        nativmix-%{version}.tar.gz
Source1:        mido-1.3.2.tar.gz

BuildArch:      noarch
BuildRequires:  hicolor-icon-theme
BuildRequires:  desktop-file-utils

# Prevent Fedora's auto-dependency scanner from generating python3dist(mido)
# for the bundled lib — mido is not in Fedora repos and is shipped inline.
%global __requires_exclude_from ^%{_datadir}/%{name}/.*$
%global __provides_exclude_from ^%{_datadir}/%{name}/.*$

# Fedora/Nobara — portmidi C library is in official repos; python3-rtmidi is not
# mido.backends.portmidi uses ctypes to load libportmidi.so — no Python binding needed
Requires:       python3-pyqt6
Requires:       python3-pyserial
Requires:       portmidi
Requires:       python3-setproctitle
Requires:       python3-packaging
Requires:       python3-pulsectl
Requires:       libnotify
Requires:       qt6-qtwayland

%description
Hardware-assisted volume mixer with Arduino and MIDI support.
Controls physical inputs, virtual sinks, and MIDI devices. (Modern deej alternative)

%prep
%setup -q -n NativMix-%{version}
# Extract mido into the source tree so cp -r * picks it up automatically
tar -xf %{SOURCE1}

%install
# 1. Setup Modular Application Directory
mkdir -p %{buildroot}%{_datadir}/%{name}
cp -r * %{buildroot}%{_datadir}/%{name}/

# 2. Bundle mido into lib/
mkdir -p %{buildroot}%{_datadir}/%{name}/lib
if [ -d "%{buildroot}%{_datadir}/%{name}/mido-1.3.2/mido" ]; then
    cp -r %{buildroot}%{_datadir}/%{name}/mido-1.3.2/mido %{buildroot}%{_datadir}/%{name}/lib/
fi

# 2a. Patch bundled portmidi_init.py to use find_library instead of hardcoded
#     'libportmidi.so' — Fedora only installs the versioned .so (libportmidi.so.0)
#     at runtime; the unversioned symlink requires portmidi-devel.
_PM_INIT="%{buildroot}%{_datadir}/%{name}/lib/mido/backends/portmidi_init.py"
if [ -f "$_PM_INIT" ]; then
    sed -i "s|dll_name = 'libportmidi.so'|import ctypes.util as _cu; dll_name = _cu.find_library('portmidi') or 'libportmidi.so'|" "$_PM_INIT"
fi

# 3. Systemd User Service
mkdir -p %{buildroot}%{_userunitdir}
if [ -f "%{buildroot}%{_datadir}/%{name}/packaging/nativmix.service" ]; then
    install -m 0644 %{buildroot}%{_datadir}/%{name}/packaging/nativmix.service %{buildroot}%{_userunitdir}/nativmix.service
fi

# 4. Cleanup
rm -rf %{buildroot}%{_datadir}/%{name}/mido-1.3.2
rm -rf %{buildroot}%{_datadir}/%{name}/packaging
rm -rf %{buildroot}%{_datadir}/%{name}/src
rm -rf %{buildroot}%{_datadir}/%{name}/pkg
rm -f %{buildroot}%{_datadir}/%{name}/PKGBUILD
rm -f %{buildroot}%{_datadir}/%{name}/nativmix.install

# 5. Desktop & Autostart
mkdir -p %{buildroot}%{_datadir}/applications
install -m 0644 data/nativmix.desktop %{buildroot}%{_datadir}/applications/

mkdir -p %{buildroot}%{_sysconfdir}/xdg/autostart
install -m 0644 data/nativmix.desktop %{buildroot}%{_sysconfdir}/xdg/autostart/

# 6. Icons
mkdir -p %{buildroot}%{_datadir}/icons/hicolor/scalable/apps
mkdir -p %{buildroot}%{_datadir}/icons/hicolor/256x256/apps
install -m 0644 assets/icon.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{name}.svg
install -m 0644 assets/icon.png %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/%{name}.png

# 7. Hardware Rules
mkdir -p %{buildroot}%{_udevrulesdir}
install -m 0644 data/udev/99-nativmix-arduino.rules %{buildroot}%{_udevrulesdir}/99-nativmix-arduino.rules

# 8. Wrapper Script
mkdir -p %{buildroot}%{_bindir}
cat <<EOF > %{buildroot}%{_bindir}/%{name}
#!/bin/bash
export PYTHONPATH="%{_datadir}/%{name}:%{_datadir}/%{name}/lib:\${PYTHONPATH}"
exec python3 -m nativmix.main "\$@"
EOF

# Normalize Shebangs
find %{buildroot}%{_bindir} -type f -exec sed -i '1s|#!.*python.*|#!/usr/bin/python3|' {} +
find %{buildroot}%{_datadir}/%{name} -type f -name "*.py" -exec sed -i '1s|#!.*python.*|#!/usr/bin/python3|' {} +

chmod 755 %{buildroot}%{_bindir}/%{name}

%post
if [ $1 -eq 1 ] || [ $1 -eq 2 ]; then
    /usr/bin/udevadm control --reload-rules >/dev/null 2>&1 || :
    /usr/bin/udevadm trigger --subsystem-match=tty >/dev/null 2>&1 || :
fi
/usr/bin/update-desktop-database -q %{_datadir}/applications || :
/usr/bin/gtk-update-icon-cache -q -t -f %{_datadir}/icons/hicolor || :

%postun
if [ $1 -eq 0 ]; then
    /usr/bin/udevadm control --reload-rules >/dev/null 2>&1 || :
fi
/usr/bin/update-desktop-database -q %{_datadir}/applications || :
/usr/bin/gtk-update-icon-cache -q -t -f %{_datadir}/icons/hicolor || :

%files
%defattr(-,root,root)
%{_bindir}/%{name}
%{_datadir}/%{name}/
%{_datadir}/applications/%{name}.desktop
%config %{_sysconfdir}/xdg/autostart/%{name}.desktop
%{_datadir}/icons/hicolor/scalable/apps/%{name}.svg
%{_datadir}/icons/hicolor/256x256/apps/%{name}.png
%{_userunitdir}/nativmix.service
%{_udevrulesdir}/99-nativmix-arduino.rules
%license LICENSE
%doc README.md

%changelog
* Tue Mar 17 2026 NativMix <noreply@github.com> - 1.0.6-1
- Local build
