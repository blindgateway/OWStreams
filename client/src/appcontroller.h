#pragma once

#include <QJsonArray>
#include <QObject>
#include <QString>
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

public:
    enum Screen { Home, Library, Search, Favorites, History, Settings, Detail };
    Q_ENUM(Screen)

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

    void refresh();
    void toggleFavorite(int mediaId, bool favorite);
    void setRating(int mediaId, double score);

signals:
    void screenChanged();
    void detailChanged();
    void searchQueryChanged();
    void busyChanged();
    void lastErrorChanged();

private:
    void setScreen(Screen screen);
    void setBusy(bool busy);

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
};