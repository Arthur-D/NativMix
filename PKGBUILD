# Maintainer: Christian Möllmann (knoelliX) <moellix@knoellix.net>
pkgname=nativmix
pkgver=1.0.3
pkgrel=1
pkgdesc="Hardware-assisted volume mixer for PipeWire/PulseAudio with Arduino support"
arch=('any')
url="https://github.com/knoelliX/NativMix"
license=('GPL-3.0-or-later')

depends=(
    'python'
    'python-pyqt6'
    'python-pulsectl'
    'python-pyserial'
    'python-setproctitle'
    'python-mido'
    'python-rtmidi'
)

makedepends=(
    'python-build'
    'python-installer'
    'python-setuptools'
    'python-wheel'
)

optdepends=(
    'kvantum: Plasma transparency and blur engine support'
)

install="${startdir}/nativmix.install"

# This URL is dynamic for AUR/Actions. 
# For local building, you can still use your local files.
source=("${pkgname}-${pkgver}.tar.gz::https://github.com/knoelliX/NativMix/archive/refs/tags/v${pkgver}.tar.gz")
sha256sums=('SKIP') # Will be updated by GitHub Action

prepare() {
    # If we are in a git repo (local build), we stay there.
    # If not, we go into the extracted source folder.
    if [ -d "$srcdir/${pkgname}-${pkgver}" ]; then
        cd "$srcdir/${pkgname}-${pkgver}"
    else
        cd "$srcdir/.."
    fi

    # Clean previous build artifacts
    rm -rf dist/ build/ lib/*.egg-info .eggs/
    find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
}

build() {
    if [ -d "$srcdir/${pkgname}-${pkgver}" ]; then
        cd "$srcdir/${pkgname}-${pkgver}"
    else
        cd "$srcdir/.."
    fi

    export PIP_NO_CACHE_DIR=1
    python -m build --wheel --no-isolation
}

package() {
    if [ -d "$srcdir/${pkgname}-${pkgver}" ]; then
        cd "$srcdir/${pkgname}-${pkgver}"
    else
        cd "$srcdir/.."
    fi

    # 1. Install the Python wheel
    python -m installer --destdir="$pkgdir" dist/*.whl

    # 2. Desktop entry
    install -Dm644 data/nativmix.desktop \
        "$pkgdir/usr/share/applications/nativmix.desktop"

    # 3. Hardware Access (udev rules)
    install -Dm644 data/udev/99-nativmix-arduino.rules \
        "$pkgdir/usr/lib/udev/rules.d/99-nativmix-arduino.rules"

    # 4. System Icons
    install -Dm644 assets/icon.svg \
        "$pkgdir/usr/share/icons/hicolor/scalable/apps/nativmix.svg"
    install -Dm644 assets/icon.png \
        "$pkgdir/usr/share/icons/hicolor/256x256/apps/nativmix.png"

    # 5. Application assets for runtime
    install -d "$pkgdir/usr/share/nativmix/assets"
    install -m644 assets/icon.png "$pkgdir/usr/share/nativmix/assets/icon.png"
    install -m644 assets/icon.svg "$pkgdir/usr/share/nativmix/assets/icon.svg"

    # 6. License
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
