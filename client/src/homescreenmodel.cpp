#include "homescreenmodel.h"

#include "librarymodel.h"

HomeRail::HomeRail(QString title, QString emptyMessage, QObject *parent)
    : QObject(parent), m_title(std::move(title)),
      m_emptyMessage(std::move(emptyMessage)), m_model(new MediaListModel(this))
{
    m_model->setEmptyMessage(m_emptyMessage);
}

bool HomeRail::isVisible() const
{
    // A rail with nothing in it is not shown at all unless the request failed:
    // an empty row with a heading and nothing under it reads as a bug, and a
    // failed rail with a "could not load" message reads as information.
    return m_model->rowCount() > 0 || m_model->isFailed() || m_model->isLoading();
}

bool HomeRail::hasContent() const
{
    return m_model->rowCount() > 0;
}

QString HomeRail::errorMessage() const
{
    return m_model->errorMessage();
}

void HomeRail::setItems(const QJsonArray &items, MediaListModel::ProgressMode mode)
{
    m_model->setLoading(false);
    m_model->setItems(items, mode);
    emit contentChanged();
    emit visibilityChanged();
}

void HomeRail::setLoading(bool loading)
{
    m_model->setLoading(loading);
    emit visibilityChanged();
}

void HomeRail::setFailed(bool failed, const QString &message, bool retryable)
{
    m_model->setFailed(failed, message, retryable);
    emit contentChanged();
    emit visibilityChanged();
}

void HomeRail::retry()
{
    emit retryRequested();
}

HomeScreenModel::HomeScreenModel(QObject *parent) : QObject(parent)
{
    // The order is the screen.  Continue Watching is first because it is the
    // most common reason to open the app; Recently Added next because a new
    // library has nothing else to show; Recommended *after* it because
    // recommendations need history, a first-run profile has none, and an empty
    // rail above a populated one looks broken.
    m_continueWatching = new HomeRail(tr("Continue watching"),
                                      tr("Nothing in progress"), this);
    m_recentlyAdded = new HomeRail(tr("Recently added"),
                                   tr("Nothing added yet"), this);
    m_recommended = new HomeRail(tr("Recommended"),
                                 tr("Watch something and this fills up"), this);
    m_favorites = new HomeRail(tr("Favourites"),
                               tr("Titles you mark as favourites appear here"), this);
    m_popular = new HomeRail(tr("Popular"), tr("Nothing popular yet"), this);
    m_topRated = new HomeRail(tr("Top rated"), tr("Nothing rated yet"), this);
    m_recentlyWatched = new HomeRail(tr("Recently watched"),
                                     tr("Nothing watched yet"), this);
}

void HomeScreenModel::setBaseUrl(const QUrl &url)
{
    if (m_baseUrl == url)
        return;
    m_baseUrl = url;
    for (HomeRail *rail : rails())
        rail->model()->setBaseUrl(url);
}

QList<HomeRail *> HomeScreenModel::rails() const
{
    return {m_continueWatching, m_recentlyAdded, m_recommended, m_favorites,
            m_popular, m_topRated, m_recentlyWatched};
}

void HomeScreenModel::refresh()
{
    // A signal, not a request: this model holds no `ApiClient` and must not
    // acquire one.  Two objects that each hold a transport and each decide
    // what a profile is are how a request ends up going out with the wrong one.
    emit refreshRequested();
}

void HomeScreenModel::beginLoad()
{
    m_loading = true;
    m_failed = false;
    m_errorMessage.clear();
    emit loadingChanged();
    emit failedChanged();
    for (HomeRail *rail : rails())
        rail->setLoading(true);
}

void HomeScreenModel::dispatch(const QString &key, HomeRail *rail,
                               MediaListModel::ProgressMode mode)
{
    rail->setItems(m_payload.value(key).toArray(), mode);
}

void HomeScreenModel::accept(const QJsonObject &payload)
{
    m_payload = payload;
    m_loading = false;
    m_failed = false;
    m_errorMessage.clear();

    dispatch(QStringLiteral("continue_watching"), m_continueWatching,
             MediaListModel::ProgressMode::Fraction);
    dispatch(QStringLiteral("recently_added"), m_recentlyAdded,
             MediaListModel::ProgressMode::None);
    dispatch(QStringLiteral("recommended"), m_recommended,
             MediaListModel::ProgressMode::None);
    dispatch(QStringLiteral("favorites"), m_favorites,
             MediaListModel::ProgressMode::Fraction);
    dispatch(QStringLiteral("popular"), m_popular, MediaListModel::ProgressMode::None);
    dispatch(QStringLiteral("top_rated"), m_topRated, MediaListModel::ProgressMode::None);
    dispatch(QStringLiteral("recently_watched"), m_recentlyWatched,
             MediaListModel::ProgressMode::Remaining);

    // Categories come back with the rails, so the home screen gets its genre
    // shortcuts from the same request rather than a second one for a list that
    // changes when the library does.
    m_genres = payload.value(QStringLiteral("categories"))
                   .toArray().toVariantList();
    m_genreCount = m_genres.size();

    emit loadingChanged();
    emit failedChanged();
    emit contentChanged();
}

void HomeScreenModel::fail(const QString &message, bool retryable)
{
    m_loading = false;
    m_failed = true;
    m_errorMessage = message;
    m_retryable = retryable;
    for (HomeRail *rail : rails())
        rail->setFailed(true, QString(), retryable);
    emit loadingChanged();
    emit failedChanged();
    emit contentChanged();
}
