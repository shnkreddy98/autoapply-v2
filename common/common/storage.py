"""Object storage abstraction. S3Storage (MinIO/S3) for prod, LocalFSStorage for
dev/unit tests. Same Storage protocol so callers are backend-agnostic."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class Storage(Protocol):
    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None: ...
    def get_bytes(self, key: str) -> bytes: ...
    def put_file(self, key: str, path: str) -> None: ...
    def get_file(self, key: str, dest: str) -> None: ...
    def exists(self, key: str) -> bool: ...


class LocalFSStorage:
    """Filesystem-backed store. `key` becomes a path under `root`."""

    def __init__(self, root: str):
        self.root = Path(root)

    def _p(self, key: str) -> Path:
        return self.root / key

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get_bytes(self, key: str) -> bytes:
        return self._p(key).read_bytes()

    def put_file(self, key: str, path: str) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, p)

    def get_file(self, key: str, dest: str) -> None:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._p(key), dest)

    def exists(self, key: str) -> bool:
        return self._p(key).is_file()


class S3Storage:
    """boto3 against S3 or MinIO (via endpoint_url)."""

    def __init__(
        self,
        bucket: str,
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str = "us-east-1",
    ):
        import boto3

        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, **extra)

    def get_bytes(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def put_file(self, key: str, path: str) -> None:
        self.client.upload_file(path, self.bucket, key)

    def get_file(self, key: str, dest: str) -> None:
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, dest)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False


def from_env() -> Storage:
    """Build a Storage from env. STORAGE_BACKEND=local|s3 (default s3)."""
    if os.getenv("STORAGE_BACKEND", "s3").lower() == "local":
        return LocalFSStorage(os.getenv("STORAGE_LOCAL_ROOT", "./.storage"))
    return S3Storage(
        bucket=os.getenv("S3_BUCKET", "autoapply"),
        endpoint_url=os.getenv("S3_ENDPOINT", "http://localhost:9100"),
        access_key=os.getenv("MINIO_ACCESS_KEY", "minioadmin"),
        secret_key=os.getenv("MINIO_SECRET_KEY", "minioadmin"),
    )
