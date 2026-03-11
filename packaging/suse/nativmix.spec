Name:           nativmix
Version:        1.0.2
Release:        14
Summary:        Hardware-assisted volume mixer for PipeWire/PulseAudio
License:        GPL-3.0-or-later
URL:            https://github.com/knoellix/NativMix
Source0:        nativmix_1.0.2.orig.tar.gz
Source1:        mido-1.3.2.tar.gz
# NativMix udev rules for Arduino-based hardware controllers
Source2:        99-nativmix-arduino.rules
BuildArch:      noarch

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
mkdir -p %{buildroot}/usr/share/nativmix
cp -r * %{buildroot}/usr/share/nativmix/
cp -r %{buildroot}/usr/share/nativmix/mido-1.3.2/mido %{buildroot}/usr/share/nativmix/lib/
rm -rf %{buildroot}/usr/share/nativmix/mido-1.3.2
rm -rf %{buildroot}/usr/share/nativmix/pkg %{buildroot}/usr/share/nativmix/src
rm -f %{buildroot}/usr/share/nativmix/PKGBUILD %{buildroot}/usr/share/nativmix/nativmix.install

# Desktop Integration
mkdir -p %{buildroot}/usr/share/applications %{buildroot}/usr/share/pixmaps
install -m 0644 %{buildroot}/usr/share/nativmix/data/nativmix.desktop %{buildroot}/usr/share/applications/
install -m 0644 %{buildroot}/usr/share/nativmix/assets/icon.svg %{buildroot}/usr/share/pixmaps/nativmix.svg

# udev rules for openSUSE
mkdir -p %{buildroot}%{_sysconfdir}/udev/rules.d/
install -m 0644 %{SOURCE2} %{buildroot}%{_sysconfdir}/udev/rules.d/99-nativmix-arduino.rules

# Wrapper Script
mkdir -p %{buildroot}/usr/bin
cat <<EOF > %{buildroot}/usr/bin/nativmix
#!/bin/sh
export PYTHONPATH="/usr/share/nativmix/lib:${PYTHONPATH}"
cd /usr/share/nativmix
exec python3 /usr/share/nativmix/lib/nativmix/main.py "$@"
EOF
chmod 755 %{buildroot}/usr/bin/nativmix

%post
# Reload udev after installation
/usr/bin/udevadm control --reload-rules || :
/usr/bin/udevadm trigger || :

%postun
# Reload udev after removal
/usr/bin/udevadm control --reload-rules || :
/usr/bin/udevadm trigger || :

%files
/usr/share/nativmix
/usr/bin/nativmix
/usr/share/applications/nativmix.desktop
/usr/share/pixmaps/nativmix.svg
%{_sysconfdir}/udev/rules.d/99-nativmix-arduino.rules

%changelog
* Wed Mar 11 2026 knoelliX <deine@mail.de> - 1.0.2-15
- Integrated hardware permission rules via udev (uaccess)
- Added udevadm reload/trigger to post and postun sections

* Tue Mar 10 2026 knoelliX <deine@mail.de> - 1.0.2-14
- Fixed V-Sink routing and ghost apps in tooltip
- Added HighDpiScaleFactorRoundingPolicy

* Mon Mar 09 2026 knoelliX <deine@mail.de> - 1.0.2-12
- Synchronized Fedora and SUSE dependencies (libnotify, wayland)