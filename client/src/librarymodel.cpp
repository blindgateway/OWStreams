#include "librarymodel.h"

#include <QDateTime>

namespace {

/// Runtime as `1 h 47 min` / `47 min`, the way a listing shows it rather than
/// as a raw minute count.
QString formatRuntime(int minutes)
{
    if (minutes <= 0)
        return {};
    if (minutes < 60)
        return QStringLiteral("%1 min").arg(minutes);
    const int hours = minutes / 60;
    const int rest = minutes % 60;
    if (rest == 0)
        return QStringLiteral("%1 h").arg(hours);
    return QStringLiteral("%1 h %2 min").arg(hours).arg(rest);
}

/// Subtitle under a poster: year, plus whatever the row already knows.
///
/// A card has room for one line, and a year is the one fact that is on almost
/// every title and tells you nothing about it in the poster.
QString buildSubtitle(const QVariantMap &row)
{
    const int year = row.value(QStringLiteral("year")).toInt();
    const QString quality = row.value(QStringLiteral("quality")).toString();
    if (year > 0 && !quality.isEmpty())
        return QStringLiteral("%1 · %2").arg(year).arg(quality);
    if (year > 0)
        return QString::number(year);
    return quality;
}

/// The absolute poster URL for a row, or an invalid QUrl.
///
/// Built from the poster *key* rather than trusting that a key implies a file:
/// the server answers 404 for a title whose artwork was never fetched, and a
/// row that knows it has no key must not ask for one on every scroll.
QUrl buildPosterUrl(const QUrl &baseUrl, int mediaId, const QString &posterKey)
{
    if (baseUrl.isEmpty() || mediaId <= 0 || posterKey.isEmpty())
        return {};
    return QUrl(baseUrl.toString() + QStringLiteral("/api/v1/media/")
                + QString::number(mediaId) + QStringLiteral("/poster"));
}

QStringList stringList(const QVariant &value)
{
    QStringList out;
    const QVariantList list = value.toList();
    for (const QVariant &item : list)
        out.append(item.toString());
    return out;
}

}  // namespace

MediaListModel::MediaListModel(QObject *parent) : QAbstractListModel(parent) {}

void MediaListModel::setBaseUrl(const QUrl &url)
{
    if (m_baseUrl == url)
        return;
    m_baseUrl = url;
    // Rows carry a poster *key*; the absolute URL is built from it, so every
    // row's URL just changed.
    if (!m_entries.isEmpty()) {
        emit dataChanged(index(0), index(int(m_entries.size()) - 1),
                         {PosterUrlRole});
    }
    emit baseUrlChanged();
}

QUrl MediaListModel::posterUrlFor(int mediaId) const
{
    for (const Entry &entry : m_entries) {
        if (entry.id == mediaId)
            return buildPosterUrl(m_baseUrl, mediaId,
                                  entry.values.value(QStringLiteral("posterKey")).toString());
    }
    return {};
}

int MediaListModel::rowCount(const QModelIndex &parent) const
{
    return parent.isValid() ? 0 : int(m_entries.size());
}

int MediaListModel::rowOfMedia(int mediaId) const
{
    if (mediaId <= 0)
        return -1;
    // A linear scan, not a lookup table.  A library page is sixty rows and a
    // press of "next" is not a hot path; a second structure kept in step with
    // this one is a second thing that can disagree about the order.
    for (int row = 0; row < m_entries.size(); ++row) {
        if (m_entries.at(row).values.value(QStringLiteral("id")).toInt() == mediaId)
            return row;
    }
    return -1;
}

int MediaListModel::mediaIdAt(int row) const
{
    if (row < 0 || row >= m_entries.size())
        return 0;
    return m_entries.at(row).values.value(QStringLiteral("id")).toInt();
}

QHash<int, QByteArray> MediaListModel::roleNames() const
{
    return {
        {IdRole, "mediaId"},
        {TitleRole, "title"},
        {SubtitleRole, "subtitle"},
        {OverviewRole, "overview"},
        {YearRole, "year"},
        {YearTextRole, "yearText"},
        {RuntimeRole, "runtimeMinutes"},
        {RuntimeTextRole, "runtimeText"},
        {PosterKeyRole, "posterKey"},
        {PosterUrlRole, "posterUrl"},
        {ProgressRole, "progress"},
        {ProgressTextRole, "progressText"},
        {FavoriteRole, "favorite"},
        {UserRatingRole, "userRating"},
        {RatingRole, "rating"},
        {RatingSourceRole, "ratingSource"},
        {QualityRole, "quality"},
        {GenresRole, "genres"},
        {MissingRole, "missing"},
    };
}

QVariant MediaListModel::data(const QModelIndex &index, int role) const
{
    if (!index.isValid() || index.row() < 0 || index.row() >= m_entries.size())
        return {};
    const QVariantMap &row = m_entries.at(index.row()).values;

    switch (role) {
    case IdRole:      return row.value(QStringLiteral("id"));
    case TitleRole:   return row.value(QStringLiteral("title"));
    case SubtitleRole:return row.value(QStringLiteral("subtitle"));
    case OverviewRole:return row.value(QStringLiteral("overview"));
    case YearRole:    return row.value(QStringLiteral("year"));
    case YearTextRole: {
        const int year = row.value(QStringLiteral("year")).toInt();
        return year > 0 ? QVariant(QString::number(year)) : QVariant();
    }
    case RuntimeRole:
        return row.value(QStringLiteral("runtime_minutes"));
    case RuntimeTextRole:
        return formatRuntime(row.value(QStringLiteral("runtime_minutes")).toInt());
    case PosterKeyRole:
        return row.value(QStringLiteral("poster_key"));
    case PosterUrlRole:
        return row.value(QStringLiteral("posterUrl"));
    case ProgressRole:
        return row.value(QStringLiteral("progress"));
    case ProgressTextRole:
        return row.value(QStringLiteral("progressText"));
    case FavoriteRole:
        return row.value(QStringLiteral("favorite"));
    case UserRatingRole:
        return row.value(QStringLiteral("userRating"));
    case RatingRole:
        return row.value(QStringLiteral("rating_value"));
    case RatingSourceRole:
        return row.value(QStringLiteral("rating_source"));
    case QualityRole:
        return row.value(QStringLiteral("quality"));
    case GenresRole:
        return stringList(row.value(QStringLiteral("genres")));
    case MissingRole:
        return row.value(QStringLiteral("missing"));
    default:
        return {};
    }
}

void MediaListModel::setLoading(bool loading)
{
    if (m_loading == loading)
        return;
    m_loading = loading;
    // A retry starts by clearing the error, so the spinner and the message do
    // not both claim the state of the request.
    if (loading) {
        m_failed = false;
        m_errorMessage.clear();
        emit failedChanged();
    }
    emit loadingChanged();
}

void MediaListModel::setEmptyMessage(const QString &message)
{
    if (m_emptyMessage == message)
        return;
    m_emptyMessage = message;
    emit emptyMessageChanged();
}

MediaListModel::Entry MediaListModel::buildEntry(const QJsonObject &object,
                                                  ProgressMode progress) const
{
    Entry entry;
    entry.progress = progress;
    entry.id = object.value(QStringLiteral("id")).toInt();
    entry.values = object.toVariantMap();

    entry.values.insert(QStringLiteral("subtitle"), buildSubtitle(entry.values));
    entry.values.insert(QStringLiteral("genres"),
                        object.value(QStringLiteral("genres")).toArray());

    // `percent` comes from the server for progress-bearing rails; without it
    // there is no progress to draw and the card is a plain poster.
    double fraction = 0.0;
    QString progressText;
    const QJsonValue percent = object.value(QStringLiteral("percent"));
    if (percent.isDouble()) {
        fraction = qBound(0.0, percent.toDouble(), 1.0);
        const QJsonObject history = object.value(QStringLiteral("progress")).toObject();
        const double position = history.value(QStringLiteral("position_seconds")).toDouble();
        const double duration = history.value(QStringLiteral("duration_seconds")).toDouble();
        if (duration > 0) {
            const double remaining = qMax(0.0, duration - position);
            progressText = QStringLiteral("%1 left").arg(
                formatRuntime(static_cast<int>(remaining / 60.0)));
        }
    }
    if (progress == ProgressMode::None) {
        fraction = 0.0;
        progressText.clear();
    } else if (progress == ProgressMode::Remaining && progressText.isEmpty()) {
        // A history rail that cannot say how much is left says nothing rather
        // than showing a bar with no label.
        fraction = 0.0;
    }
    entry.values.insert(QStringLiteral("progress"), fraction);
    entry.values.insert(QStringLiteral("progressText"), progressText);
    entry.values.insert(QStringLiteral("missing"),
                        object.value(QStringLiteral("integrity_state")).toString()
                            == QStringLiteral("missing"));

    // A row with no artwork gets no URL at all, rather than one that would 404
    // on every repaint of every delegate that shows it.
    const QString posterKey = object.value(QStringLiteral("poster_key")).toString();
    entry.values.insert(QStringLiteral("posterKey"), posterKey);
    entry.values.insert(QStringLiteral("posterUrl"),
                        buildPosterUrl(m_baseUrl, entry.id, posterKey));
    return entry;
}

void MediaListModel::setItems(const QJsonArray &items, ProgressMode progress)
{
    m_progress = progress;
    beginResetModel();
    m_entries.clear();
    m_entries.reserve(items.size());
    for (const QJsonValue &value : items) {
        if (value.isObject())
            m_entries.append(buildEntry(value.toObject(), progress));
    }
    endResetModel();
    emit countChanged();
}

void MediaListModel::appendItems(const QJsonArray &items)
{
    if (items.isEmpty())
        return;
    const int first = m_entries.size();
    m_entries.reserve(first + items.size());
    beginInsertRows(QModelIndex(), first, first + items.size() - 1);
    for (const QJsonValue &value : items) {
        if (value.isObject())
            m_entries.append(buildEntry(value.toObject(), m_progress));
    }
    endInsertRows();
    emit countChanged();
}

void MediaListModel::remove(int mediaId)
{
    for (int row = 0; row < m_entries.size(); ++row) {
        if (m_entries.at(row).id != mediaId)
            continue;
        beginRemoveRows(QModelIndex(), row, row);
        m_entries.remove(row);
        endRemoveRows();
        emit countChanged();
        return;
    }
}

void MediaListModel::clear()
{
    if (m_entries.isEmpty())
        return;
    beginResetModel();
    m_entries.clear();
    endResetModel();
    emit countChanged();
}

void MediaListModel::setFavorite(int mediaId, bool favorite)
{
    for (int row = 0; row < m_entries.size(); ++row) {
        if (m_entries.at(row).id != mediaId)
            continue;
        m_entries[row].values.insert(QStringLiteral("favorite"), favorite);
        // One row, one repaint: the whole rail does not flicker because a
        // heart was tapped.
        emit dataChanged(index(row), index(row), {FavoriteRole});
        return;
    }
}

QVariantMap MediaListModel::find(int mediaId) const
{
    for (const Entry &entry : m_entries) {
        if (entry.id == mediaId)
            return entry.values;
    }
    return {};
}

void MediaListModel::setFailed(bool failed, const QString &message, bool retryable)
{
    if (m_failed == failed && m_errorMessage == message)
        return;
    m_failed = failed;
    m_errorMessage = message;
    m_retryable = failed && retryable;
    emit failedChanged();
}
