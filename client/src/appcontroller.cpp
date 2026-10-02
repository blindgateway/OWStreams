#include "appcontroller.h"

#include "apiclient.h"
#include "homescreenmodel.h"
#include "librarymodel.h"

AppController::AppController(ApiClient *api, QObject *parent)
    : QObject(parent), m_api(api),
      m_home(new HomeScreenModel(this)),
      m_library(new MediaListModel(this)),
      m_favorites(new MediaListModel(this)),
      m_history(new MediaListModel(this)),
      m_searchResults(new MediaListModel(this))
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
    connect(m_api, &ApiClient::mediaPageLoaded, this, [this](const QJsonObject &payload) {
        m_library->setItems(payload.value(QStringLiteral("items")).toArray());
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
        setScreen(Detail);
        setBusy(false);
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
    // Remember where "back" goes from a detail screen.  Everything else is a
    // top-level destination with no back of its own.
    if (m_screen != Detail)
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
    if (m_library->rowCount() == 0) {
        m_library->setLoading(true);
        m_api->loadMediaPage(60, 0);
    }
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
}

void AppController::openDetail(int mediaId)
{
    setBusy(true);
    m_api->loadMedia(mediaId);
}

void AppController::back()
{
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
