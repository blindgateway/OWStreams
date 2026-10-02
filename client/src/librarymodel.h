#pragma once

#include <QAbstractListModel>
#include <QJsonArray>
#include <QJsonObject>
#include <QObject>
#include <QVariantMap>
#include <QVector>

/// One screen's worth of media, as a list model.
///
/// QML cannot index into a `QVariantList` of maps without re-reading and
/// re-resolving every property on every frame of a scroll, and it cannot
/// update one poster in place either.  A model can: roles change one poster,
/// and `dataChanged` is scoped to the row that changed.
///
/// Roles exist instead of property names because a QML delegate binding to
/// `model.title` is a lookup by name on every repaint; a role index is not.
class MediaListModel : public QAbstractListModel
{
    Q_OBJECT
    Q_PROPERTY(int count READ rowCount NOTIFY countChanged)
    Q_PROPERTY(bool loading READ isLoading WRITE setLoading NOTIFY loadingChanged)
    Q_PROPERTY(bool failed READ isFailed NOTIFY failedChanged)
    Q_PROPERTY(QString errorMessage READ errorMessage NOTIFY failedChanged)
    Q_PROPERTY(bool retryable READ isRetryable NOTIFY failedChanged)
    Q_PROPERTY(QString emptyMessage READ emptyMessage WRITE setEmptyMessage
                   NOTIFY emptyMessageChanged)

public:
    enum Roles {
        IdRole = Qt::UserRole + 1,
        TitleRole,
        SubtitleRole,
        OverviewRole,
        YearRole,
        YearTextRole,
        RuntimeRole,
        RuntimeTextRole,
        PosterKeyRole,
        PosterUrlRole,
        ProgressRole,
        ProgressTextRole,
        FavoriteRole,
        UserRatingRole,
        RatingRole,
        RatingSourceRole,
        QualityRole,
        GenresRole,
        MissingRole,
    };
    Q_ENUM(Roles)

    explicit MediaListModel(QObject *parent = nullptr);

    int rowCount(const QModelIndex &parent = {}) const override;
    QVariant data(const QModelIndex &index, int role) const override;
    QHash<int, QByteArray> roleNames() const override;

    bool isLoading() const { return m_loading; }
    void setLoading(bool loading);
    bool isFailed() const { return m_failed; }
    bool isRetryable() const { return m_retryable; }
    QString errorMessage() const { return m_errorMessage; }
    QString emptyMessage() const { return m_emptyMessage; }
    void setEmptyMessage(const QString &message);

    /// Replace the contents.  `role` decides what the progress bar shows:
    /// a browse rail has no progress, a Continue Watching rail is mostly
    /// progress, and a history rail is sorted by it.
    enum class ProgressMode { None, Fraction, Remaining };

    void setItems(const QJsonArray &items, ProgressMode progress = ProgressMode::None);
    void clear();
    /// Flip one title's favourite flag without refetching the rail.
    void setFavorite(int mediaId, bool favorite);

    /// Record a failure.  `retryable` comes from the error kind, not from the
    /// screen: a 404 or a 403 will be exactly as true in thirty seconds.
    void setFailed(bool failed, const QString &message, bool retryable);
    /// The whole row for one id, or an empty map.  The detail screen reads the
    /// same model rather than making its own request.
    QVariantMap find(int mediaId) const;

signals:
    void countChanged();
    void loadingChanged();
    void failedChanged();
    void emptyMessageChanged();

private:
    struct Entry {
        QVariantMap values;
        int id = 0;
        ProgressMode progress = ProgressMode::None;
    };

    QVector<Entry> m_entries;
    bool m_loading = false;
    bool m_failed = false;
    bool m_retryable = false;
    QString m_errorMessage;
    QString m_emptyMessage;
};