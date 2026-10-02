#pragma once

#include <QJsonArray>
#include <QJsonObject>
#include <QObject>
#include <QVector>

#include "librarymodel.h"

/// One rail on the home screen.
///
/// Each rail is its own model rather than one model with a section header,
/// because the rails load at different times and a section header in a single
/// model means a reset that empties the screen while one rail is still
/// loading.
class HomeRail : public QObject
{
    Q_OBJECT
    Q_PROPERTY(MediaListModel *model READ model CONSTANT)
    Q_PROPERTY(QString title READ title CONSTANT)
    Q_PROPERTY(bool visible READ isVisible NOTIFY visibilityChanged)
    Q_PROPERTY(bool hasContent READ hasContent NOTIFY contentChanged)

public:
    HomeRail(QString title, QString emptyMessage, QObject *parent = nullptr);

    MediaListModel *model() const { return m_model; }
    QString title() const { return m_title; }
    bool isVisible() const;
    bool hasContent() const;

    /// Feed the rail its items.  `progressMode` is the rail's own: Continue
    /// Watching and Recently Watched carry progress, the rest do not.
    void setItems(const QJsonArray &items, MediaListModel::ProgressMode mode);
    void setLoading(bool loading);
    void setFailed(bool failed, const QString &message, bool retryable);
    void retry();

signals:
    void visibilityChanged();
    void contentChanged();
    void retryRequested();

private:
    QString m_title;
    QString m_emptyMessage;
    MediaListModel *m_model;
};

/// The home screen: every rail, in the order a person would look for them.
///
/// The order is a design decision, not an accident: what you were watching is
/// at the top because that is the most likely reason to open the app, and
/// "recommended" sits below Recently Added because a fresh library has no
/// recommendations and an empty rail above a populated one looks broken.
class HomeScreenModel : public QObject
{
    Q_OBJECT
    Q_PROPERTY(HomeRail *continueWatching READ continueWatching CONSTANT)
    Q_PROPERTY(HomeRail *recentlyAdded READ recentlyAdded CONSTANT)
    Q_PROPERTY(HomeRail *recommended READ recommended CONSTANT)
    Q_PROPERTY(HomeRail *favorites READ favorites CONSTANT)
    Q_PROPERTY(HomeRail *popular READ popular CONSTANT)
    Q_PROPERTY(HomeRail *topRated READ topRated CONSTANT)
    Q_PROPERTY(HomeRail *recentlyWatched READ recentlyWatched CONSTANT)
    Q_PROPERTY(bool loading READ isLoading NOTIFY loadingChanged)
    Q_PROPERTY(bool failed READ isFailed NOTIFY failedChanged)
    Q_PROPERTY(QString errorMessage READ errorMessage NOTIFY failedChanged)
    Q_PROPERTY(bool retryable READ isRetryable NOTIFY failedChanged)
    Q_PROPERTY(int genreCount READ genreCount NOTIFY contentChanged)

public:
    explicit HomeScreenModel(QObject *parent = nullptr);

    HomeRail *continueWatching() const { return m_continueWatching; }
    HomeRail *recentlyAdded() const { return m_recentlyAdded; }
    HomeRail *recommended() const { return m_recommended; }
    HomeRail *favorites() const { return m_favorites; }
    HomeRail *popular() const { return m_popular; }
    HomeRail *topRated() const { return m_topRated; }
    HomeRail *recentlyWatched() const { return m_recentlyWatched; }

    bool isLoading() const { return m_loading; }
    bool isFailed() const { return m_failed; }
    QString errorMessage() const { return m_errorMessage; }
    bool isRetryable() const { return m_retryable; }
    int genreCount() const { return m_genreCount; }

    QList<HomeRail *> rails() const;

    void beginLoad();
    void accept(const QJsonObject &payload);
    void fail(const QString &message, bool retryable);

signals:
    void loadingChanged();
    void failedChanged();
    void contentChanged();

private:
    void dispatch(const QString &key, HomeRail *rail,
                  MediaListModel::ProgressMode mode);

    HomeRail *m_continueWatching;
    HomeRail *m_recentlyAdded;
    HomeRail *m_recommended;
    HomeRail *m_favorites;
    HomeRail *m_popular;
    HomeRail *m_topRated;
    HomeRail *m_recentlyWatched;
    bool m_loading = false;
    bool m_failed = false;
    bool m_retryable = false;
    QString m_errorMessage;
    int m_genreCount = 0;
    QJsonObject m_payload;
};