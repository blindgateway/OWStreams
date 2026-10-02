#pragma once

#include <QJsonArray>
#include <QObject>
#include <QString>
#include <QUrl>
#include <QVariantList>
#include <QVariantMap>

// The model types appear in Q_PROPERTY declarations, and Qt's meta-type
// system needs a *complete* type for a pointer property -- a forward
// declaration is a static_assert failure, not a compile-time curiosity.
#include "homescreenmodel.h"
#include "librarymodel.h"

class ApiClient;

/// The screens inside the signed-in app, and which one is showing.
///
/// Navigation is here rather than in a QML StackView because the destinations
/// are a fixed set -- home, library, search, favourites, history, settings --
/// and a StackView would make "go home" a matter of unwinding an unknown
/// depth.  Back is therefore: the detail screen, or the rail you came from.
class AppController : public QObject
{
    Q_OBJECT
    Q_PROPERTY(Screen screen READ screen NOTIFY screenChanged)
    Q_PROPERTY(HomeScreenModel *home READ home CONSTANT)
    Q_PROPERTY(MediaListModel *library READ library CONSTANT)
    Q_PROPERTY(MediaListModel *favorites READ favorites CONSTANT)
    Q_PROPERTY(MediaListModel *history READ history CONSTANT)
    Q_PROPERTY(MediaListModel *searchResults READ searchResults CONSTANT)
    Q_PROPERTY(QVariantMap detail READ detail NOTIFY detailChanged)
    Q_PROPERTY(int detailMediaId READ detailMediaId NOTIFY detailChanged)
    Q_PROPERTY(QString searchQuery READ searchQuery NOTIFY searchQueryChanged)
    Q_PROPERTY(bool busy READ isBusy NOTIFY busyChanged)
    Q_PROPERTY(QString lastError READ lastError NOTIFY lastErrorChanged)
    Q_PROPERTY(QUrl baseUrl READ baseUrl NOTIFY baseUrlChanged)

    // Filters and pagination for the library grid.  Held here rather than in
    // the QML screen because "which page am I on" and "how many titles does
    // the server say there are" are facts about the session, not about a view:
    // leaving the screen and coming back should land on the same page.
    Q_PROPERTY(int libraryOffset READ libraryOffset NOTIFY libraryPageChanged)
    Q_PROPERTY(int libraryTotal READ libraryTotal NOTIFY libraryPageChanged)
    Q_PROPERTY(bool libraryHasMore READ libraryHasMore NOTIFY libraryPageChanged)
    Q_PROPERTY(QString librarySort READ librarySort NOTIFY libraryPageChanged)
    Q_PROPERTY(QString libraryGenre READ libraryGenre NOTIFY libraryPageChanged)
    Q_PROPERTY(QString libraryYear READ libraryYear NOTIFY libraryPageChanged)
    Q_PROPERTY(int libraryPageSize READ libraryPageSize CONSTANT)
    /// What the player is showing, or 0 when it is closed.  The player screen
    /// reads this rather than being handed an id, so a change of title while
    /// it is open cannot leave it pointing at the previous one.
    Q_PROPERTY(int playerMediaId READ playerMediaId NOTIFY screenChanged)
    /// What the player is showing, or 0 when it is closed.  The player screen
    /// reads this rather than being handed an id, so a change of title while
    /// it is open cannot leave it pointing at the previous one.
    Q_PROPERTY(int playerMediaId READ playerMediaId NOTIFY screenChanged)

public:
    enum Screen {
        Home = 0, Library = 1, Search = 2, Favorites = 3,
        History = 4, Settings = 5, Player = 6, Detail = 7
    };
    Q_ENUM(Screen)

    /// How many titles one library page holds.  Sixty is about one screenful
    /// on a desktop and about four on a phone; a bigger page would spend the
    /// response budget on posters nobody has scrolled to yet.
    static constexpr int kPageSize = 60;

    explicit AppController(ApiClient *api, QObject *parent = nullptr);

    Screen screen() const { return m_screen; }
    HomeScreenModel *home() const { return m_home; }
    MediaListModel *library() const { return m_library; }
    MediaListModel *favorites() const { return m_favorites; }
    MediaListModel *history() const { return m_history; }
    MediaListModel *searchResults() const { return m_searchResults; }

    QVariantMap detail() const { return m_detail; }
    int detailMediaId() const { return m_detailId; }
    QString searchQuery() const { return m_searchQuery; }
    bool isBusy() const { return m_busy; }
    QString lastError() const { return m_lastError; }
    QUrl baseUrl() const;

    int libraryOffset() const { return m_libraryOffset; }
    int libraryTotal() const { return m_libraryTotal; }
    bool libraryHasMore() const { return m_libraryHasMore; }
    QString librarySort() const { return m_librarySort; }
    QString libraryGenre() const { return m_libraryGenre; }
    QString libraryYear() const { return m_libraryYear; }
    int libraryPageSize() const { return kPageSize; }
    int playerMediaId() const { return m_playerMediaId; }

public slots:
    /// Called when a profile is chosen.  Everything the app shows is
    /// profile-scoped, so this is where the first load happens.
    void onProfileReady();

    void goHome();
    void goLibrary();
    void goSearch();
    void goFavorites();
    void goHistory();
    void goSettings();
    void openDetail(int mediaId);
    void back();

    void setSearchQuery(const QString &query);
    void submitSearch();
    void clearSearch();

    /// Ask for one more page and append it.  A silent no-op at the end of the
    /// list, so a scroll that overshoots does not spend a request.
    void loadMoreLibrary();
    void setLibrarySort(const QString &sort);
    void setLibraryGenre(const QString &genre);
    void setLibraryYear(const QString &year);
    void clearLibraryFilters();
    /// Loads genres, years and tags for the filter bar.  Cached: the list only
    /// changes when the library does, and it is already part of /library/home.
    void loadFilterOptions();

    void openPlayer(int mediaId);
    void closePlayer();

    void refresh();
    void toggleFavorite(int mediaId, bool favorite);
    void setRating(int mediaId, double score);

    /// Move to `screen` without going through a request.  `openDetail` fetches,
    /// so the detail screen only appears when the answer lands; a routing test
    /// needs to assert the *return* path, and that means being able to put the
    /// controller in a screen directly.
    void setScreenForTest(Screen screen) { setScreen(screen); }

signals:
    void screenChanged();
    void detailChanged();
    void searchQueryChanged();
    void busyChanged();
    void lastErrorChanged();
    void baseUrlChanged();
    void libraryPageChanged();
    void filterOptionsChanged();

private:
    void setScreen(Screen screen);
    void setBusy(bool busy);
    /// Ask the server for the library page at `m_libraryOffset`.  `append` adds
    /// to what is already on screen instead of replacing it.
    void requestLibraryPage(bool append);
    void setLastError(const QString &message);

    ApiClient *m_api;
    HomeScreenModel *m_home;
    MediaListModel *m_library;
    MediaListModel *m_favorites;
    MediaListModel *m_history;
    MediaListModel *m_searchResults;
    QVariantMap m_detail;
    int m_detailId = 0;
    QString m_searchQuery;
    bool m_busy = false;
    QString m_lastError;
    Screen m_screen = Home;
    Screen m_screenBeforeDetail = Home;

    int m_libraryOffset = 0;
    int m_libraryTotal = 0;
    bool m_libraryHasMore = false;
    QString m_librarySort = QStringLiteral("added");
    QString m_libraryGenre;
    QString m_libraryYear;
    QVariantMap m_filterOptions;
    /// True while the page in flight is an append rather than a replacement.
    /// The answer cannot tell us which it was, so the flag is read once, at
    /// the moment the answer lands, and cleared.
    bool m_libraryAppending = false;
    int m_playerMediaId = 0;
};