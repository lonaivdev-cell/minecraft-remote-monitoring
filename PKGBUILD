# Maintainer: CarborioLand <lorenzods.ls1@gmail.com>
# Local/dev PKGBUILD: build straight from this checkout with `makepkg -si`.
pkgname=mcctl
pkgver=0.2.0
pkgrel=1
pkgdesc="Remote control & monitoring for a modded Minecraft server over SSH (tmux + ServerStarterJar launch model)"
arch=('any')
url="https://github.com/lonaivdev-cell/minecraft-remote-monitoring"
license=('MIT')
depends=('python' 'python-rich' 'openssh' 'rsync')
makedepends=('python-build' 'python-installer' 'python-wheel' 'python-setuptools')
checkdepends=('python-pytest' 'tmux')
optdepends=(
    'libnotify: desktop notifications from the watchdog'
    'zstd: local verification of pulled backup archives'
    'tmux: integration tests / local transport mode'
    'python-gobject: GTK desktop app (mcctl-gui)'
    'python-anthropic: AI log/crash/mod analysis (mcctl ai)'
    'gtk4: GTK desktop app (mcctl-gui)'
    'libadwaita: GTK desktop app (mcctl-gui)'
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
    install -Dm644 systemd/mcctl-watchdog.service \
        "$pkgdir/usr/lib/systemd/user/mcctl-watchdog.service"
    install -Dm644 systemd/mcctl-autosave.service \
        "$pkgdir/usr/lib/systemd/user/mcctl-autosave.service"
    install -Dm644 systemd/mcctl-autosave.timer \
        "$pkgdir/usr/lib/systemd/user/mcctl-autosave.timer"
    install -Dm644 systemd/mcctl-backup.service \
        "$pkgdir/usr/lib/systemd/user/mcctl-backup.service"
    install -Dm644 systemd/mcctl-backup.timer \
        "$pkgdir/usr/lib/systemd/user/mcctl-backup.timer"
    install -Dm644 completions/mcctl.fish \
        "$pkgdir/usr/share/fish/vendor_completions.d/mcctl.fish"
    install -Dm644 data/io.github.lonaivdev_cell.mcctl.desktop \
        "$pkgdir/usr/share/applications/io.github.lonaivdev_cell.mcctl.desktop"
    install -Dm644 data/icons/io.github.lonaivdev_cell.mcctl.svg \
        "$pkgdir/usr/share/icons/hicolor/scalable/apps/io.github.lonaivdev_cell.mcctl.svg"
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
    install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
}
