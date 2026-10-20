"""Which exact Hugging Face snapshot a model name means (provenance only)."""

from pathlib import Path


def split_revision(spec: str) -> tuple[str, str | None]:
    """`"org/repo@rev"` -> `("org/repo", "rev")`; no suffix -> revision `None`."""
    repo, separator, revision = spec.partition("@")
    return (repo, revision or None) if separator else (spec, None)


def resolved_revision(repo_id: str, revision: str | None = None) -> str | None:
    """The commit a repo id (and optional branch/tag) resolves to in the cache.

    `from_pretrained` loads the snapshot `refs/<revision or main>` points at;
    this reads the same reference without a network call. `None` for a local
    path, or when the cache cannot say.
    """
    try:
        from huggingface_hub import snapshot_download

        return Path(
            snapshot_download(repo_id, revision=revision, local_files_only=True)
        ).name
    except Exception:  # noqa: BLE001 -- provenance is best effort, never fatal
        return None
