// The poster URL.
//
// A row's poster is served by an authenticated route, and `QQuickImage` cannot
// send a header -- so the URL has to be absolute, built from the server we are
// signed in to.  The bug this guards against is the quiet one: a model filled
// before the base URL is known ends up with rows that silently never load
// artwork, and the grid looks like a grid of blank rectangles.

#include <QtTest>

#include "librarymodel.h"

namespace {

QJsonObject row(int id, const QString &title, const QString &posterKey = {})
{
    QJsonObject object{{QStringLiteral("id"), id},
                       {QStringLiteral("title"), title}};
    if (!posterKey.isEmpty())
        object.insert(QStringLiteral("poster_key"), posterKey);
    return object;
}

}  // namespace

class LibraryModelTest : public QObject
{
    Q_OBJECT

private slots:
    void noPosterKeyMeansNoUrl();
    void noBaseUrlMeansNoUrl();
    void posterUrlIsAbsolute();
    void settingTheBaseUrlLaterRepaintsEveryRow();
    void switchingServerRepaintsEveryRow();
    void appendDoesNotDisturbExistingRows();
    void appendAddsToTheEnd();
    void removeDropsOneRow();
    void removeIgnoresAnUnknownId();
};

void LibraryModelTest::noPosterKeyMeansNoUrl()
{
    MediaListModel model;
    model.setBaseUrl(QUrl(QStringLiteral("http://box.local:8484")));
    model.setItems(QJsonArray{row(1, QStringLiteral("No artwork"))});

    QCOMPARE(model.rowCount(), 1);
    // An invalid QUrl rather than a route that will 404 on every repaint.
    QVERIFY(!model.posterUrlFor(1).isValid());
}

void LibraryModelTest::noBaseUrlMeansNoUrl()
{
    MediaListModel model;
    model.setItems(QJsonArray{row(1, QStringLiteral("Anything"), QStringLiteral("p.jpg"))});
    // Filled before we knew the server: no absolute URL, on purpose.
    QVERIFY(!model.posterUrlFor(1).isValid());
}

void LibraryModelTest::posterUrlIsAbsolute()
{
    MediaListModel model;
    model.setBaseUrl(QUrl(QStringLiteral("http://box.local:8484")));
    model.setItems(QJsonArray{
        row(1, QStringLiteral("Dune"), QStringLiteral("dune.jpg")),
        row(2, QStringLiteral("Alien"), QStringLiteral("alien.jpg"))});

    QCOMPARE(model.posterUrlFor(1).toString(),
             QStringLiteral("http://box.local:8484/api/v1/media/1/poster"));
    QCOMPARE(model.posterUrlFor(2).toString(),
             QStringLiteral("http://box.local:8484/api/v1/media/2/poster"));
    QVERIFY(!model.posterUrlFor(99).isValid());
}

void LibraryModelTest::settingTheBaseUrlLaterRepaintsEveryRow()
{
    MediaListModel model;
    QSignalSpy spy(&model, &QAbstractItemModel::dataChanged);

    model.setItems(QJsonArray{row(1, QStringLiteral("Dune"), QStringLiteral("dune.jpg"))});
    QCOMPARE(spy.count(), 0);

    model.setBaseUrl(QUrl(QStringLiteral("http://box.local:8484")));

    // One row, one repaint -- and it covers the poster role for every row.
    QCOMPARE(spy.count(), 1);
    const QList<QVariant> roles = spy.at(0).at(2).toList();
    QCOMPARE(roles.size(), 1);
    QCOMPARE(roles.at(0).toInt(), int(MediaListModel::PosterUrlRole));
    QCOMPARE(model.posterUrlFor(1).toString(),
             QStringLiteral("http://box.local:8484/api/v1/media/1/poster"));
}

void LibraryModelTest::switchingServerRepaintsEveryRow()
{
    MediaListModel model;
    model.setBaseUrl(QUrl(QStringLiteral("http://old.local:8484")));
    model.setItems(QJsonArray{row(1, QStringLiteral("Dune"), QStringLiteral("dune.jpg"))});

    QSignalSpy spy(&model, &QAbstractItemModel::dataChanged);
    model.setBaseUrl(QUrl(QStringLiteral("http://new.local:8484")));

    QCOMPARE(spy.count(), 1);
    QCOMPARE(model.posterUrlFor(1).toString(),
             QStringLiteral("http://new.local:8484/api/v1/media/1/poster"));
}

void LibraryModelTest::appendDoesNotDisturbExistingRows()
{
    MediaListModel model;
    model.setBaseUrl(QUrl(QStringLiteral("http://box.local:8484")));
    model.setItems(QJsonArray{row(1, QStringLiteral("Dune"), QStringLiteral("dune.jpg"))});

    QSignalSpy resetSpy(&model, &QAbstractItemModel::modelReset);
    model.appendItems(QJsonArray{row(2, QStringLiteral("Alien"), QStringLiteral("alien.jpg"))});

    // A reset on every page would empty a thousand-poster grid for the length
    // of a request.
    QCOMPARE(resetSpy.count(), 0);
    QCOMPARE(model.rowCount(), 2);
    QCOMPARE(model.data(model.index(0), MediaListModel::TitleRole).toString(),
             QStringLiteral("Dune"));
}

void LibraryModelTest::appendAddsToTheEnd()
{
    MediaListModel model;
    model.setBaseUrl(QUrl(QStringLiteral("http://box.local:8484")));
    model.setItems(QJsonArray{row(1, QStringLiteral("Dune"), QStringLiteral("dune.jpg"))});
    model.appendItems(QJsonArray{
        row(2, QStringLiteral("Alien"), QStringLiteral("alien.jpg")),
        row(3, QStringLiteral("Arrival"))});

    QCOMPARE(model.rowCount(), 3);
    QCOMPARE(model.data(model.index(2), MediaListModel::TitleRole).toString(),
             QStringLiteral("Arrival"));
    // The appended row was built by the same code as the first, so it has a
    // poster URL too -- page two must not look different from page one.
    QCOMPARE(model.posterUrlFor(3).toString(), QUrl());
    QCOMPARE(model.posterUrlFor(2).toString(),
             QUrl(QStringLiteral("http://box.local:8484/api/v1/media/2/poster")));

    model.appendItems(QJsonArray{});
    QCOMPARE(model.rowCount(), 3);
}

void LibraryModelTest::removeDropsOneRow()
{
    MediaListModel model;
    model.setItems(QJsonArray{
        row(1, QStringLiteral("Dune")), row(2, QStringLiteral("Alien")),
        row(3, QStringLiteral("Arrival"))});

    QSignalSpy resetSpy(&model, &QAbstractItemModel::modelReset);
    QSignalSpy removeSpy(&model, &QAbstractItemModel::rowsRemoved);
    model.remove(2);

    QCOMPARE(resetSpy.count(), 0);
    QCOMPARE(removeSpy.count(), 1);
    QCOMPARE(model.rowCount(), 2);
    QCOMPARE(model.data(model.index(1), MediaListModel::TitleRole).toString(),
             QStringLiteral("Arrival"));
}

void LibraryModelTest::removeIgnoresAnUnknownId()
{
    MediaListModel model;
    model.setItems(QJsonArray{row(1, QStringLiteral("Dune"))});

    QSignalSpy removeSpy(&model, &QAbstractItemModel::rowsRemoved);
    model.remove(404);

    QCOMPARE(removeSpy.count(), 0);
    QCOMPARE(model.rowCount(), 1);
}

QTEST_GUILESS_MAIN(LibraryModelTest)
#include "tst_librarymodel.moc"
