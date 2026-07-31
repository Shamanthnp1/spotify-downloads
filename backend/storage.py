import os
from datetime import datetime, timezone, timedelta

import boto3
from botocore.exceptions import ClientError


def _get_client():
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _bucket() -> str:
    return os.environ["R2_BUCKET_NAME"]


def upload_file(local_path: str, object_key: str) -> str:
    """
    Uploads a file from local_path to R2 under object_key.
    Returns the object_key on success.
    Raises RuntimeError on upload failure.
    """
    client = _get_client()
    bucket = _bucket()

    try:
        client.upload_file(local_path, bucket, object_key)
    except ClientError as exc:
        raise RuntimeError(
            f"Failed to upload '{local_path}' to R2 key '{object_key}': {exc}"
        ) from exc

    return object_key


def generate_presigned_url(object_key: str, expiry_seconds: int = 3600) -> str:
    """
    Generates a presigned GET URL for object_key valid for expiry_seconds.
    Default validity: 1 hour.
    Raises RuntimeError if URL generation fails.
    """
    client = _get_client()
    bucket = _bucket()

    try:
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": object_key},
            ExpiresIn=expiry_seconds,
        )
    except ClientError as exc:
        raise RuntimeError(
            f"Failed to generate presigned URL for '{object_key}': {exc}"
        ) from exc

    return url


def delete_object(object_key: str) -> None:
    """
    Deletes a single object from R2 by key.
    Silently no-ops if the key does not exist (idempotent).
    Raises RuntimeError on unexpected S3 errors.
    """
    client = _get_client()
    bucket = _bucket()

    try:
        client.delete_object(Bucket=bucket, Key=object_key)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code == "NoSuchKey":
            return
        raise RuntimeError(
            f"Failed to delete R2 object '{object_key}': {exc}"
        ) from exc


def list_objects_older_than(minutes: int) -> list[str]:
    """
    Returns a list of object keys whose LastModified timestamp is older
    than `minutes` minutes ago (UTC).

    Handles S3/R2 pagination transparently — safe for large buckets.
    """
    client = _get_client()
    bucket = _bucket()
    cutoff = datetime.now(tz=timezone.utc) - timedelta(minutes=minutes)

    old_keys: list[str] = []
    paginator = client.get_paginator("list_objects_v2")

    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            last_modified: datetime = obj["LastModified"]
            if last_modified < cutoff:
                old_keys.append(obj["Key"])

    return old_keys
