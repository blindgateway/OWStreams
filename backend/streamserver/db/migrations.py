"""Schema migrations.

Rules followed here:

* A migration is never edited after release.  Bugs get a *new* migration.
* Every statement is idempotent-ish (IF NOT EXISTS / OR IGNORE) so a partially
  applied migration can be replayed by hand if an operator needs to.
* Destructive changes (DROP COLUMN / DROP TABLE) live in their own migration so
  they can be reviewed in isolation.

Layout on the target hardware: the database file and the ``images`` tree live on
the 10 GB SSD, media files live on the 1 TB HDD and are referenced by absolute
path after being resolved against ``media.library_roots``.  Nothing large is
ever stored as a BLOB in SQLite -- posters are files, not rows.
"""

from __future__ import annotations

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "initial_core",
        """
        -- ------------------------------------------------------------------
        -- Accounts and profiles
        -- ------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS users (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            username                TEXT    NOT NULL UNIQUE COLLATE NOCASE,
            email                   TEXT    UNIQUE COLLATE NOCASE,
            display_name            TEXT    NOT NULL DEFAULT '',
            password_hash           TEXT    NOT NULL,
            password_changed_at     TEXT,
            must_change_password    INTEGER NOT NULL DEFAULT 0,
            is_admin                INTEGER NOT NULL DEFAULT 0,
            is_active               INTEGER NOT NULL DEFAULT 1,
            autoplay                TEXT    NOT NULL DEFAULT 'all',
            preferred_audio_lang    TEXT,
            preferred_subtitle_lang TEXT,
            created_at              TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at              TEXT    NOT NULL DEFAULT (datetime('now')),
            last_login_at           TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active);

        CREATE TABLE IF NOT EXISTS profiles (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name          TEXT    NOT NULL,
            avatar_id     TEXT,
            -- Only a scrypt hash is stored; see security/passwords.py.
            pin_hash      TEXT,
            pin_salt      TEXT,
            pin_updated_at TEXT,
            is_kids       INTEGER NOT NULL DEFAULT 0,
            max_watch_minutes INTEGER,
            sort_order    INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            UNIQUE (user_id, name)
        );
        CREATE INDEX IF NOT EXISTS idx_profiles_user ON profiles(user_id, sort_order);

        -- ------------------------------------------------------------------
        -- Sessions
        -- ------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS sessions (
            id                  TEXT PRIMARY KEY,
            user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            refresh_token_hash  TEXT    NOT NULL,
            device_name         TEXT,
            client_version      TEXT,
            ip_address          TEXT,
            user_agent          TEXT,
            created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
            last_used_at        TEXT    NOT NULL DEFAULT (datetime('now')),
            expires_at          TEXT    NOT NULL,
            revoked_at          TEXT,
            revoked_reason      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, revoked_at);
        CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

        CREATE TABLE IF NOT EXISTS revoked_tokens (
            jti             TEXT PRIMARY KEY,
            expires_at      TEXT NOT NULL,
            reason          TEXT,
            revoked_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_revoked_expiry ON revoked_tokens(expires_at);

        -- Brute-force throttling for both usernames and source addresses.
        CREATE TABLE IF NOT EXISTS login_attempts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT NOT NULL,
            ip_address  TEXT,
            successful  INTEGER NOT NULL DEFAULT 0,
            reason       TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_login_attempts ON login_attempts(username, created_at);
        CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip_address, created_at);

        -- ------------------------------------------------------------------
        -- Library
        -- ------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS media (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            -- Stable public identifier used by the API (never the path).
            slug              TEXT    NOT NULL UNIQUE,
            title             TEXT    NOT NULL,
            original_title    TEXT,
            sort_title        TEXT    NOT NULL DEFAULT '',
            overview          TEXT    NOT NULL DEFAULT '',
            tagline           TEXT,
            year              INTEGER,
            release_date      TEXT,
            runtime_minutes   INTEGER,
            content_type      TEXT    NOT NULL DEFAULT 'movie'
                              CHECK (content_type IN ('movie','series','episode')),
            parental_rating   TEXT,

            rating_value      REAL,
            rating_source     TEXT,
            rating_vote_count INTEGER,

            -- Media file, absolute path resolved under a library root.
            media_path        TEXT    NOT NULL UNIQUE,
            media_root        TEXT    NOT NULL,
            file_size_bytes   INTEGER,
            file_modified_at  TEXT,
            file_inode        TEXT,

            video_codec       TEXT,
            video_profile     TEXT,
            width             INTEGER,
            height            INTEGER,
            aspect_ratio      TEXT,
            frame_rate        REAL,
            bitrate_bps       INTEGER,
            hdr_format        TEXT,
            color_space       TEXT,
            audio_codec       TEXT,
            audio_channels    INTEGER,
            audio_languages   TEXT,
            subtitle_languages TEXT,
            container         TEXT,

            -- Artwork lives on disk; these are relative keys into image_dir.
            poster_key        TEXT,
            backdrop_key      TEXT,
            logo_key          TEXT,

            -- Owning series for episodes.
            parent_media_id   INTEGER REFERENCES media(id) ON DELETE CASCADE,

            source             TEXT NOT NULL DEFAULT 'scan'
                               CHECK (source IN ('scan','import','api','manual')),
            external_ids       TEXT NOT NULL DEFAULT '{}',
            extra              TEXT NOT NULL DEFAULT '{}',
            searchable_text    TEXT NOT NULL DEFAULT '',
            added_at           TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
            last_played_at     TEXT,
            play_count         INTEGER NOT NULL DEFAULT 0,
            hidden             INTEGER NOT NULL DEFAULT 0,
            integrity_state    TEXT NOT NULL DEFAULT 'unknown'
                               CHECK (integrity_state IN ('unknown','ok','missing','corrupt'))
        );

        CREATE INDEX IF NOT EXISTS idx_media_sort_title ON media(sort_title);
        CREATE INDEX IF NOT EXISTS idx_media_added ON media(added_at DESC);
        CREATE INDEX IF NOT EXISTS idx_media_year ON media(year);
        CREATE INDEX IF NOT EXISTS idx_media_type_hidden ON media(content_type, hidden);
        CREATE INDEX IF NOT EXISTS idx_media_rating ON media(rating_value DESC);
        CREATE INDEX IF NOT EXISTS idx_media_parent ON media(parent_media_id);
        CREATE INDEX IF NOT EXISTS idx_media_last_played ON media(last_played_at DESC);

        CREATE TABLE IF NOT EXISTS genres (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            name  TEXT NOT NULL UNIQUE COLLATE NOCASE,
            slug  TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_genres_slug ON genres(slug);

        CREATE TABLE IF NOT EXISTS media_genres (
            media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            genre_id INTEGER NOT NULL REFERENCES genres(id) ON DELETE CASCADE,
            PRIMARY KEY (media_id, genre_id)
        );
        CREATE INDEX IF NOT EXISTS idx_media_genres_genre ON media_genres(genre_id, media_id);

        CREATE TABLE IF NOT EXISTS tags (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            name  TEXT NOT NULL UNIQUE COLLATE NOCASE,
            slug  TEXT NOT NULL UNIQUE
        );
        CREATE INDEX IF NOT EXISTS idx_tags_slug ON tags(slug);

        CREATE TABLE IF NOT EXISTS media_tags (
            media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            tag_id   INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (media_id, tag_id)
        );
        CREATE INDEX IF NOT EXISTS idx_media_tags_tag ON media_tags(tag_id, media_id);

        CREATE TABLE IF NOT EXISTS people (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL COLLATE NOCASE,
            slug        TEXT NOT NULL UNIQUE,
            role        TEXT NOT NULL DEFAULT 'cast' CHECK (role IN ('cast','director','writer')),
            tmdb_id     TEXT,
            imdb_id     TEXT,
            photo_key   TEXT,
            birth_date  TEXT,
            place       TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_people_name ON people(name);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_people_name_role ON people(name, role);

        CREATE TABLE IF NOT EXISTS media_credits (
            media_id  INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            person_id INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
            character TEXT,
            job       TEXT,
            ord       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (media_id, person_id)
        );
        CREATE INDEX IF NOT EXISTS idx_credits_person ON media_credits(person_id);

        CREATE TABLE IF NOT EXISTS collections (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            slug        TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            poster_key  TEXT
        );

        CREATE TABLE IF NOT EXISTS collection_media (
            collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            media_id      INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            position      INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (collection_id, media_id)
        );

        -- ------------------------------------------------------------------
        -- Full-text search
        -- ------------------------------------------------------------------
        CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5(
            title,
            original_title,
            overview,
            tagline,
            genres,
            tags,
            cast,
            directors,
            content='',
            tokenize='unicode61 remove_diacritics 2'
        );

        -- ------------------------------------------------------------------
        -- Per-profile state
        -- ------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS favorites (
            profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
            media_id   INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            created_at TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (profile_id, media_id)
        );
        CREATE INDEX IF NOT EXISTS idx_favorites_profile ON favorites(profile_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS watch_history (
            profile_id      INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
            media_id        INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            position_seconds REAL   NOT NULL DEFAULT 0,
            duration_seconds REAL   NOT NULL DEFAULT 0,
            percent         REAL    NOT NULL DEFAULT 0,
            completed       INTEGER NOT NULL DEFAULT 0,
            first_watched_at TEXT   NOT NULL DEFAULT (datetime('now')),
            last_watched_at  TEXT   NOT NULL DEFAULT (datetime('now')),
            play_count       INTEGER NOT NULL DEFAULT 1,
            finished_at      TEXT,
            PRIMARY KEY (profile_id, media_id)
        );
        CREATE INDEX IF NOT EXISTS idx_history_recent
            ON watch_history(profile_id, last_watched_at DESC);
        CREATE INDEX IF NOT EXISTS idx_history_continue
            ON watch_history(profile_id, completed, last_watched_at DESC);

        CREATE TABLE IF NOT EXISTS ratings (
            profile_id   INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
            media_id     INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            -- 0.5 .. 5.0 in half stars, like every commercial player.
            score         REAL    NOT NULL CHECK (score >= 0.5 AND score <= 5.0),
            created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (profile_id, media_id)
        );
        CREATE INDEX IF NOT EXISTS idx_ratings_profile ON ratings(profile_id, score DESC);

        -- ------------------------------------------------------------------
        -- Security / audit
        -- ------------------------------------------------------------------
        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            at          TEXT    NOT NULL DEFAULT (datetime('now')),
            user_id     INTEGER,
            username    TEXT,
            profile_id  INTEGER,
            action      TEXT    NOT NULL,
            outcome     TEXT    NOT NULL DEFAULT 'ok' CHECK (outcome IN ('ok','denied','error')),
            ip_address  TEXT,
            device_name TEXT,
            detail      TEXT,
            -- Deliberately coarse: never stores tokens, passwords or file bodies.
            request_id  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id, at DESC);
        CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, at DESC);

        CREATE TABLE IF NOT EXISTS scan_jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            root        TEXT    NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued','running','done','failed','cancelled')),
            started_at  TEXT,
            finished_at TEXT,
            added       INTEGER NOT NULL DEFAULT 0,
            updated     INTEGER NOT NULL DEFAULT 0,
            skipped     INTEGER NOT NULL DEFAULT 0,
            removed     INTEGER NOT NULL DEFAULT 0,
            error       TEXT
        );

        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """,
    ),
    (
        2,
        "external_ids_and_availability",
        """
        -- Extracted external identifiers into a real table so a title can be
        -- matched against several providers without JSON parsing.
        CREATE TABLE IF NOT EXISTS media_external_ids (
            media_id  INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            provider  TEXT    NOT NULL,
            value     TEXT    NOT NULL,
            PRIMARY KEY (media_id, provider)
        );
        CREATE INDEX IF NOT EXISTS idx_external_lookup ON media_external_ids(provider, value);

        CREATE TABLE IF NOT EXISTS media_streams (
            media_id    INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            stream_id   INTEGER NOT NULL,
            kind        TEXT    NOT NULL CHECK (kind IN ('video','audio','subtitle')),
            index_no    INTEGER NOT NULL,
            language    TEXT,
            title       TEXT,
            codec       TEXT,
            channels    INTEGER,
            channel_layout TEXT,
            is_default  INTEGER NOT NULL DEFAULT 0,
            is_forced   INTEGER NOT NULL DEFAULT 0,
            external_id TEXT,
            PRIMARY KEY (media_id, stream_id)
        );
        CREATE INDEX IF NOT EXISTS idx_streams_media ON media_streams(media_id, kind, index_no);

        ALTER TABLE users ADD COLUMN playback_preference TEXT NOT NULL DEFAULT 'server';
        ALTER TABLE media ADD COLUMN availability TEXT NOT NULL DEFAULT 'unknown';
        """,
    ),
    (
        3,
        "recommendations_and_playback_sessions",
        """
        CREATE TABLE IF NOT EXISTS recommendations (
            profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
            media_id   INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            score      REAL    NOT NULL,
            reason     TEXT,
            computed_at TEXT   NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (profile_id, media_id)
        );
        CREATE INDEX IF NOT EXISTS idx_recommendations_profile
            ON recommendations(profile_id, score DESC);

        -- One row per in-flight playback request.  Lets a client resume a
        -- stream after a network blip without re-authorising from scratch, and
        -- gives the server a place to record what was actually watched.
        CREATE TABLE IF NOT EXISTS playback_sessions (
            id            TEXT PRIMARY KEY,
            profile_id    INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
            media_id      INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
            token_hash    TEXT    NOT NULL,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            started_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            last_seen_at  TEXT    NOT NULL DEFAULT (datetime('now')),
            expires_at    TEXT    NOT NULL,
            bytes_served  INTEGER NOT NULL DEFAULT 0,
            requests      INTEGER NOT NULL DEFAULT 0,
            start_offset  REAL    NOT NULL DEFAULT 0,
            ended         INTEGER NOT NULL DEFAULT 0,
            client_ip     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_playback_profile
            ON playback_sessions(profile_id, ended);
        CREATE INDEX IF NOT EXISTS idx_playback_expiry ON playback_sessions(expires_at);
        """,
    ),
]

LATEST_VERSION = max(version for version, _, _ in MIGRATIONS)