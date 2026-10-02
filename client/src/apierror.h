#pragma once

#include <QString>

/// Why a request failed, in terms the UI can act on.
///
/// Every failure the API can produce is mapped here exactly once, so a screen
/// never has to pattern-match on an error string.  `Unknown` is a deliberate
/// member rather than a catch-all `Other`: it means the server said something
/// this build does not understand, which is worth showing differently from a
/// known condition.
enum class ApiErrorKind {
    None,
    NetworkUnreachable,   ///< DNS failure, connection refused, no route
    Timeout,              ///< no answer within the deadline
    Unauthorized,         ///< 401 -- no token, expired, or revoked
    Forbidden,            ///< 403 -- wrong privilege, or a playback token on
                           ///<      a non-stream route
    NotFound,             ///< 404 -- also what a foreign profile returns
    Conflict,             ///< 409
    RateLimited,          ///< 429
    ValidationFailed,     ///< 422
    Gone,                 ///< 410 -- the file is recorded missing
    ServerError,          ///< 5xx
    Cancelled,
    Unknown,
};

/// A failed request, carrying everything a screen needs to react sensibly.
///
/// `retryAfterSeconds` is populated from `Retry-After` on a 429 so a client can
/// show a countdown rather than retrying into the same wall.
struct ApiError {
    ApiErrorKind kind = ApiErrorKind::Unknown;
    QString message;
    QString code;
    QString requestId;
    int retryAfterSeconds = 0;
    int statusCode = 0;

    bool isNone() const { return kind == ApiErrorKind::None; }

    /// Whether trying the same request again could plausibly succeed.
    /// A 404 or a 403 will not fix itself; a timeout might.
    bool isRetryable() const;

    /// Whether the user should be sent back to the server-configuration
    /// screen rather than shown a message.  Only a network-level failure or a
    /// 401 on the *connection check* means the server itself is the problem.
    bool isServerProblem() const {
        return kind == ApiErrorKind::NetworkUnreachable
            || kind == ApiErrorKind::Timeout
            || kind == ApiErrorKind::Unauthorized;
    }
};

/// Map an HTTP status onto the taxonomy.
ApiErrorKind kindForStatus(int status);