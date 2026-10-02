// The error taxonomy is what every screen branches on, so the mapping is
// tested directly: a status that maps to the wrong kind puts the wrong message
// in front of the user, and a retry loop that fires on a 404 burns the
// rate-limit budget the retryable cases need.

#include <QtTest>

#include "apierror.h"

class ApiErrorTest : public QObject
{
    Q_OBJECT

private slots:
    void statusesMapToKinds();
    void onlyTransientFailuresAreRetryable();
    void unknownStatusIsNotSilentSuccess();
};

void ApiErrorTest::statusesMapToKinds()
{
    QCOMPARE(kindForStatus(200), ApiErrorKind::None);
    QCOMPARE(kindForStatus(206), ApiErrorKind::None);   // partial content
    QCOMPARE(kindForStatus(401), ApiErrorKind::Unauthorized);
    QCOMPARE(kindForStatus(403), ApiErrorKind::Forbidden);
    QCOMPARE(kindForStatus(404), ApiErrorKind::NotFound);
    QCOMPARE(kindForStatus(409), ApiErrorKind::Conflict);
    QCOMPARE(kindForStatus(410), ApiErrorKind::Gone);
    QCOMPARE(kindForStatus(422), ApiErrorKind::ValidationFailed);
    QCOMPARE(kindForStatus(429), ApiErrorKind::RateLimited);
    QCOMPARE(kindForStatus(500), ApiErrorKind::ServerError);
    QCOMPARE(kindForStatus(503), ApiErrorKind::ServerError);
}

void ApiErrorTest::onlyTransientFailuresAreRetryable()
{
    // A timeout or a refused connection may fix itself.
    QVERIFY(ApiError({ApiErrorKind::Timeout, {}, {}, {}, 0, 0}).isRetryable());
    QVERIFY(ApiError({ApiErrorKind::NetworkUnreachable, {}, {}, {}, 0, 0}).isRetryable());
    QVERIFY(ApiError({ApiErrorKind::RateLimited, {}, {}, {}, 5, 0}).isRetryable());
    QVERIFY(ApiError({ApiErrorKind::ServerError, {}, {}, {}, 0, 0}).isRetryable());

    // These will be exactly as true in thirty seconds as they are now.
    QVERIFY(!ApiError({ApiErrorKind::NotFound, {}, {}, {}, 0, 0}).isRetryable());
    QVERIFY(!ApiError({ApiErrorKind::Forbidden, {}, {}, {}, 0, 0}).isRetryable());
    QVERIFY(!ApiError({ApiErrorKind::ValidationFailed, {}, {}, {}, 0, 0}).isRetryable());
}

void ApiErrorTest::unknownStatusIsNotSilentSuccess()
{
    // A 3xx or an unparseable status must not read as "no error": the client
    // follows redirects manually, so a redirect that reaches us is a bug, and
    // treating it as success would show an empty screen instead of an error.
    QVERIFY(kindForStatus(302) != ApiErrorKind::None);
    QVERIFY(kindForStatus(0) != ApiErrorKind::None);
    QVERIFY(kindForStatus(-1) != ApiErrorKind::None);
}

QTEST_GUILESS_MAIN(ApiErrorTest)
#include "tst_apierror.moc"
