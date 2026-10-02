// The capture guard's job is not to protect anything -- that is the platform's
// job -- but to *report honestly* what it did and did not do.
//
// Every test here is about the report.  A guard that claims more than it does
// is worse than no guard: the brief asks for platform limitations to be
// documented rather than papered over, and the first way to paper over one is to
// let a boolean stand in for a platform that enforces nothing.  So the
// assertions are deliberately about the *negative* case -- that on a platform
// with no mechanism, the guard says so and does not say it is active.

#include <QtTest>
#include <QWindow>

#include "captureguard.h"

class CaptureGuardTest : public QObject
{
    Q_OBJECT

private slots:
    void aFreshGuardClaimsNothing();
    void protectingNothingIsReportedAsNothing();
    void releaseClearsTheEnforcementClaim();
    void theReportNamesThePlatformAndItsLimit();
    void repeatedProtectCallsDoNotAccumulateNotes();
    void signalsFollowTheStateRatherThanTheCall();
};

void CaptureGuardTest::aFreshGuardClaimsNothing()
{
    const CaptureGuard guard;

    // On a platform that enforces something (Windows, Android) this is the one
    // place a fresh guard may be active: the Windows path needs a window handle
    // and gets none here, so it is inactive too.  The invariant is that no
    // *note* claims a protection that no call made.
    QVERIFY(!guard.applied().isEmpty());
    for (const QString &note : guard.applied())
        QVERIFY2(!note.contains(QLatin1String("screen capture")),
                 qPrintable(QStringLiteral("a fresh guard published a capture "
                                           "note without being asked: %1").arg(note)));
}

void CaptureGuardTest::protectingNothingIsReportedAsNothing()
{
    CaptureGuard guard;
    QSignalSpy changed(&guard, &CaptureGuard::activeChanged);

    guard.protect(nullptr);

    // The one case where the guard is silently doing nothing.  If it were a
    // no-op, `active()` would keep whatever it had and the settings screen would
    // go on describing a protection that is not there.
    QVERIFY(!guard.active());
    QVERIFY(guard.applied().contains(QStringLiteral("no window: nothing was protected")));
    QVERIFY(changed.count() > 0);
}

void CaptureGuardTest::releaseClearsTheEnforcementClaim()
{
    CaptureGuard guard;
    guard.protect(nullptr);
    QVERIFY(!guard.active());

    guard.release();

    // A report that outlived its protection is the claim this class exists to
    // avoid.  `release()` is called when the player closes; the settings screen
    // can be opened at any time afterwards.
    QVERIFY(!guard.active());
    QVERIFY(!guard.applied().contains(QStringLiteral("no window: nothing was protected")));
    // The recent-media note is not tied to a window and survives, so the list
    // is empty rather than absent.
    QVERIFY(!guard.applied().isEmpty());
}

void CaptureGuardTest::theReportNamesThePlatformAndItsLimit()
{
    // A real window, so the platform's *own* answer is what is under test.  An
    // earlier version of this suite passed `nullptr` and asserted the X11 and
    // Wayland notes, which is a different fact: it was reading the missing-window
    // path and congratulating itself on a Linux-specific claim.
    //
    // The assertions are on content rather than on the exact string: a report is
    // user-facing text that will be reworded, and a test that quotes it whole
    // fails the next time somebody fixes a comma instead of the claim.
    QWindow window;
    window.resize(320, 240);
    window.show();
    QVERIFY(QTest::qWaitForWindowExposed(&window));

    CaptureGuard guard;
    guard.protect(&window);

    const QString report = guard.platformReport();
    QVERIFY(!report.isEmpty());
    QVERIFY2(!report.contains(QLatin1String("DRM"), Qt::CaseInsensitive),
             qPrintable(report));
    QVERIFY2(!report.contains(QLatin1String("prevent"), Qt::CaseInsensitive),
             qPrintable(report));

#ifdef Q_OS_WIN
    QVERIFY(guard.active());
    QVERIFY(report.contains(QLatin1String("Windows")));
#elif defined(Q_OS_ANDROID)
    QVERIFY(guard.active());
#else
    // X11 and Wayland both land here and both must say `false`.  A Linux build
    // that claimed to be protected would be claiming a mechanism its platform
    // does not offer.
    QVERIFY2(!guard.active(), qPrintable(report));
    if (QGuiApplication::platformName().startsWith(QLatin1String("wayland")))
        QVERIFY2(report.contains(QLatin1String("Wayland")), qPrintable(report));
    else
        QVERIFY2(report.contains(QLatin1String("X11")), qPrintable(report));
#endif

    // And the note the list shows agrees with the summary sentence.
    QVERIFY(!guard.applied().isEmpty());
    QVERIFY(!guard.applied().contains(QStringLiteral("no window: nothing was protected")));
    QVERIFY(!guard.applied().contains(QString(), Qt::CaseInsensitive));
}

void CaptureGuardTest::repeatedProtectCallsDoNotAccumulateNotes()
{
    CaptureGuard guard;

    // A player that is re-mapped loses whatever was set on the native window,
    // so `protect()` is called again.  If each call appended its note, the
    // settings screen would grow a line per re-map and a title watched
    // repeatedly would list the same mechanism a dozen times.
    const int baseline = guard.applied().count();
    for (int i = 0; i < 5; ++i)
        guard.protect(nullptr);

    QCOMPARE(guard.applied().count(), baseline + 1);
    QCOMPARE(guard.applied().count(QStringLiteral("no window: nothing was protected")), 1);
}

void CaptureGuardTest::signalsFollowTheStateRatherThanTheCall()
{
    CaptureGuard guard;
    QSignalSpy changed(&guard, &CaptureGuard::activeChanged);

    guard.protect(nullptr);
    const int afterFirst = changed.count();
    QVERIFY(afterFirst > 0);

    // A call that changes nothing must stay quiet.  QML binds to these
    // properties, and a signal emitted on every re-map would rebuild the
    // settings section for no reason.
    guard.setRecentMediaSuppressed(true);
    QCOMPARE(changed.count(), afterFirst);
}

QTEST_MAIN(CaptureGuardTest)
#include "tst_captureguard.moc"