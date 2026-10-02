#pragma once

#ifdef Q_OS_WIN
#  include <windows.h>
#endif

#include <QtQml/qqmlregistration.h>

#include <QObject>
#include <QString>
#include <QStringList>
#include <QWindow>

/// Stops the *accidental* leaks of a film: the taskbar thumbnail, the desktop's
/// recent-media list, and -- where the platform has one -- the screen-capture
/// and screen-recording APIs.
///
/// **This is not DRM and is not described as such.**  Anyone who can watch a
/// stream can record it with a camera, a capture card, or a modified client.
/// What follows raises the cost of *sharing a screenshot*, not the cost of
/// *watching*.  The claims made here are exactly what each platform's
/// documentation says they do:
///
/// * **Windows** -- `WDA_EXCLUDEFROMCAPTURE` on the window handle.  Windows 10
///   version 2004 and later exclude the window from `BitBlt`-based capture,
///   `PrintWindow`, the screen-recording path, and the taskbar's preview; and
///   blank it in a hardware encoder's capture.  It is the one platform here
///   where the exclusion is enforced by the system rather than requested from a
///   compositor that may ignore it.
/// * **Android** -- `FLAG_SECURE` on the window, which takes the window out of
///   screenshots, the screen recorder, and the recent-apps thumbnail.
/// * **X11** -- nothing is set.  `_NET_WM_BYPASS_COMPOSITOR` is a *request* to
///   the window manager, honoured by mutter and KWin and ignored by others, and
///   the flag that actually works -- `ignore_event_mask` -- additionally stops
///   the window receiving input, which would break the player.  Neither is
///   applied.
/// * **Wayland** -- there is no protocol mechanism for withholding a region
///   from capture, and nothing is set.
///
/// `active()` is therefore true only where the platform *enforces* something:
/// Windows and Android.  On Linux it is false, and `platformReport()` says why
/// in the words a user can act on.  The brief asks for platform limitations to
/// be documented rather than papered over, so nothing here claims a protection
/// the code does not apply.
class CaptureGuard : public QObject
{
    Q_OBJECT
    QML_ELEMENT
    QML_UNCREATABLE("Provided by main(); not constructible from QML.")
    /// Whether this build and this platform *enforce* anything at all.  False
    /// where what exists is only a compositor request, and false when
    /// `protect()` could not find the window -- a failure to apply is reported
    /// as a failure and never assumed away.
    Q_PROPERTY(bool active READ active NOTIFY activeChanged)
    /// One line per mechanism attempted, saying whether each was enforced or is
    /// unavailable here.  Shown in the settings screen so the list and the
    /// summary sentence cannot disagree.
    Q_PROPERTY(QStringList applied READ applied NOTIFY activeChanged)
    /// The sentence the settings screen shows in place of a claim of
    /// protection.
    Q_PROPERTY(QString platformReport READ platformReport NOTIFY activeChanged)

public:
    explicit CaptureGuard(QObject *parent = nullptr);

    bool active() const { return m_enforced; }
    QStringList applied() const { return m_notes; }
    QString platformReport() const;

    /// Protect a window, and report what was actually done.
    ///
    /// Called from QML when the player becomes visible, and safe to call
    /// repeatedly: a window that is re-mapped has silently lost whatever the
    /// previous call set, and re-applying is cheaper than detecting that.
    Q_INVOKABLE void protect(QWindow *window);

    /// Drop everything `protect()` applied.  Called when the player closes.
    Q_INVOKABLE void release();

    /// Record that the recent-media list is being suppressed.
    ///
    /// Not a switch, because there is no API to call: the freedesktop.org
    /// recent-media list is populated from a desktop entry this client does not
    /// install, and the Windows taskbar list is fed from system playback
    /// sessions Qt does not use.  It exists as a method so that the claim in
    /// `platformReport()`, the claim in `SECURITY.md` and the claim a test can
    /// assert are one object rather than three copies that drift apart.
    Q_INVOKABLE void setRecentMediaSuppressed(bool suppressed);

signals:
    void activeChanged();

private:
    /// What one platform's mechanism did.  `touched` and `enforced` are
    /// separate on purpose: only `enforced` may set `m_enforced`, and a request
    /// the platform may ignore is never recorded as one.
    struct Outcome {
        /// Whether this platform has a mechanism at all.  False means the note
        /// describes a *limit* rather than a failure.
        bool touched = false;
        bool enforced = false;
        QString note;
    };

    Outcome applyPlatform(QWindow *window);
    Outcome applyWindows();
    Outcome applyAndroid();
    Outcome applyX11();

    QString m_captureNote;
    QStringList m_notes;
    bool m_enforced = false;
    bool m_recentMediaSuppressed = false;
#ifdef Q_OS_WIN
    void *m_protectedWindow = nullptr;   ///< the HWND the flag was set on
#endif
};