"""Cloudflare R2 artifact sync (S3 API via boto3).

Env (set on both Railway services):
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET

Downloads are ETag-conditional: the object's ETag is remembered in a
``<dest>.etag`` sidecar, so the nightly-refresh check is a single HEAD
when nothing changed. Uploads stream multipart (the archive is 4.7 GB)
and S3 PUT/complete-multipart is atomic per object — readers see the old
or the new artifact, never a partial one.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger("deploy.r2sync")


def client():
    import boto3
    from botocore.config import Config

    account = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
    )


def bucket() -> str:
    return os.environ.get("R2_BUCKET", "ca-leginfo")


def remote_etag(s3, key: str) -> str | None:
    """ETag of the object, or None if it doesn't exist."""
    try:
        return s3.head_object(Bucket=bucket(), Key=key)["ETag"]
    except s3.exceptions.ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def download(s3, key: str, dest: Path, *, only_if_changed: bool = True
             ) -> bool:
    """Fetch ``key`` into ``dest`` atomically (tmp + os.replace).

    Returns True if a new file was installed. With only_if_changed, a
    matching ``<dest>.etag`` sidecar skips the transfer entirely.
    """
    dest = Path(dest)
    sidecar = dest.with_name(dest.name + ".etag")
    etag = remote_etag(s3, key)
    if etag is None:
        raise FileNotFoundError(f"r2://{bucket()}/{key} does not exist")
    if (only_if_changed and dest.exists() and sidecar.exists()
            and sidecar.read_text().strip() == etag):
        log.info("%s unchanged (etag %s) — skipping download", key, etag)
        return False
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.unlink(missing_ok=True)
    log.info("downloading r2://%s/%s -> %s", bucket(), key, dest)
    try:
        s3.download_file(bucket(), key, str(tmp))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, dest)
    sidecar.write_text(etag)
    log.info("installed %s (%d MB)", dest, dest.stat().st_size // 1_000_000)
    return True


def upload(s3, src: Path, key: str) -> None:
    """Multipart upload; atomic at the object level."""
    src = Path(src)
    log.info("uploading %s (%d MB) -> r2://%s/%s",
             src, src.stat().st_size // 1_000_000, bucket(), key)
    s3.upload_file(str(src), bucket(), key)
    log.info("upload complete: %s", key)
