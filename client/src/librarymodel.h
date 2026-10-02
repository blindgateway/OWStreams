#pragma once

#include <QtQml/qqmlregistration.h>

#include <QAbstractListModel>
#include <QJsonArray>
#include <QJsonObject>
#include <QObject>
#include <QUrl>
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
    QML_ELEMENT
    QML_UNCREATABLE("Provided by AppController; not constructible from QML.")
    Q_PROPERTY(int count READ rowCount NOTIFY countChanged)
    Q_PROPERTY(bool loading READ isLoading WRITE setLoading NOTIFY loadingChanged)
    Q_PROPERTY(bool failed READ isFailed NOTIFY failedChanged)
    Q_PROPERTY(QString errorMessage READ errorMessage NOTIFY failedChanged)
    Q_PROPERTY(bool retryable READ isRetryable NOTIFY failedChanged)
    Q_PROPERTY(QString emptyMessage READ emptyMessage WRITE setEmptyMessage
                   NOTIFY emptyMessageChanged)
    /// Where a row's poster lives.  Absolute, because the poster route is
    /// authenticated and an `Image` cannot send a header -- the access manager
    /// on the ApiClient attaches the token.  `setBaseUrl` is how that manager
    /// is told which server it belongs to, and setting it must repaint the
    /// rows: a model filled before the server was known holds relative URLs.
    Q_PROPERTY(QUrl baseUrl READ baseUrl WRITE setBaseUrl NOTIFY baseUrlChanged)

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

    /// The row showing `mediaId`, or -1.  What "next" and "previous" are
    /// resolved against: a person presses them about the film they are
    /// watching, and the model holds the order they are looking at.
    Q_INVOKABLE int rowOfMedia(int mediaId) const;
    /// The media id at `row`, or 0 for a row that is not there.  The pair above,
    /// named so neither can be used without the other.
    Q_INVOKABLE int mediaIdAt(int row) const;
    QVariant data(const QModelIndex &index, int role) const override;
    QHash<int, QByteArray> roleNames() const override;

    bool isLoading() const { return m_loading; }
    void setLoading(bool loading);
    bool isFailed() const { return m_failed; }
    bool isRetryable() const { return m_retryable; }
    QString errorMessage() const { return m_errorMessage; }
    QString emptyMessage() const { return m_emptyMessage; }
    void setEmptyMessage(const QString &message);

    QUrl baseUrl() const { return m_baseUrl; }
    void setBaseUrl(const QUrl &url);
    /// Build the absolute poster URL for `mediaId`, or an empty URL when the
    /// row carries no poster key -- so an `Image` is never pointed at a route
    /// that is guaranteed to 404.
    QUrl posterUrlFor(int mediaId) const;

    /// Replace the contents.  `role` decides what the progress bar shows:
    /// a browse rail has no progress, a Continue Watching rail is mostly
    /// progress, and a history rail is sorted by it.
    enum class ProgressMode { None, Fraction, Remaining };

    void setItems(const QJsonArray &items, ProgressMode progress = ProgressMode::None);
    /// Add to the end without resetting.  A `beginResetModel` on every page
    /// would empty a thousand-poster grid for the length of a request.
    void appendItems(const QJsonArray &items);
    /// Drop one row, for an entry the server has just removed.
    void remove(int mediaId);
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
    void baseUrlChanged();

private:
    struct Entry {
        QVariantMap values;
        int id = 0;
        ProgressMode progress = ProgressMode::None;
    };

    /// Turn one server row into a delegate-ready entry.  Shared by `setItems`
    /// and `appendItems` so a page-two poster cannot disagree with a page-one
    /// one about its subtitle or its progress bar.
    Entry buildEntry(const QJsonObject &object, ProgressMode progress) const;

    QVector<Entry> m_entries;
    bool m_loading = false;
    bool m_failed = false;
    bool m_retryable = false;
    QString m_errorMessage;
    QString m_emptyMessage;
    QUrl m_baseUrl;
    ProgressMode m_progress = ProgressMode::None;
};