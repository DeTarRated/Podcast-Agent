"""交付层·Cloudflare R2 / S3 兼容存储适配。

对象使用内容寻址 key 与 sha256 metadata；HEAD 不匹配时拒绝覆盖。
签名 URL 只在返回值中使用，不写日志。
"""
from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from delivery.errors import DeliveryError


@dataclass(frozen=True)
class R2Settings:
    endpoint: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    region: str = "auto"
    expires_seconds: int = 900

    @classmethod
    def from_env(cls) -> "R2Settings":
        names = (
            "R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
        )
        values = {name: os.environ.get(name) for name in names}
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise DeliveryError("r2_not_configured", f"缺少 R2 配置: {','.join(missing)}")
        try:
            expires = int(os.environ.get("R2_URL_EXPIRES_SECONDS", "900"))
        except ValueError as exc:
            raise DeliveryError("r2_not_configured", "R2_URL_EXPIRES_SECONDS 不是整数") from exc
        if expires <= 0 or expires > 604800:
            raise DeliveryError("r2_not_configured", "签名有效期必须在 1 秒到 7 天之间")
        return cls(
            endpoint=values["R2_ENDPOINT"],
            bucket=values["R2_BUCKET"],
            access_key_id=values["R2_ACCESS_KEY_ID"],
            secret_access_key=values["R2_SECRET_ACCESS_KEY"],
            region=os.environ.get("R2_REGION", "auto"),
            expires_seconds=expires,
        )


class R2Store:
    def __init__(self, settings: R2Settings, client=None):
        self.settings = settings
        self.client = client or boto3.client(
            "s3",
            endpoint_url=settings.endpoint,
            aws_access_key_id=settings.access_key_id,
            aws_secret_access_key=settings.secret_access_key,
            region_name=settings.region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    def _head(self, key: str) -> Optional[dict]:
        try:
            return self.client.head_object(Bucket=self.settings.bucket, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404 or code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise DeliveryError("r2_error", f"R2 HEAD 失败: {code or status or 'unknown'}") from exc

    def _validate(self, key: str, size: int, sha256: str) -> bool:
        head = self._head(key)
        if head is None:
            return False
        metadata = {str(k).lower(): v for k, v in (head.get("Metadata") or {}).items()}
        if head.get("ContentLength") != size or metadata.get("sha256") != sha256:
            raise DeliveryError("object_conflict", "R2 同 key 存在不同内容，拒绝覆盖", {"key": key})
        return True

    def _ensure(self, key: str, body, size: int, sha256: str, content_type: str) -> bool:
        if self._validate(key, size, sha256):
            return False
        try:
            self.client.put_object(
                Bucket=self.settings.bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                Metadata={"sha256": sha256},
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", "unknown"))
            raise DeliveryError("r2_error", f"R2 上传失败: {code}", {"key": key}) from exc
        if not self._validate(key, size, sha256):
            raise DeliveryError("r2_error", "R2 上传后 HEAD 校验失败", {"key": key})
        return True

    def ensure_bytes(self, key: str, payload: bytes, content_type: str) -> bool:
        return self._ensure(
            key,
            io.BytesIO(payload),
            len(payload),
            hashlib.sha256(payload).hexdigest(),
            content_type,
        )

    def ensure_file(self, key: str, path: Path, content_type: str) -> bool:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(64 * 1024), b""):
                digest.update(chunk)
        size = path.stat().st_size
        sha256 = digest.hexdigest()
        if self._validate(key, size, sha256):
            return False
        try:
            with open(path, "rb") as stream:
                self.client.put_object(
                    Bucket=self.settings.bucket,
                    Key=key,
                    Body=stream,
                    ContentType=content_type,
                    Metadata={"sha256": sha256},
                )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", "unknown"))
            raise DeliveryError("r2_error", f"R2 上传失败: {code}", {"key": key}) from exc
        if not self._validate(key, size, sha256):
            raise DeliveryError("r2_error", "R2 上传后 HEAD 校验失败", {"key": key})
        return True

    def sign_get(self, key: str, *, download_name: Optional[str] = None) -> str:
        params = {"Bucket": self.settings.bucket, "Key": key}
        if download_name:
            params["ResponseContentDisposition"] = f'attachment; filename="{download_name}"'
        try:
            return self.client.generate_presigned_url(
                "get_object", Params=params, ExpiresIn=self.settings.expires_seconds
            )
        except ClientError as exc:
            raise DeliveryError("r2_error", "R2 签名失败") from exc
