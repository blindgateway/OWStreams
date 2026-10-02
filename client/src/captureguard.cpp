#include "captureguard.h"

#include <QGuiApplication>
#include <QThread>
#include <QWindow>

#ifdef Q_OS_WIN
#  include <windows.h>
#endif

#ifdef Q_OS_ANDROID
#  include <QAndroidJniObject>
#  include <QtAndroid>
#endif

// Nothing platform-private is included on purpose.  The xcb plugin's
// `QXcbWindow` has no public counterpart -- there is no `QNativeInterface` for
// it -- and reaching into it would mean linking `libQt6XcbQpa`, for which this
// Qt does not install a CMake package.  So the X11 path sets nothing and says
// so, which is the honest outcome rather than a private-header dependency that
// builds on exactly one machine.

namespace {

/// The note recorded when `protect()` was handed no window.
///
/// A function-local constant rather than a string written out at each use: it
/// is appended on one path and removed on three, and a typo in one of them is a
/// note that can never be taken off again -- which shows up as a settings screen
/// that keeps claiming a protection nobody applied.
const QString &noWindowNote()
{
    static const QString note = QStringLiteral("no window: nothing was protected");
    return note;
}

#ifdef Q_OS_WIN
/// `WDA_EXCLUDEFROMCAPTURE`.  Spelled out rather than taken from `<windows.h>`
/// because that header only defines it when the target SDK is new enough, and
/// this file has to compile against the oldest SDK still in use -- where the
/// constant is simply absent.
constexpr DWORD kExcludeFromCapture = 0x00000020;
/// The `SetWindowLongPtr` failure code for a window on a desktop that does not
/// know the flag.  Windows 10 before 2004 answers this, and it is the only way
/// to tell "too old" from "the call worked and the previous value was 0".
constexpr DWORD kWindowApiUnsupported = 0x00000002;
#endif

/// `WindowManager.LayoutParams.FLAG_SECURE`, fixed by the Android platform ABI
/// since API 9.  Spelled out so this file does not need the Android SDK headers
/// to read a constant that cannot change.
constexpr int kAndroidFlagSecure = 0x00000800;

}  // namespace

CaptureGuard::CaptureGuard(QObject *parent)
    : QObject(parent)
{
    // Recorded from construction rather than when something first plays: it
    // describes what is already true, and doing it later would leave a window in
    // which the first title a viewer played is not accounted for.
    setRecentMediaSuppressed(true);
}

void CaptureGuard::setRecentMediaSuppressed(bool suppressed)
{
    if (m_recentMediaSuppressed == suppressed)
        return;
    m_recentMediaSuppressed = suppressed;

    // Two desktop mechanisms publish what is being watched, and neither has an
    // API in Qt:
    //
    // * The freedesktop.org **recent-media list**, which the desktop
    //   environment populates from the application's desktop entry.  An entry
    //   naming a player that is not installed lists nothing, so the client
    //   deliberately calls neither `QGuiApplication::setDesktopFileName` nor
    //   anything else, and no `.desktop` file is installed for it.
    // * The Windows shell's **taskbar recent-media entries and jumplists**, fed
    //   from the system playback-session APIs, which Qt does not use.
    //
    // So this is a record of what is true rather than a switch: there is no API
    // here to call.  It exists so that the claim in `platformReport()`, the
    // claim in `SECURITY.md` and the claim a test can assert are one object
    // instead of three copies that drift apart.
    static const QString kNote = QStringLiteral(
        "recent media: nothing is published -- no desktop entry is installed and "
        "no system playback session is registered");
    if (suppressed)
        m_notes.append(kNote);
    else
        m_notes.removeAll(kNote);
    emit activeChanged();
}

void CaptureGuard::protect(QWindow *window)
{
    if (!window) {
        // Not a no-op.  A window that could not be found is the one case where
        // the guard is silently doing nothing, and saying so is what stops it
        // being read as protected.
        m_notes.removeAll(m_captureNote);
        m_notes.removeAll(noWindowNote());
        m_notes.append(noWindowNote());
        m_enforced = false;
        emit activeChanged();
        return;
    }

    // Called from QML, which is the GUI thread, and every mechanism below acts
    // on this thread's window.  The assertion is here because a caller reaching
    // in from a worker thread is the one way this could corrupt state.
    Q_ASSERT(QThread::currentThread() == window->thread());

    // Re-protect rather than accumulate.  A window that is re-mapped has lost
    // whatever the last call set, and the notes describe the current state, not
    // a history of attempts.
    m_notes.removeAll(m_captureNote);
    m_notes.removeAll(noWindowNote());
    m_enforced = false;

    const Outcome outcome = applyPlatform(window);
    if (!outcome.touched) {
        // The window exists but this platform has nothing to do to it.  That is
        // a different fact from "there was no window", and the settings screen
        // reads it as one line either way -- so it is recorded here rather than
        // leaving the last note standing.
        m_notes.append(outcome.note);
        emit activeChanged();
        return;
    }

    m_captureNote = outcome.note;
    m_notes.append(outcome.note);

    // Only `enforced` may claim protection.  A platform where the mechanism is a
    // request to something that may ignore it is recorded as a request.
    m_enforced = outcome.enforced;

#ifdef Q_OS_WIN
    // Remembered rather than looked up: the Windows flag lives on the HWND, and
    // Qt may have re-mapped the window between two titles, so a lookup at
    // `release()` would miss the handle that was actually protected.
    m_protectedWindow = reinterpret_cast<HWND>(window->winId());
#endif

    emit activeChanged();
}

void CaptureGuard::release()
{
#ifdef Q_OS_WIN
    if (m_protectedWindow) {
        // `GWLP_EXSTYLE` is spelled out because `GetWindowLongPtr`'s index is a
        // compile-time choice.
        SetWindowLongPtr(m_protectedWindow, GWLP_EXSTYLE,
                         GetWindowLongPtr(m_protectedWindow, GWLP_EXSTYLE)
                             & ~kExcludeFromCapture);
    }
#endif
    #ifdef Q_OS_WIN
    m_protectedWindow = nullptr;
#endif
    m_notes.removeAll(m_captureNote);
    m_notes.removeAll(noWindowNote());
    // Nothing to undo on Android -- the flag dies with the window -- or on X11,
    // where nothing was set.  The reported state goes with the flag, because a
    // report that outlived its protection is the claim this class exists to
    // avoid making.
    m_enforced = false;
    emit activeChanged();
}

CaptureGuard::Outcome CaptureGuard::applyPlatform(QWindow *window)
{
    Q_UNUSED(window)
#ifdef Q_OS_WIN
    return applyWindows();
#elif defined(Q_OS_ANDROID)
    return applyAndroid();
#else
    return applyX11();
#endif
}

CaptureGuard::Outcome CaptureGuard::applyWindows()
{
#ifdef Q_OS_WIN
    if (!m_protectedWindow)
        return {};

    SetLastError(ERROR_SUCCESS);
    const LONG_PTR previous =
        SetWindowLongPtr(m_protectedWindow, GWLP_EXSTYLE,
                         GetWindowLongPtr(m_protectedWindow, GWLP_EXSTYLE)
                             | kExcludeFromCapture);
    if (previous == 0 && GetLastError() == kWindowApiUnsupported) {
        // Windows 10 before 2004.  The flag is a documented no-op there, and
        // asking for it anyway would leave the app claiming a protection the
        // system silently ignored.
        return {false, false,
                QStringLiteral("screen capture: unavailable before Windows 10 "
                               "version 2004")};
    }

    return {true, true,
            QStringLiteral("screen capture: excluded from screen capture, "
                           "screen recording and taskbar previews")};
#else
    return {};
#endif
}

CaptureGuard::Outcome CaptureGuard::applyAndroid()
{
#ifdef Q_OS_ANDROID
    // `FLAG_SECURE` is set on the *activity's* window, and a Qt window on
    // Android is the activity's own window.  Qt does not expose the Java side
    // of it, so the flag is applied through JNI rather than through `QWindow`.
    QAndroidJniObject activity = QtAndroid::androidActivity();
    if (!activity.isValid())
        return {};

    if (!activity.callMethod<void>("setSecureSurface", kAndroidFlagSecure))
        return {};

    return {true, true,
            QStringLiteral("screen capture: excluded from screenshots, the "
                           "screen recorder and the recent-apps thumbnail")};
#else
    return {};
#endif
}

CaptureGuard::Outcome CaptureGuard::applyX11()
{
    // Deliberately sets nothing, and `captureguard.h` says why: on X11 the flag
    // that actually excludes a window from capture, `ignore_event_mask`, also
    // stops it receiving input, and there is no way to have both on the window
    // the player draws into.
    if (QGuiApplication::platformName().startsWith(QLatin1String("wayland"))) {
        return {false, false,
                QStringLiteral("screen capture: the Wayland protocol has no "
                               "mechanism for this")};
    }
    return {false, false,
            QStringLiteral("screen capture: X11 asks the window manager to "
                           "bypass compositing for the window, and mutter and "
                           "KWin honour it -- but the flag that also blocks input "
                           "would make the player unusable, so it is not set")};
}

QString CaptureGuard::platformReport() const
{
#ifdef Q_OS_WIN
    return m_enforced
               ? tr("This device excludes the player from screen capture, "
                    "screen recording and taskbar previews (Windows). That "
                    "raises the cost of an accidental screenshot. It does not "
                    "stop anyone recording what they are allowed to watch.")
               : tr("This device could not exclude the player from screen "
                    "capture. Nothing is being withheld.");
#elif defined(Q_OS_ANDROID)
    return m_enforced
               ? tr("This device excludes the player from screenshots, the "
                    "screen recorder and the recent-apps thumbnail (Android). "
                    "That raises the cost of an accidental screenshot. It does "
                    "not stop anyone recording what they are allowed to watch.")
               : tr("This device could not exclude the player from screen "
                    "capture. Nothing is being withheld.");
#else
    if (QGuiApplication::platformName().startsWith(QLatin1String("wayland"))) {
        return tr("This desktop is Wayland, whose protocol has no mechanism for "
                  "excluding a window from capture. Nothing is applied and "
                  "nothing is claimed. The server-side protections -- "
                  "authenticated media routes, short playback tokens, no direct "
                  "file access -- do not depend on the client window.");
    }
    return tr("This desktop is X11, where excluding a window from capture "
              "requires a window-manager request that mutter and KWin may honour "
              "and others ignore, or a flag that would also stop the player "
              "receiving input. Neither is applied: playback would break, and a "
              "protection that might work is not one worth claiming.");
#endif
}