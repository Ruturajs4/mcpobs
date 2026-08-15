"""What the archiver has written, and proof it can be read back.

An archive nobody has ever read is a backup nobody has ever restored. This lists
the objects and, for the newest one, actually decompresses and re-frames it back
into individual OTLP messages -- the operation a replay would perform.

    python scripts/archive_ls.py [--tenant acme]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archiver.archiver import unframe


def client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.getenv("S3_ENDPOINT", "http://localhost:9002"),
        aws_access_key_id=os.getenv("S3_ACCESS_KEY", "mcpobs"),
        aws_secret_access_key=os.getenv("S3_SECRET_KEY", "mcpobs-secret"),
        region_name=os.getenv("S3_REGION", "us-east-1"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default=None, help="only this tenant's prefix")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    bucket = os.getenv("ARCHIVE_BUCKET", "mcpobs-archive")
    s3 = client()
    prefix = f"{args.tenant}/" if args.tenant else ""
    response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    objects = response.get("Contents", [])
    if not objects:
        print(f"  s3://{bucket}/{prefix} is empty")
        return 0

    objects.sort(key=lambda o: o["LastModified"], reverse=True)
    total = sum(o["Size"] for o in objects)
    tenants = {o["Key"].split("/", 1)[0] for o in objects}

    print(f"  s3://{bucket}/{prefix}")
    print(f"  {len(objects)} object(s), {total / 1024:.1f} KiB, {len(tenants)} tenant(s)")
    print()
    for obj in objects[: args.limit]:
        print(f"  {obj['Size']:>9,}  {obj['LastModified']:%Y-%m-%d %H:%M}  {obj['Key']}")
    if len(objects) > args.limit:
        print(f"  ... and {len(objects) - args.limit} more")

    # Read the newest one back. This is the whole point of the tool: the format
    # is only real if something other than the writer can parse it.
    newest = objects[0]
    body = s3.get_object(Bucket=bucket, Key=newest["Key"])["Body"].read()
    messages = unframe(body)
    print()
    print(f"  read back {newest['Key']}")
    print(f"  {len(messages)} OTLP message(s), "
          f"{sum(len(m) for m in messages):,} bytes uncompressed "
          f"(stored {newest['Size']:,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
