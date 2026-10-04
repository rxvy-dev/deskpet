# Maintainer: rxvy-dev <https://github.com/rxvy-dev>
pkgname=deskpet
pkgver=3.1
pkgrel=1
pkgdesc="Little desktop pets for Wayland - they climb your windows, build things, have moods and talk (local LLM)"
arch=('any')
url="https://github.com/rxvy-dev/deskpet"
license=('MIT')
depends=('python' 'python-gobject' 'python-cairo' 'gtk3' 'gtk-layer-shell')
optdepends=('python-pillow: regenerate the built-in sprites (tools/gen_sprites.py)'
            'ollama-cuda: talk to your pets with a local LLM (or ollama / ollama-rocm)')
source=("$pkgname-$pkgver.tar.gz::$url/archive/refs/tags/v$pkgver.tar.gz")
sha256sums=('SKIP')

package() {
    cd "$srcdir/$pkgname-$pkgver"
    install -Dm755 deskpet.py "$pkgdir/usr/share/deskpet/deskpet.py"
    cp -r pets props "$pkgdir/usr/share/deskpet/"
    install -dm755 "$pkgdir/usr/bin"
    ln -s /usr/share/deskpet/deskpet.py "$pkgdir/usr/bin/deskpet"
    sed -e "s|@BIN@|/usr/bin/deskpet|g" -e "s|@ICON@|/usr/share/deskpet/pets/tux/idle_0.png|g" deskpet.desktop \
        | install -Dm644 /dev/stdin "$pkgdir/usr/share/applications/deskpet.desktop"
    install -Dm644 README.md "$pkgdir/usr/share/doc/deskpet/README.md"
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/deskpet/LICENSE"
}
