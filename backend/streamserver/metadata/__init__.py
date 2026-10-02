"""Metadata providers.

The offline :class:`LocalSidecarProvider` is the default; everything else needs
an operator-supplied key and is never contacted implicitly.
"""

from .providers import (
    Credit,
    ImdbProvider,
    LocalSidecarProvider,
    MediaMetadata,
    MetadataProvider,
    NoProvider,
    OmdbProvider,
    ProviderChain,
    TmdbProvider,
    build_provider,
    merge_metadata,
)

__all__ = [
    "Credit", "ImdbProvider", "LocalSidecarProvider", "MediaMetadata",
    "MetadataProvider", "NoProvider", "OmdbProvider", "ProviderChain",
    "TmdbProvider", "build_provider", "merge_metadata",
]
