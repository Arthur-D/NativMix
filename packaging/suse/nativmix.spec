Name:           nativmix
Version:        1.0.3
Release:        0
Summary:        Hardware-assisted volume mixer for PipeWire/PulseAudio
License:        GPL-3.0-or-later
URL:            https://github.com/knoellix/NativMix
Source0:        nativmix_%{version}.orig.tar.gz
Source1:        mido-1.3.2.tar.gz
# NativMix udev rules for Arduino-based hardware controllers
Source2:        99-nativmix-arduino.rules
BuildArch:      noarch
BuildRequires:  hicolor-icon-theme

%if 0%{?fedora}
# Fedora specific requirements
Requires:       python3-pyqt6, python3-pyserial, python3-python-rtmidi, python3-setproctitle, python3-packaging, python3-pulsectl
Requires:       libnotify, qt6-qtwayland
%endif

%if 0%{?suse_version}
# openSUSE specific requirements
Requires:       python3-qt6, python3-pyserial, python3-python-rtmidi, python3-setproctitle, python3-packaging, python3-pulsectl
Requires:       qt6-wayland, libQt6Widgets6, libnotify-tools
%endif

%description
Hardware-assisted volume mixer with Arduino and MIDI support.

%prep
# Extract Source0 and Source1 (-a 1) into the build directory
%setup -q -n NativMix-%{version} -a 1

%install
mkdir -p %{buildroot}%{_datadir}/%{name}
cp -r * %{buildroot}%{_datadir}/%{name}/
cp -r %{buildroot}%{_datadir}/%{name}/mido-1.3.2/mido %{buildroot}%{_datadir}/%{name}/lib/
rm -rf %{buildroot}%{_datadir}/%{name}/mido-1.3.2
rm -rf %{buildroot}%{_datadir}/%{name}/pkg %{buildroot}%{_datadir}/%{name}/src
rm -f %{buildroot}%{_datadir}/%{name}/PKGBUILD %{buildroot}%{_datadir}/%{name}/nativmix.install

# Desktop Integration
mkdir -p %{buildroot}%{_datadir}/applications
install -m 0644 %{buildroot}%{_datadir}/%{name}/data/nativmix.desktop %{buildroot}%{_datadir}/applications/

# Hicolor Icon Integration
mkdir -p %{buildroot}%{_datadir}/icons/hicolor/scalable/apps
mkdir -p %{buildroot}%{_datadir}/icons/hicolor/256x256/apps
install -m 0644 %{buildroot}%{_datadir}/%{name}/assets/icon.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{name}.svg
install -m 0644 %{buildroot}%{_datadir}/%{name}/assets/icon.png %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/%{name}.png

# Hardware Rules
mkdir -p %{buildroot}%{_sysconfdir}/udev/rules.d/
install -m 0644 %{SOURCE2} %{buildroot}%{_sysconfdir}/udev/rules.d/99-nativmix-arduino.rules

# Wrapper Script
mkdir -p %{buildroot}%{_bindir}
cat <<EOF > %{buildroot}%{_bindir}/%{name}
#!/bin/sh
export PYTHONPATH="%{_datadir}/%{name}/lib:\${PYTHONPATH}"
cd %{_datadir}/%{name}
exec python3 %{_datadir}/%{name}/lib/%{name}/main.py "\$@"
EOF
chmod 755 %{buildroot}%{_bindir}/%{name}

%post
# Reload udev after installation
/usr/bin/udevadm control --reload-rules || :
/usr/bin/udevadm trigger || :

%postun
# Reload udev after removal
/usr/bin/udevadm control --reload-rules || :
/usr/bin/udevadm trigger || :

%files
%defattr(-,root,root)
# Binary
%{_bindir}/%{name}

# Main Application Data (Modular Path)
%dir %{_datadir}/%{name}
%{_datadir}/%{name}/

# System Integration
%{_datadir}/applications/%{name}.desktop
%{_datadir}/icons/hicolor/scalable/apps/%{name}.svg
%{_datadir}/icons/hicolor/256x256/apps/%{name}.png

# Hardware Rules
%config(noreplace) %{_sysconfdir}/udev/rules.d/99-nativmix-arduino.rules

# Documentation
%license LICENSE
%doc README.md

%changelog
* Wed Mar 11 2026 Christian Möllmann <moellix@knoellix.net> - 1.0.3-0
- Bump version to 1.0.3
- Fix: NativeMix hardware access via udev rules
- Fix: High-DPI scaling and pixel rounding
- Modularized installation paths for openSUSE
- Cleaned up informational logs (INFO -> DEBUG)

* Wed Mar 11 2026 Christian Möllmann <moellix@knoellix.net> - 1.0.2-15
- Integrated hardware permission rules via udev (uaccess)
- Added udevadm reload/trigger to post and postun sections

* Tue Mar 10 2026 Christian Möllmann <moellix@knoellix.net> - 1.0.2-14
- Fixed V-Sink routing and ghost apps in tooltip
- Added HighDpiScaleFactorRoundingPolicy

* Mon Mar 09 2026 Christian Möllmann <moellix@knoellix.net> - 1.0.2-12
- Synchronized Fedora and SUSE dependencies (libnotify, wayland)