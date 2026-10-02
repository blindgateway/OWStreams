#!/bin/sh
# Run qmllint over every QML file in the module.
#
# `qmllint` needs the *build's* generated module description -- the qmldir, the
# qmltypes, the metatypes -- to resolve `import Stream`, and those only exist
# after `qt_add_qml_module` has run.  Pointing it at `build/` is therefore not
# an optimisation, it is the difference between "Stream is not a module" and a
# real lint.  Build first; this script says so rather than producing a hundred
# warnings that all mean the same thing.
#
# A warning is an error.  QML's mistakes are runtime failures the compiler
# cannot see, and this is the only thing standing between a typo and a blank
# screen.
#
# Two warnings are exempted, and neither is a QML defect:
#
#   unqualified  -- `session`, `api`, `credentials` and `controller` are
#                   context properties injected from C++, so the linter has
#                   nothing to resolve them against.  The `App` singleton
#                   exists so the UI itself never has to use them.
#   import       -- the `Stream` import itself, when the generated module
#                   description is missing (i.e. the project has not been
#                   configured yet).

set -e

root=$(cd "$(dirname "$0")" && pwd)
build=${STREAM_BUILD_DIR:-"$root/build"}

if [ ! -f "$build/Stream/qmldir" ]; then
    echo "No generated module in $build. Configure and build first:" >&2
    echo "  cmake -S '$root' -B '$build' && cmake --build '$build'" >&2
    exit 1
fi

lint=$(command -v qmllint-qt6 || true)
if [ -z "$lint" ]; then
    for candidate in /usr/lib64/qt6/bin/qmllint /usr/lib/qt6/bin/qmllint \
                     /usr/lib/x86_64-linux-gnu/qt6/bin/qmllint; do
        [ -x "$candidate" ] && lint=$candidate && break
    done
fi
if [ -z "$lint" ]; then
    echo "qmllint not found. Install the Qt 6 development tools." >&2
    exit 1
fi

# The file list comes from CMake so it cannot drift from the module.
files=$(sed -n '/^set(STREAM_QML_FILES$/,/^)$/p' "$root/CMakeLists.txt" \
        | sed -n 's/^ *\(qml\/.*\.qml\)$/\1/p')

# The Qt import path has to be given explicitly: qmllint reads
# `qt.query(qt_install_qml)` from the project when it runs inside CMake, but a
# standalone script has to find it, and without it every `Item` and `Loader` in
# the project is an unresolved import -- which is not a lint, it is a missing
# -I flag.
qmlpath=$("$lint" --help >/dev/null 2>&1 && echo "")   # keep shellcheck quiet
for candidate in "${QT_INSTALL_QML:-}" \
                 /usr/lib64/qt6/qml /usr/lib/qt6/qml \
                 /usr/lib/x86_64-linux-gnu/qt6/qml; do
    [ -n "$candidate" ] && [ -d "$candidate" ] && qmlpath=$candidate && break
done
if [ -z "$qmlpath" ]; then
    echo "Could not find the Qt QML import path. Set QT_INSTALL_QML." >&2
    exit 1
fi

# `.qmllint.ini` next to the source sets the policy: which warnings count, and
# why the ones that are off are off.  `--ignore-settings` is deliberately *not*
# passed -- the policy file is the point.
status=0
count=0
for f in $files; do
    count=$((count + 1))
    "$lint" -I "$qmlpath" -I "$build" -I "$root" "$root/$f" || status=1
done

if [ "$status" -eq 0 ]; then
    echo "qml lint: clean ($count files)"
fi
exit "$status"
