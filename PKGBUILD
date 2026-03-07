# Maintainer: Christian Möllmann (knoelliX) <moellix@knoellix.net>
pkgname=nativmix
pkgver=1.0.0
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
install=nativmix.install
source=()
sha256sums=()

# NOTE: Do NOT run makepkg with -C flag!
# makepkg's $srcdir defaults to $startdir/src/ which is our Python
# source directory. The -C flag would delete all source code.

prepare() {
    cd "$startdir"
    # Satisfy makepkg's internal requirement for a 'src' directory
    mkdir -p src
    # Clean build artifacts
    rm -rf build dist lib/nativmix.egg-info
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
}

build() {
    cd "$startdir"
    export PIP_NO_CACHE_DIR=1
    /usr/bin/python -m build --wheel --no-isolation

    # ── Verify the wheel contains current code ──
    echo "==> Verifying wheel contents..."
    /usr/bin/python -c "
import zipfile, sys, pathlib
whl = list(pathlib.Path('dist').glob('*.whl'))[0]
with zipfile.ZipFile(whl) as z:
    names = z.namelist()
    if any(n.startswith('nativmix/main.py') for n in names):
        print('  ✓ main.py found in wheel')
        content = z.read('nativmix/main.py').decode()
        if 'nativmix loaded from' in content:
            print('  ✓ main.py contains debug marker (FRESH build)')
        else:
            print('  ✗ main.py is STALE – debug marker missing!', file=sys.stderr)
            sys.exit(1)
    else:
        print('  ✗ main.py NOT FOUND in wheel!', file=sys.stderr)
        print('Found files: ' + str(names[:10]) + ('...' if len(names) > 10 else ''), file=sys.stderr)
        sys.exit(1)
"
}

package() {
    cd "$startdir"

    # Install the Python wheel system-wide
    /usr/bin/python -m installer --destdir="$pkgdir" dist/*.whl

    # Application assets (icons used at runtime via paths.py)
    install -Dm644 assets/icon.png \
        "$pkgdir/usr/share/nativmix/assets/icon.png"
    install -Dm644 assets/icon.svg \
        "$pkgdir/usr/share/nativmix/assets/icon.svg"

    # Desktop entry
    install -Dm644 data/nativmix.desktop \
        "$pkgdir/usr/share/applications/nativmix.desktop"

    # Scalable icon (SVG) for icon themes
    install -Dm644 assets/icon.svg \
        "$pkgdir/usr/share/icons/hicolor/scalable/apps/nativmix.svg"

    # Pixel icon (48x48 PNG fallback) for icon themes
    install -Dm644 assets/icon.png \
        "$pkgdir/usr/share/icons/hicolor/48x48/apps/nativmix.png"

    # License
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
