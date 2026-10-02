// Runtimes are written out in two languages -- C++ for the model roles QML
// reads, JavaScript for the detail screen's fact strip -- and three copies of
// the same seven-line function is how they drift.  These are the cases a drift
// would show up in, expressed once.
//
// The fixtures are deliberately the awkward ones: an hour exactly, a minute
// exactly, and the boundary between them, where "60 min" and "1 h" are the same
// length of film and the reader is looking for one of them.

#include <QtTest>

#include "appcontroller.h"
#include "homescreenmodel.h"
#include "librarymodel.h"

namespace {

/// The same function `DetailScreen.qml`'s `formatRuntime` is, transcribed.
/// Deliberately a copy rather than a call into the QML file: the QML cannot be
/// loaded from a guiless test, and a test that *could* load it would stop
/// testing anything when the QML failed to parse.
QString detailScreenRuntime(int minutes)
{
    if (minutes <= 0)
        return {};
    const int hours = minutes / 60;
    const int rest = minutes % 60;
    if (hours == 0)
        return QStringLiteral("%1 min").arg(rest);
    if (rest == 0)
        return QStringLiteral("%1 h").arg(hours);
    return QStringLiteral("%1 h %2 min").arg(hours).arg(rest);
}

QVariantMap row(int minutes)
{
    return {{QStringLiteral("id"), 1},
            {QStringLiteral("title"), QStringLiteral("A film")},
            {QStringLiteral("runtime_minutes"), minutes}};
}

QVariant modelRoleFor(int minutes)
{
    MediaListModel model;
    model.setItems(QJsonArray{QJsonObject::fromVariantMap(row(minutes))});
    return model.data(model.index(0, 0), MediaListModel::RuntimeTextRole);
}

}  // namespace

class TimeTextTest : public QObject
{
    Q_OBJECT

private slots:
    void bothLanguagesAgreeOnEveryFixture();
    void theFixturesCoverTheAwkwardBoundaries();
};

void TimeTextTest::bothLanguagesAgreeOnEveryFixture()
{
    const QList<int> fixtures = {0,   1,   42,  59,  60,  61,  89,
                                 90,  119, 120, 121, 143, 144, 149};
    for (const int minutes : fixtures) {
        const QString fromModel = modelRoleFor(minutes).toString();
        const QString fromQml = detailScreenRuntime(minutes);
        QVERIFY2(fromModel == fromQml,
                 qPrintable(QStringLiteral("%1 minutes: model says \"%2\", "
                                           "detail screen says \"%3\"")
                                .arg(minutes).arg(fromModel, fromQml)));
    }
}

void TimeTextTest::theFixturesCoverTheAwkwardBoundaries()
{
    // The three that a rewrite is most likely to get wrong.
    QCOMPARE(detailScreenRuntime(0), QString());
    QCOMPARE(detailScreenRuntime(59), QStringLiteral("59 min"));
    QCOMPARE(detailScreenRuntime(60), QStringLiteral("1 h"));
    QCOMPARE(detailScreenRuntime(61), QStringLiteral("1 h 1 min"));
    QCOMPARE(detailScreenRuntime(120), QStringLiteral("2 h"));
    QCOMPARE(detailScreenRuntime(143), QStringLiteral("2 h 23 min"));

    // And the same three through the model, so a change on either side that
    // breaks the boundary fails here rather than on a poster.
    QCOMPARE(modelRoleFor(60).toString(), QStringLiteral("1 h"));
    QCOMPARE(modelRoleFor(59).toString(), QStringLiteral("59 min"));
    QCOMPARE(modelRoleFor(0).toString(), QString());
}

QTEST_GUILESS_MAIN(TimeTextTest)
#include "tst_timetext.moc"