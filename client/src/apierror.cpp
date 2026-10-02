#include "apierror.h"

ApiErrorKind kindForStatus(int status)
{
    switch (status) {
    case 200: case 201: case 204:
    case 206: case 304:
        return ApiErrorKind::None;
    case 401: return ApiErrorKind::Unauthorized;
    case 403: return ApiErrorKind::Forbidden;
    case 404: return ApiErrorKind::NotFound;
    case 409: return ApiErrorKind::Conflict;
    case 410: return ApiErrorKind::Gone;
    case 422: return ApiErrorKind::ValidationFailed;
    case 429: return ApiErrorKind::RateLimited;
    default:
        break;
    }
    if (status >= 500)
        return ApiErrorKind::ServerError;
    if (status >= 400)
        return ApiErrorKind::ValidationFailed;
    return ApiErrorKind::Unknown;
}

bool ApiError::isRetryable() const
{
    switch (kind) {
    case ApiErrorKind::NetworkUnreachable:
    case ApiErrorKind::Timeout:
    case ApiErrorKind::RateLimited:
    case ApiErrorKind::ServerError:
        return true;
    default:
        // A 404 or a 403 will be exactly as true in thirty seconds as it is
        // now.  Retrying them only burns the rate-limit budget that the
        // retryable cases need.
        return false;
    }
}
