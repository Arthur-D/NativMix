Name:           nativmix
# OBS set_version service will replace '0' with your Git Tag (e.g., 1.0.4)
Version:        0
# OBS will also manage the release number automatically
Release:        0
Summary:        Hardware-based PipeWire volume & MIDI mixer for Wayland/X11
License:        GPL-3.0-or-later
URL:            https://github.com/Arthur-D/NativMix
Packager:       Christian Möllmann <moellix@knoellix.net>

# Standard OBS/RPM source naming
Source0:        nativmix-%{version}.tar.gz
Source1:        mido-1.3.2.tar.gz

BuildArch:      noarch
BuildRequires:  hicolor-icon-theme

%if 0%{?fedora}
Requires:       python3-pyqt6, python3-pyserial, python3-python-rtmidi, python3-setproctitle, python3-packaging, python3-pulsectl
Requires:       libnotify, qt6-qtwayland
%endif

%if 0%{?suse_version}
Requires:       python3-qt6, python3-pyserial, python3-python-rtmidi, python3-setproctitle, python3-packaging, python3-pulsectl
Requires:       qt6-wayland, libQt6Widgets6, libnotify-tools
%endif

%description
Hardware-assisted volume mixer with Arduino and MIDI support.
Controls physical inputs, virtual sinks, and MIDI devices. (Modern deej alternative)

%prep
# The directory name inside the tarball must match what tar_scm generates
%setup -q -n NativMix-%{version} -a 1

%install
# 1. Setup Modular Application Directory
mkdir -p %{buildroot}%{_datadir}/%{name}
cp -r * %{buildroot}%{_datadir}/%{name}/

# 2. Move bundled mido library
mkdir -p %{buildroot}%{_datadir}/%{name}/lib
if [ -d "%{buildroot}%{_datadir}/%{name}/mido-1.3.2/mido" ]; then
    cp -r %{buildroot}%{_datadir}/%{name}/mido-1.3.2/mido %{buildroot}%{_datadir}/%{name}/lib/
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
/usr/bin/udevadm control --reload-rules || :
/usr/bin/udevadm trigger || :
/usr/bin/update-desktop-database -q %{_datadir}/applications || :
/usr/bin/gtk-update-icon-cache -q -t -f %{_datadir}/icons/hicolor || :

%postun
/usr/bin/udevadm control --reload-rules || :
/usr/bin/udevadm trigger || :
/usr/bin/update-desktop-database -q %{_datadir}/applications || :
/usr/bin/gtk-update-icon-cache -q -t -f %{_datadir}/icons/hicolor || :

%files
%defattr(-,root,root)
%{_bindir}/%{name}
%{_datadir}/%{name}/
%{_datadir}/applications/%{name}.desktop
%{_sysconfdir}/xdg/autostart/%{name}.desktop
%{_datadir}/icons/hicolor/scalable/apps/%{name}.svg
%{_datadir}/icons/hicolor/256x256/apps/%{name}.png
%{_userunitdir}/nativmix.service
%{_udevrulesdir}/99-nativmix-arduino.rules
%license LICENSE
%doc README.md

%changelog
# Managed by OBS