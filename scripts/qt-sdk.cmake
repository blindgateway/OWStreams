# -----------------------------------------------------------------------------
# Qt SDK bootstrap
#
# The development machine running this project does not have Qt installed in a
# standard location, and several Qt host tools must be kept in a private prefix
# so they do not collide with the distribution-provided copies in /usr/bin.
#
# This script builds a self-contained SDK under $QT_PREFIX using only dnf, sudo
# and rpm2cpio -- no network access beyond the configured dnf repositories and
# no manual compilation.  It is idempotent: if a component is already present it
# is skipped.
#
#   ./scripts/qt-sdk.sh              # install everything that is missing
#   ./scripts/qt-sdk.sh --print-env  # print the env for an already built SDK
#   ./scripts/qt-sdk.sh --force      # rebuild even if the SDK exists
#
# Everything lands in $QT_PREFIX (default: $HOME/.local/share/stream-qt-sdk) and
# is removable with `rm -rf $QT_PREFIX`.
# -----------------------------------------------------------------------------
set -euo pipefail

QT_PREFIX="${QT_PREFIX:-$HOME/.local/share/stream-qt-sdk}"
WORK_DIR="${QT_WORK_DIR:-$QT_PREFIX/.build"
LIBDIR="lib64"
STAGE="$QT_PREFIX/root"
SYSROOT="$QT_PREFIX/sysroot"
HOSTBIN="$QT_PREFIX/hostbin"
SYSCONF="$QT_PREFIX/sysconf"

# Host tools we cannot run from the distribution prefix because their matching
# runtimes differ from the ones inside the SDK.
HOST_TOOLS=(cmake ninja gmake gcc g++ c++ ld ar as ranlib moc rcc uic qmake qmake6 qmlscene qml qmltestrunner qmlplugindump qsb qmlcachegen)

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warn:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

need_root() { [ "$(id -u)" -eq 0 ] || die "must be run as root: sudo $0 $*"; }

download() {  # download <pkg-spec>
    local spec="$1"
    dnf -y download --resolve --alldeps --destdir "$WORK_DIR/rpms" "$spec" \
        || die "dnf download failed for '$spec'"
}

extract() {   # extract  -> unpacks every rpm in $WORK_DIR/rpms into $STAGE
    mkdir -p "$STAGE"
    shopt -s nullglob
    local rpms=("$WORK_DIR"/rpms/*.rpm)
    shopt -u nullglob
    [ ${#rpms[@]} -gt 0 ] || die "no rpm files to extract in $WORK_DIR/rpms"
    for rpm in "${rpms[@]}"; do
        rpm2cpio "$rpm" | cpio -idmu --quiet -D "$STAGE" 2>/dev/null \
            || rpm2cpio "$rpm" | (cd "$STAGE" && cpio -idm --quiet)
    done
}

relocate() { # relocate <subdir-under-files> <path...>  (rewrite shebangs/rpaths)
    local root="$STAGE/$1"; shift
    local path
    for path in "$@"; do
        [ -e "$root/$path" ] || continue
        "$SCRIPTDIR/relocate.py" "$root/$path"
    done
}

SCRIPTDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "${1:-}" = "--print-env" ]; then
    [ -d "$STAGE" ] || die "SDK not built yet at $STAGE"
    printf 'export QT_PREFIX=%q\n' "$QT_PREFIX"
    printf 'export CMAKE_PREFIX_PATH=%q\n' "$STAGE:$STAGE/$LIBDIR/cmake"
    printf 'export LD_LIBRARY_PATH=%q\n' "$STAGE/$LIBDIR"
    printf 'export PKG_CONFIG_PATH=%q\n' "$STAGE/$LIBDIR/pkgconfig"
    printf 'export PATH=%s:$PATH\n' "$HOSTBIN"
    exit 0
fi

[ "${1:-}" = "--force" ] && rm -rf "$QT_PREFIX"

mkdir -p "$WORK_DIR/rpms" "$HOSTBIN" "$SYSCONF"

# ---- Qt 6 SDK -------------------------------------------------------------
if [ ! -d "$STAGE/usr/$LIBDIR/cmake/Qt6" ]; then
    log "Downloading Qt 6 development packages"
    for p in qt6-qtbase-devel qt6-qtdeclarative-devel qt6-qtmultimedia-devel \
             qt6-qtsvg-devel qt6-qtwayland-devel; do
        download "$p.x86_64"
    done
    log "Extracting SDK"
    extract
    relocate "usr/$LIBDIR" "$STAGE/usr/$LIBDIR/cmake/Qt6" "$STAGE/usr/$LIBDIR/cmake/Qt6Qml" \
               "$STAGE/usr/$LIBDIR/cmake/Qt6Core" "$STAGE/usr/$LIBDIR/cmake/Qt6Gui" \
               "$STAGE/usr/$LIBDIR/cmake/Qt6Quick" "$STAGE/usr/$LIBDIR/cmake/Qt6Network" \
               "$STAGE/usr/$LIBDIR/cmake/Qt6Multimedia" "$STAGE/usr/$LIBDIR/cmake/Qt6Sql" \
               "$STAGE/usr/$LIBDIR/cmake/Qt6Test" "$STAGE/usr/$LIBDIR/cmake/Qt6Concurrent" \
               "$STAGE/usr/$LIBDIR/cmake/Qt6QuickControls2" "$STAGE/usr/$LIBDIR/cmake/Qt6OpenGL" \
               "$STAGE/usr/$LIBDIR/cmake/Qt6QmlModels" "$STAGE/usr/$LIBDIR/cmake/Qt6DBus" \
               "$STAGE/usr/$LIBDIR/cmake/Qt6StateMachine" "$STAGE/usr/$LIBDIR/cmake/Qt6HttpServer" \
               "$STAGE/usr/$LIBDIR/cmake/Qt6ShaderTools" "$STAGE/usr/$LIBDIR/cmake/Qt6Xml" \
               "$STAGE/usr/$LIBDIR/cmake/Qt6CoreTools" "$STAGE/usr/$LIBDIR/cmake/Qt6Widgets" \
               "$STAGE/usr/$LIBDIR/cmake/Qt6PrintSupport" "$STAGE/usr/$LIBDIR/cmake/Qt6Test" \
               "$STAGE/usr/$LIBDIR/cmake/Qt6Concurrent" "$STAGE/usr/$LIBDIR/cmake/Qt6Sql" \
               "$STAGE/usr/$LIBDIR/cmake/Qt6Designer" "$STAGE/usr/$LIBDIR/cmake/Qt6UiTools" \
               "$STAGE/usr/$LIBDIR/cmake/Qt6Charts" "$STAGE/usr/$LIBDIR/cmake/Qt6Location"
else
    log "Qt 6 SDK already present"
fi

# ---- Qt 6 runtime-only split packages -------------------------------------
# The -devel packages do not pull the runtime QML plugins, so fetch those too.
if [ ! -d "$STAGE/usr/$LIBDIR/qt6/qml/QtQuick" ]; then
    log "Downloading Qt 6 runtime QML modules"
    for p in qt6-qtbase-gui qt6-qtbase-icu qt6-qtsvg qt6-qtquickcontrols2 \
             qt6-qtmultimedia qt6-qtimageformats qt6-qt5compat qt6-qtshadertools; do
        download "$p.x86_64" || warn "optional package $p not available"
    done
    extract
fi

# ---- GCC binutils (provides the assembler/linker the compiler shells out to) --
if [ ! -x "$STAGE/usr/bin/as" ] && [ ! -x "$STAGE/usr/bin/ld" ]; then
    log "Downloading binutils"
    for p in binutils gcc; do download "$p.x86_64" || warn "optional $p"; done
    extract
fi

# ---- libffi (needed by the QML runtime's dlopen of native plugins) ----------
if [ ! -e "$STAGE/usr/$LIBDIR/libffi.so.8" ]; then
    log "Downloading libffi"
    download "libffi.x86_64" || warn "libffi not available"
    extract
fi

# ---- X.Org config data required by the XCB platform plugin -----------------
if [ ! -d "$STAGE/usr/share/X11/xkb" ]; then
    log "Downloading X.Org keyboard data"
    download "libxkbfile.x86_64" || warn "libxkbfile not available"
    download "xkeyboard-config.x86_64" || warn "xkeyboard-config not available"
    extract
fi

log "Done.  Activate with:"
echo  "  source <($0 --print-env)"
echo  "Qt version staged at $STAGE"
