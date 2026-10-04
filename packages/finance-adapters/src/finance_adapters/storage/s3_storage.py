from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class S3Config:
    endpoint_url: str
    bucket: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"


class S3ObjectStorage:
    """Optional S3/MinIO adapter; imports boto3 lazily so local mode stays lightweight."""

    def __init__(self, config: S3Config):
        import boto3

        self._client = boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            aws_access_key_id=config.access_key,
            aws_secret_access_key=config.secret_key,
            region_name=config.region,
        )
        self._bucket = config.bucket

    def put(self, key: str, content: bytes, content_type: str = "application/pdf") -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=content, ContentType=content_type)

    def get(self, key: str) -> bytes:
        return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
