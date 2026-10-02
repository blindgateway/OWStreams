#include "appcontroller.h"

#include <QUrlQuery>

#include <QUrlQuery>

#include "apiclient.h"
#include "homescreenmodel.h"
#include "librarymodel.h"

AppController::AppController(ApiClient *api, QObject *parent)
    : QObject(parent), m_api(api),
      m_home(new HomeScreenModel(this)),
      m_library(new MediaListModel(this)),
      m_favorites(new MediaListModel(this)),
      m_history(new MediaListModel(this)),
      m_searchResults(new MediaListModel(this)),
      m_player(new PlaybackController(api, this))
{
    m_library->setEmptyMessage(tr("Nothing in the library yet."));
    m_favorites->setEmptyMessage(tr("No favourites yet."));
    m_history->setEmptyMessage(tr("Nothing watched yet."));
    m_searchResults->setEmptyMessage(tr("No titles matched."));

    connect(m_api, &ApiClient::homeLoaded, this, [this](const QJsonObject &payload) {
        m_home->accept(payload);
        setBusy(false);
        m_lastError.clear();
        emit lastErrorChanged();
    });
    connect(m_api, &ApiClient::baseUrlChanged, this, [this] {
        // Posters are absolute URLs built from this, so every model has to be
        // told.  A server switch invalidates them all: the new server has its
        // own media ids and its own credentials.
        const QUrl url(m_api->baseUrl());
        m_home->setBaseUrl(url);
        m_library->setBaseUrl(url);
        m_favorites->setBaseUrl(url);
        m_history->setBaseUrl(url);
        m_searchResults->setBaseUrl(url);
        emit baseUrlChanged();
    });

    connect(m_api, &ApiClient::mediaPageLoaded, this, [this](const QJsonObject &payload) {
        const QJsonArray items = payload.value(QStringLiteral("items")).toArray();
        m_libraryTotal = payload.value(QStringLiteral("total")).toInt();
        m_libraryHasMore = payload.value(QStringLiteral("has_more")).toBool();

        if (m_libraryAppending)
            m_library->appendItems(items);
        else
            m_library->setItems(items);

        m_libraryOffset = m_library->rowCount();
        emit libraryPageChanged();
        m_library->setLoading(false);
        setBusy(false);
    });
    connect(m_api, &ApiClient::favoritesLoaded, this, [this](const QJsonObject &payload) {
        m_favorites->setItems(payload.value(QStringLiteral("items")).toArray(),
                              MediaListModel::ProgressMode::Fraction);
        m_favorites->setLoading(false);
        setBusy(false);
    });
    connect(m_api, &ApiClient::historyLoaded, this, [this](const QJsonObject &payload) {
        m_history->setItems(payload.value(QStringLiteral("items")).toArray(),
                            MediaListModel::ProgressMode::Remaining);
        m_history->setLoading(false);
        setBusy(false);
    });
    connect(m_api, &ApiClient::searchLoaded, this, [this](const QJsonObject &payload) {
        m_searchResults->setItems(payload.value(QStringLiteral("items")).toArray());
        m_searchResults->setLoading(false);
        setBusy(false);
    });
    connect(m_api, &ApiClient::mediaLoaded, this, [this](const QJsonObject &media) {
        m_detail = media.toVariantMap();
        m_detailId = m_detail.value(QStringLiteral("id")).toInt();
        emit detailChanged();
        setBusy(false);
        if (m_pendingPlayerMediaId == m_detailId && m_pendingPlayerMediaId > 0) {
            // Play was pressed on a rail, not on this detail screen: the screen
            // the viewer was looking at stays behind the player.
            m_playerMediaId = m_detailId;
            m_pendingPlayerMediaId = 0;
            m_player->open(m_detailId, m_detail);
            setScreen(Player);
            return;
        }
        m_pendingPlayerMediaId = 0;
        setScreen(Detail);
    });
    connect(m_api, &ApiClient::categoriesLoaded, this,
            [this](const QJsonObject &payload) {
        m_filterOptions = payload.toVariantMap();
        emit filterOptionsChanged();
    });
    connect(m_api, &ApiClient::historyCleared, this, [this] {
        m_history->clear();
        m_favorites->clear();
        refresh();
    });
    connect(m_api, &ApiClient::historyEntryRemoved, this, [this](int mediaId) {
        m_history->remove(mediaId);
        m_favorites->remove(mediaId);
    });
    connect(m_api, &ApiClient::categoriesLoaded, this,
            [this](const QJsonObject &payload) {
        m_filterOptions = payload.toVariantMap();
        emit filterOptionsChanged();
    });
    connect(m_api, &ApiClient::historyCleared, this, [this] {
        m_history->clear();
        m_favorites->clear();
        refresh();
    });
    connect(m_api, &ApiClient::historyEntryRemoved, this, [this](int mediaId) {
        m_history->remove(mediaId);
        m_favorites->remove(mediaId);
    });
    connect(m_api, &ApiClient::favoriteChanged, this,
            [this](int mediaId, bool favorite) {
        m_library->setFavorite(mediaId, favorite);
        m_favorites->setFavorite(mediaId, favorite);
        if (m_detailId == mediaId) {
            m_detail.insert(QStringLiteral("is_favorite"), favorite);
            emit detailChanged();
        }
        // A heart tapped on a home rail also changes the favourites rail, but
        // only if that rail has been fetched; a stale heart on a rail nobody
        // has loaded is invisible, and refreshing it would fight the scroll.
    });
    connect(m_api, &ApiClient::failed, this,
            [this](const QString &operation, const ApiError &error) {
        if (operation == QLatin1String("library.home")) {
            m_home->fail(error.message, error.isRetryable());
        } else if (operation == QLatin1String("library.page")) {
            m_library->setFailed(true, error.message, error.isRetryable());
            m_library->setLoading(false);
            // The page we asked for never arrived, so the *next* request has to
            // start from the offset we actually hold rather than from the one
            // the failed request claimed.
            m_libraryOffset = m_library->rowCount();
            // The page we asked for never arrived, so the *next* request has to
            // start from the offset we actually hold rather than from the one
            // the failed request claimed.
            m_libraryOffset = m_library->rowCount();
        } else if (operation == QLatin1String("library.favorites")) {
            m_favorites->setFailed(true, error.message, error.isRetryable());
            m_favorites->setLoading(false);
        } else if (operation == QLatin1String("library.history")) {
            m_history->setFailed(true, error.message, error.isRetryable());
            m_history->setLoading(false);
        } else if (operation == QLatin1String("library.search")) {
            m_searchResults->setFailed(true, error.message, error.isRetryable());
            m_searchResults->setLoading(false);
        } else if (operation == QLatin1String("library.media")) {
            // A detail screen that could not load shows the failure; it does
            // not show an empty poster and a title of "Unknown".
            m_detail.clear();
            m_detailId = 0;
            emit detailChanged();
        }
        setBusy(false);
        m_lastError = error.message;
        emit lastErrorChanged();
    });
}

void AppController::onProfileReady()
{
    goHome();
    refresh();
}

void AppController::setScreen(Screen screen)
{
    if (m_screen == screen)
        return;
    // Remember where "back" goes.  Everything except the player is a
    // top-level destination with no back of its own, and the player is a layer
    // over whatever opened it.
    if (m_screen != Player)
        m_screenBeforeDetail = m_screen;
    m_screen = screen;
    emit screenChanged();
}

void AppController::setBusy(bool busy)
{
    if (m_busy == busy)
        return;
    m_busy = busy;
    emit busyChanged();
}

void AppController::goHome()
{
    setScreen(Home);
    // Only fetch when the rails are empty: coming back to home should not
    // re-request 500 titles you are already looking at.
    if (m_home->rails().isEmpty()
        || m_home->rails().first()->model()->rowCount() == 0) {
        refresh();
    }
}

void AppController::goLibrary()
{
    setScreen(Library);
    loadFilterOptions();
    if (m_library->rowCount() == 0)
        requestLibraryPage(false);
}

void AppController::goSearch()
{
    setScreen(Search);
}

void AppController::goFavorites()
{
    setScreen(Favorites);
    m_favorites->setLoading(true);
    m_api->loadFavorites();
}

void AppController::goHistory()
{
    setScreen(History);
    m_history->setLoading(true);
    m_api->loadHistory();
}

void AppController::goSettings()
{
    setScreen(Settings);
    // The session list changes when a device is revoked from a browser, so it
    // is fetched on entry rather than cached from sign-in.
    m_api->loadSessions();
}

void AppController::requestLibraryPage(bool append)
{
    if (!m_libraryHasMore && append)
        return;   // the end of the list is not a request

    m_libraryAppending = append;
    m_library->setLoading(true);
    setBusy(true);

    // The filters go on the query string, because the server is what decides
    // what "genre=Science Fiction, sorted by year" means -- a client-side
    // filter would have to download the whole library to apply it.
    QUrlQuery query;
    query.addQueryItem(QStringLiteral("limit"), QString::number(kPageSize));
    query.addQueryItem(QStringLiteral("offset"), QString::number(m_libraryOffset));
    query.addQueryItem(QStringLiteral("sort"), m_librarySort);
    if (!m_libraryGenre.isEmpty())
        query.addQueryItem(QStringLiteral("genre"), m_libraryGenre);
    if (!m_libraryYear.isEmpty())
        query.addQueryItem(QStringLiteral("year"), m_libraryYear);
    m_api->loadMediaPage(query);
}

void AppController::loadMoreLibrary()
{
    if (!m_libraryHasMore || m_library->isLoading())
        return;
    requestLibraryPage(true);
}

void AppController::setLibrarySort(const QString &sort)
{
    if (m_librarySort == sort)
        return;
    m_librarySort = sort;
    emit libraryPageChanged();
    requestLibraryPage(false);
}

void AppController::setLibraryGenre(const QString &genre)
{
    if (m_libraryGenre == genre)
        return;
    m_libraryGenre = genre;
    emit libraryPageChanged();
    requestLibraryPage(false);
}

void AppController::setLibraryYear(const QString &year)
{
    if (m_libraryYear == year)
        return;
    m_libraryYear = year;
    emit libraryPageChanged();
    requestLibraryPage(false);
}

void AppController::clearLibraryFilters()
{
    if (m_libraryGenre.isEmpty() && m_libraryYear.isEmpty())
        return;
    m_libraryGenre.clear();
    m_libraryYear.clear();
    emit libraryPageChanged();
    requestLibraryPage(false);
}

void AppController::loadFilterOptions()
{
    if (!m_filterOptions.isEmpty())
        return;
    m_api->loadCategories();
}

void AppController::openPlayer(int mediaId)
{
    // From a rail the controller only has an id, so the full record is fetched
    // first: the player needs the stored position to resume, and a resume from
    // zero is the bug that makes "continue watching" lie.
    if (m_detailId == mediaId && !m_detail.isEmpty()) {
        m_playerMediaId = mediaId;
        m_player->open(mediaId, m_detail);
        setScreen(Player);
        return;
    }
    m_pendingPlayerMediaId = mediaId;
    setBusy(true);
    m_api->loadMedia(mediaId);
}

void AppController::closePlayer()
{
    m_player->close();
    m_playerMediaId = 0;
    // Back to whatever opened the player, which is almost always the detail
    // screen -- the player is a layer over it, not a destination.
    setScreen(m_screenBeforeDetail == Player ? Home : m_screenBeforeDetail);
}

QUrl AppController::baseUrl() const
{
    return QUrl(m_api->baseUrl());
}

void AppController::openDetail(int mediaId)
{
    setBusy(true);
    m_api->loadMedia(mediaId);
}

void AppController::back()
{
    if (m_screen == Player) {
        closePlayer();
        return;
    }
    if (m_screen == Detail) {
        setScreen(m_screenBeforeDetail);
        return;
    }
    goHome();
}

void AppController::setSearchQuery(const QString &query)
{
    if (m_searchQuery == query)
        return;
    m_searchQuery = query;
    emit searchQueryChanged();
}

void AppController::submitSearch()
{
    if (m_searchQuery.trimmed().isEmpty()) {
        m_searchResults->clear();
        return;
    }
    setScreen(Search);
    m_searchResults->setLoading(true);
    m_api->search(m_searchQuery.trimmed(), 60);
}

void AppController::clearSearch()
{
    m_searchQuery.clear();
    m_searchResults->clear();
    emit searchQueryChanged();
}

void AppController::refresh()
{
    m_home->beginLoad();
    setBusy(true);
    m_api->loadHome(20);
}

void AppController::toggleFavorite(int mediaId, bool favorite)
{
    m_api->setFavorite(mediaId, favorite);
}

void AppController::setRating(int mediaId, double score)
{
    m_api->rateMedia(mediaId, score);
}
