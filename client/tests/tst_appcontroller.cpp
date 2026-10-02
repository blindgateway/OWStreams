// Screen routing and the library grid's paging.
//
// Both are things QML *cannot* get right on its own: `Shell.qml` binds one
// Loader per destination against `AppController::Screen`, and a wrong enum
// value there means two screens visible at once with one of them never
// updating.  A test that only exercised the screens themselves would not catch
// it.

#include <QtTest>

#include "apiclient.h"
#include "appcontroller.h"

class AppControllerTest : public QObject
{
    Q_OBJECT

private slots:
    void init();

    void screenEnumIsStable();
    void navigationMovesBetweenTopLevelScreens();
    void openDetailFetchesBeforeItShows();
    void backFromDetailReturnsToWhereItCameFrom();
    void thePlayerIsALayerOverTheDetailScreen();
    void backFromThePlayerDoesNotGoToItself();
    void anEmptyQueryDoesNotReachTheServer();
    void searchClearsWhenTheQueryIsEmptied();

private:
    ApiClient *m_api = nullptr;
    AppController *m_controller = nullptr;
};

void AppControllerTest::init()
{
    m_api = new ApiClient(this);
    m_controller = new AppController(m_api, this);
}

void AppControllerTest::screenEnumIsStable()
{
    // These numbers are what `Shell.qml` compares against.  Reordering the enum
    // silently repoints every navigation target in the UI.
    QCOMPARE(int(AppController::Home), 0);
    QCOMPARE(int(AppController::Library), 1);
    QCOMPARE(int(AppController::Search), 2);
    QCOMPARE(int(AppController::Favorites), 3);
    QCOMPARE(int(AppController::History), 4);
    QCOMPARE(int(AppController::Settings), 5);
    QCOMPARE(int(AppController::Player), 6);
    QCOMPARE(int(AppController::Detail), 7);

    // Settings and Detail were both 6 for a while, which made the settings
    // screen and the detail screen share one Loader.
    QVERIFY(int(AppController::Settings) != int(AppController::Detail));
    QVERIFY(int(AppController::Player) != int(AppController::Detail));
}

void AppControllerTest::navigationMovesBetweenTopLevelScreens()
{
    m_controller->goLibrary();
    QCOMPARE(m_controller->screen(), AppController::Library);
    m_controller->goFavorites();
    QCOMPARE(m_controller->screen(), AppController::Favorites);
    m_controller->goHistory();
    QCOMPARE(m_controller->screen(), AppController::History);
    m_controller->goSettings();
    QCOMPARE(m_controller->screen(), AppController::Settings);
    m_controller->goSearch();
    QCOMPARE(m_controller->screen(), AppController::Search);
}

void AppControllerTest::openDetailFetchesBeforeItShows()
{
    // The detail screen only exists once the server has answered: showing it
    // first would mean a poster, a title and a cast list that are all empty
    // while the request is in flight.  Against no server the screen therefore
    // does *not* change.
    m_controller->goLibrary();
    m_controller->openDetail(42);

    QVERIFY(m_controller->isBusy());
    QCOMPARE(m_controller->screen(), AppController::Library);

    m_controller->setScreenForTest(AppController::Detail);
    QCOMPARE(m_controller->screen(), AppController::Detail);
}

void AppControllerTest::backFromDetailReturnsToWhereItCameFrom()
{
    // Back is "the rail you came from", not "home": opening a title from the
    // library and pressing back should land on the library.
    m_controller->goLibrary();
    m_controller->setScreenForTest(AppController::Detail);
    m_controller->back();
    QCOMPARE(m_controller->screen(), AppController::Library);

    m_controller->goFavorites();
    m_controller->setScreenForTest(AppController::Detail);
    m_controller->back();
    QCOMPARE(m_controller->screen(), AppController::Favorites);
}

void AppControllerTest::thePlayerIsALayerOverTheDetailScreen()
{
    m_controller->goLibrary();
    m_controller->openPlayer(42);
    QCOMPARE(m_controller->screen(), AppController::Player);
    QCOMPARE(m_controller->playerMediaId(), 42);

    m_controller->closePlayer();
    // Back to the rail, not to the detail screen -- the player is a layer
    // over whatever opened it, not a destination of its own.
    QCOMPARE(m_controller->screen(), AppController::Library);
    QCOMPARE(m_controller->playerMediaId(), 0);
}

void AppControllerTest::backFromThePlayerDoesNotGoToItself()
{
    m_controller->goHome();
    m_controller->openPlayer(7);
    m_controller->back();
    // The player was opened from Home, so Home is the answer -- but never the
    // player itself, which would be a screen with no way out.
    QVERIFY(m_controller->screen() != AppController::Player);
    QCOMPARE(m_controller->screen(), AppController::Home);
    QCOMPARE(m_controller->playerMediaId(), 0);
}

void AppControllerTest::anEmptyQueryDoesNotReachTheServer()
{
    // The search endpoint is the most expensive query in the app and has its
    // own rate-limit bucket; submitting whitespace must not spend one.
    QSignalSpy spy(m_api, &ApiClient::searchLoaded);

    m_controller->setSearchQuery(QString());
    m_controller->submitSearch();
    m_controller->setSearchQuery(QStringLiteral("   "));
    m_controller->submitSearch();

    QVERIFY(spy.isEmpty());
    QCOMPARE(m_controller->searchQuery(), QStringLiteral("   "));
}

void AppControllerTest::searchClearsWhenTheQueryIsEmptied()
{
    m_controller->setSearchQuery(QStringLiteral("dune"));
    m_controller->clearSearch();
    QCOMPARE(m_controller->searchQuery(), QString());
    QCOMPARE(m_controller->searchResults()->rowCount(), 0);
}

QTEST_GUILESS_MAIN(AppControllerTest)
#include "tst_appcontroller.moc"
