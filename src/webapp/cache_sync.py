"""Sync the OHLC cache from S3 into the local data_loader directory using boto3.

boto3 reads the ECS task-role credentials via the container credential provider —
no AWS CLI needed in the image. Mirrors scripts/pull_cache.sh's bucket/prefix."""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

# Bucket/prefix are env-configurable so the deployed task can point at a
# same-region copy (e.g. an ap-northeast-1 mirror) without rebuilding the image.
BUCKET = os.environ.get("FEIYANG_S3_BUCKET", "hahacapital-jp")
PREFIX = os.environ.get("FEIYANG_S3_PREFIX", "jojo_quant/ohlc/")


def local_path_for(prefix: str, key: str, dest_dir: str) -> str:
    rel = key[len(prefix):] if key.startswith(prefix) else key
    return os.path.join(dest_dir, rel)


def sync_cache(*, bucket: str = BUCKET, prefix: str = PREFIX,
               dest_dir: str | None = None, client=None, max_workers: int = 16) -> int:
    """Download every object under s3://bucket/prefix into dest_dir. Skips the
    prefix 'folder' marker and objects already present with the same size.
    Downloads run concurrently (boto3 clients are thread-safe for requests) so a
    fresh cache of ~10k small files syncs in seconds. Returns the number of files
    written."""
    if dest_dir is None:
        import data_loader as dl
        dest_dir = str(dl.DATA_DIR)
    if client is None:
        import boto3
        client = boto3.client("s3")

    todo: list[tuple[str, str]] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key, size = obj["Key"], obj.get("Size", 0)
            if key.endswith("/") or (key == prefix):
                continue
            dest = local_path_for(prefix, key, dest_dir)
            if os.path.exists(dest) and os.path.getsize(dest) == size:
                continue
            todo.append((key, dest))

    def _download(item: tuple[str, str]) -> None:
        key, dest = item
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        client.download_file(Bucket=bucket, Key=key, Filename=dest)

    if todo:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(_download, todo))
    return len(todo)
