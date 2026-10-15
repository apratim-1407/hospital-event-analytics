"""S3-compatible object storage (MinIO locally). Bulk CSV uploads are staged here
before being loaded, so large blobs never travel through the database or the API."""
from urllib.parse import urlparse

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.core.config import settings


def get_s3():
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4", retries={"max_attempts": 3}, connect_timeout=5, read_timeout=60),
    )


def ensure_bucket() -> None:
    s3 = get_s3()
    try:
        s3.head_bucket(Bucket=settings.s3_bucket)
    except ClientError:
        s3.create_bucket(Bucket=settings.s3_bucket)


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """s3://bucket/key -> (bucket, key). Only the configured bucket is accepted, so the
    API cannot be pointed at arbitrary storage."""
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.lstrip("/"):
        raise ValueError("source_uri must look like s3://<bucket>/<key>")
    if parsed.netloc != settings.s3_bucket:
        raise ValueError(f"source_uri must reference the configured bucket '{settings.s3_bucket}'")
    return parsed.netloc, parsed.path.lstrip("/")


def object_exists(uri: str) -> bool:
    bucket, key = parse_s3_uri(uri)
    try:
        get_s3().head_object(Bucket=bucket, Key=key)
        return True
    except ClientError:
        return False


def open_object(uri: str):
    """Return a streaming file-like body for the object."""
    bucket, key = parse_s3_uri(uri)
    return get_s3().get_object(Bucket=bucket, Key=key)["Body"]


def upload_file(path: str, key: str) -> str:
    ensure_bucket()
    get_s3().upload_file(path, settings.s3_bucket, key)
    return f"s3://{settings.s3_bucket}/{key}"
