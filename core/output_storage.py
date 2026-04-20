from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse


class OutputStorage(Protocol):
    def ensure_layout(self) -> None: ...
    def create_temp_location(self, upload_id: str) -> str: ...
    def finalize_temp_to_blob(self, temp_location: str, blob_id: str) -> str: ...
    def write_bytes(self, location: str, body_bytes: bytes) -> None: ...
    def read_bytes(self, location: str) -> bytes: ...
    def exists(self, location: str) -> bool: ...
    def delete(self, location: str) -> None: ...


@dataclass
class LocalOutputStorage:
    root_dir: str

    def _uploads_dir(self) -> str:
        return os.path.join(self.root_dir, "uploads")

    def _blobs_dir(self) -> str:
        return os.path.join(self.root_dir, "blobs")

    def ensure_layout(self) -> None:
        os.makedirs(self._uploads_dir(), exist_ok=True)
        os.makedirs(self._blobs_dir(), exist_ok=True)

    def create_temp_location(self, upload_id: str) -> str:
        return os.path.join(self._uploads_dir(), f"{upload_id}.bin")

    def finalize_temp_to_blob(self, temp_location: str, blob_id: str) -> str:
        final_path = os.path.join(self._blobs_dir(), f"{blob_id}.bin")
        os.makedirs(os.path.dirname(final_path), exist_ok=True)
        os.replace(temp_location, final_path)
        return final_path

    def write_bytes(self, location: str, body_bytes: bytes) -> None:
        os.makedirs(os.path.dirname(location), exist_ok=True)
        with open(location, "wb") as fh:
            fh.write(body_bytes)

    def read_bytes(self, location: str) -> bytes:
        with open(location, "rb") as fh:
            return fh.read()

    def exists(self, location: str) -> bool:
        return os.path.exists(location)

    def delete(self, location: str) -> None:
        try:
            os.remove(location)
        except FileNotFoundError:
            return


@dataclass
class S3CompatibleOutputStorage:
    bucket: str
    prefix: str
    client: object
    local_fallback: LocalOutputStorage

    def ensure_layout(self) -> None:
        # S3-compatible backends are flat key/value stores.
        # Keep local layout for backward-compatible access to legacy local blobs.
        self.local_fallback.ensure_layout()

    def _key(self, *parts: str) -> str:
        normalized = [str(part).strip("/") for part in parts if str(part).strip("/")]
        joined = "/".join(normalized)
        if self.prefix:
            return f"{self.prefix.rstrip('/')}/{joined}"
        return joined

    def _to_uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key.lstrip('/')}"

    def _parse_uri(self, location: str) -> tuple[str, str]:
        parsed = urlparse(location)
        if parsed.scheme != "s3":
            raise ValueError("Expected s3:// URI.")
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        if not bucket or not key:
            raise ValueError("Invalid s3:// URI.")
        return bucket, key

    def _is_s3_uri(self, location: str) -> bool:
        return str(location or "").strip().lower().startswith("s3://")

    def create_temp_location(self, upload_id: str) -> str:
        key = self._key("uploads", f"{upload_id}.bin")
        return self._to_uri(key)

    def finalize_temp_to_blob(self, temp_location: str, blob_id: str) -> str:
        if not self._is_s3_uri(temp_location):
            return self.local_fallback.finalize_temp_to_blob(temp_location, blob_id)
        src_bucket, src_key = self._parse_uri(temp_location)
        dst_key = self._key("blobs", f"{blob_id}.bin")
        self.client.copy_object(
            Bucket=self.bucket,
            Key=dst_key,
            CopySource={"Bucket": src_bucket, "Key": src_key},
        )
        self.client.delete_object(Bucket=src_bucket, Key=src_key)
        return self._to_uri(dst_key)

    def write_bytes(self, location: str, body_bytes: bytes) -> None:
        if not self._is_s3_uri(location):
            self.local_fallback.write_bytes(location, body_bytes)
            return
        bucket, key = self._parse_uri(location)
        self.client.put_object(Bucket=bucket, Key=key, Body=body_bytes)

    def read_bytes(self, location: str) -> bytes:
        if not self._is_s3_uri(location):
            return self.local_fallback.read_bytes(location)
        bucket, key = self._parse_uri(location)
        obj = self.client.get_object(Bucket=bucket, Key=key)
        return obj["Body"].read()

    def exists(self, location: str) -> bool:
        if not self._is_s3_uri(location):
            return self.local_fallback.exists(location)
        bucket, key = self._parse_uri(location)
        try:
            self.client.head_object(Bucket=bucket, Key=key)
            return True
        except Exception:
            return False

    def delete(self, location: str) -> None:
        if not self._is_s3_uri(location):
            self.local_fallback.delete(location)
            return
        bucket, key = self._parse_uri(location)
        self.client.delete_object(Bucket=bucket, Key=key)


def create_output_storage_from_env(*, local_root: str) -> OutputStorage:
    backend = str(os.environ.get("OUTPUT_BLOB_STORAGE_BACKEND", "local")).strip().lower() or "local"
    local_backend = LocalOutputStorage(root_dir=os.path.abspath(local_root))
    if backend in {"local", "filesystem", "fs"}:
        return local_backend
    if backend not in {"s3", "r2", "minio"}:
        raise RuntimeError(
            "OUTPUT_BLOB_STORAGE_BACKEND must be one of: local, s3, r2, minio."
        )
    bucket = str(os.environ.get("OUTPUT_BLOB_S3_BUCKET", "")).strip()
    if not bucket:
        raise RuntimeError("OUTPUT_BLOB_S3_BUCKET is required for s3/r2/minio backends.")
    prefix = str(os.environ.get("OUTPUT_BLOB_S3_PREFIX", "aztea-outputs")).strip().strip("/")
    endpoint_url = str(os.environ.get("OUTPUT_BLOB_S3_ENDPOINT", "")).strip() or None
    region_name = str(os.environ.get("OUTPUT_BLOB_S3_REGION", "")).strip() or None
    access_key_id = str(os.environ.get("OUTPUT_BLOB_S3_ACCESS_KEY_ID", "")).strip() or None
    secret_access_key = str(os.environ.get("OUTPUT_BLOB_S3_SECRET_ACCESS_KEY", "")).strip() or None
    session_token = str(os.environ.get("OUTPUT_BLOB_S3_SESSION_TOKEN", "")).strip() or None
    addressing_style = str(os.environ.get("OUTPUT_BLOB_S3_ADDRESSING_STYLE", "")).strip().lower() or None

    try:
        import boto3  # type: ignore
        from botocore.config import Config  # type: ignore
    except Exception as exc:
        raise RuntimeError("boto3 is required for s3/r2/minio output storage backends.") from exc

    config = None
    if addressing_style in {"path", "virtual"}:
        config = Config(s3={"addressing_style": addressing_style})
    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=region_name,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        aws_session_token=session_token,
        config=config,
    )
    return S3CompatibleOutputStorage(
        bucket=bucket,
        prefix=prefix,
        client=client,
        local_fallback=local_backend,
    )
