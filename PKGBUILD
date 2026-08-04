# Maintainer: CarborioLand <lorenzods.ls1@gmail.com>
# Local/dev PKGBUILD: build straight from this checkout with `makepkg -si`.
pkgname=lulism
pkgver=2.0.0
pkgrel=1
pkgdesc="Legit Ultimate Linux Server Monitor — remote control & monitoring for game servers and hosts over SSH"
arch=('any')
url="https://github.com/lonaivdev-cell/minecraft-remote-monitoring"
license=('MIT')
provides=('mcctl')
replaces=('mcctl')
conflicts=('mcctl')
depends=('python' 'python-rich' 'openssh' 'rsync')
makedepends=('python-build' 'python-installer' 'python-wheel' 'python-setuptools')
checkdepends=('python-pytest' 'tmux')
optdepends=(
    'libnotify: desktop notifications from the watchdog'
    'zstd: local verification of pulled backup archives'
    'tmux: integration tests / local transport mode'
    'python-gobject: GTK desktop app (lulism-gui)'
    'python-anthropic: AI analysis & chat via Claude (lulism ai; or use a local ollama instead)'
    'ollama: local LLM backend for AI analysis & chat ([llm].provider = "ollama")'
    'gtk4: GTK desktop app (lulism-gui)'
    'libadwaita: GTK desktop app (lulism-gui)'
)

build() {
    cd "$startdir"
    python -m build --wheel --no-isolation --outdir "$srcdir/dist"
}

check() {
    cd "$startdir"
    python -m pytest -q
}

package() {
    cd "$startdir"
    python -m installer --destdir="$pkgdir" "$srcdir"/dist/*.whl
    install -Dm644 src/lulism/units/lulism-watchdog.service \
        "$pkgdir/usr/lib/systemd/user/lulism-watchdog.service"
    install -Dm644 src/lulism/units/lulism-autosave.service \
        "$pkgdir/usr/lib/systemd/user/lulism-autosave.service"
    install -Dm644 src/lulism/units/lulism-autosave.timer \
        "$pkgdir/usr/lib/systemd/user/lulism-autosave.timer"
    install -Dm644 src/lulism/units/lulism-backup.service \
        "$pkgdir/usr/lib/systemd/user/lulism-backup.service"
    install -Dm644 src/lulism/units/lulism-backup.timer \
        "$pkgdir/usr/lib/systemd/user/lulism-backup.timer"
    install -Dm644 src/lulism/units/lulism-metrics.service \
        "$pkgdir/usr/lib/systemd/user/lulism-metrics.service"
    install -Dm644 src/lulism/units/lulism-metrics.timer \
        "$pkgdir/usr/lib/systemd/user/lulism-metrics.timer"
    install -Dm644 completions/lulism.fish \
        "$pkgdir/usr/share/fish/vendor_completions.d/lulism.fish"
    install -Dm644 data/io.github.lonaivdev_cell.lulism.desktop \
        "$pkgdir/usr/share/applications/io.github.lonaivdev_cell.lulism.desktop"
    install -Dm644 data/icons/io.github.lonaivdev_cell.lulism.svg \
        "$pkgdir/usr/share/icons/hicolor/scalable/apps/io.github.lonaivdev_cell.lulism.svg"
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
    install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
}
