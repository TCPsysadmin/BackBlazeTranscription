"""Generate missing B2 thumbnails for previously archived videos.

The script is a dry run unless --apply is supplied. It scopes database rows to
clients_registry entries assigned to the selected B2 bucket and refuses
ambiguous filename matches.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Any

import requests
from b2sdk.v2 import B2Api, InMemoryAccountInfo

VIDEO_EXTENSIONS = {
    ".avi",
    ".flv",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".webm",
    ".wmv",
}


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def normalize_stem(value: str) -> str:
    stem = Path(value).stem.casefold()
    return "".join(character for character in stem if character.isalnum())


def thumbnail_path_for(
    video_path: str, video_prefix: str, thumbnail_prefix: str
) -> str:
    path = PurePosixPath(video_path)
    video_root = PurePosixPath(video_prefix.strip("/"))
    try:
        relative = path.relative_to(video_root)
    except ValueError:
        # Legacy buckets such as TCP-MASTER organize videos directly beneath
        # category folders rather than videos/<job-id>/. Include the filename
        # stem so several videos in one category cannot overwrite each other.
        return str(
            PurePosixPath(thumbnail_prefix.strip("/"))
            / path.parent
            / path.stem
            / "thumbnail.webp"
        )

    # Current uploads use videos/<job-id>/<filename>. Preserve that job-id so
    # the backfilled object follows the same layout as newly ingested videos.
    parent = relative.parent
    if str(parent) == ".":
        parent = PurePosixPath(path.stem)
    return str(PurePosixPath(thumbnail_prefix.strip("/")) / parent / "thumbnail.webp")


class SupabaseRest:
    def __init__(self, base_url: str, service_key: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "apikey": service_key,
                "Authorization": f"Bearer {service_key}",
                "Content-Type": "application/json",
            }
        )

    def get(self, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        response = self.session.get(
            f"{self.base_url}/rest/v1/{table}", params=params, timeout=60
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise TypeError(f"Unexpected Supabase response for {table}")
        return payload

    def patch(self, table: str, params: dict[str, str], values: dict[str, Any]) -> None:
        response = self.session.patch(
            f"{self.base_url}/rest/v1/{table}",
            params=params,
            json=values,
            headers={"Prefer": "return=minimal"},
            timeout=60,
        )
        response.raise_for_status()


def generate_thumbnail(video_path: Path, output_path: Path, at_seconds: float) -> None:
    filter_value = (
        "scale=640:360:force_original_aspect_ratio=decrease,"
        "pad=640:360:(ow-iw)/2:(oh-ih)/2"
    )
    last_error = ""
    for timestamp in dict.fromkeys((max(0.0, at_seconds), 0.0)):
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                str(timestamp),
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-vf",
                filter_value,
                "-c:v",
                "libwebp",
                "-quality",
                "82",
                str(output_path),
            ],
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode == 0 and output_path.exists():
            return
        last_error = result.stderr[-500:]
    raise RuntimeError(f"FFmpeg could not create a thumbnail: {last_error}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default="TCP-MASTER")
    parser.add_argument(
        "--video-prefix", default=os.getenv("B2_VIDEO_PREFIX", "videos")
    )
    parser.add_argument(
        "--thumbnail-prefix",
        default=os.getenv("B2_THUMBNAIL_PREFIX", "thumbnails"),
    )
    parser.add_argument(
        "--at-seconds",
        type=float,
        default=float(os.getenv("THUMBNAIL_AT_SECONDS", "3")),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Generate/upload thumbnails and update Supabase. Default is dry-run.",
    )
    parser.add_argument(
        "--scan-all",
        action="store_true",
        help="Scan video files in every bucket folder, including legacy layouts.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    key_id = required_env("B2_KEY_ID")
    application_key = required_env("B2_APPLICATION_KEY")
    supabase = SupabaseRest(
        required_env("SUPABASE_URL"), required_env("SUPABASE_SERVICE_KEY")
    )

    clients = supabase.get(
        "clients_registry",
        {
            "select": "client_id,display_name,b2_bucket",
            "b2_bucket": f"eq.{args.bucket}",
            "status": "eq.active",
        },
    )
    if not clients:
        raise SystemExit(
            f"No active clients_registry workspace is assigned to bucket {args.bucket!r}"
        )

    account_info = InMemoryAccountInfo()
    b2_api = B2Api(account_info)
    b2_api.authorize_account("production", key_id, application_key)
    bucket = b2_api.get_bucket_by_name(args.bucket)

    video_paths: list[str] = []
    existing_paths: set[str] = set()
    for file_version, _folder_name in bucket.ls(recursive=True):
        object_path = file_version.file_name
        existing_paths.add(object_path)
        is_video = PurePosixPath(object_path).suffix.casefold() in VIDEO_EXTENSIONS
        is_in_video_prefix = object_path.startswith(f"{args.video_prefix.strip('/')}/")
        if is_video and (args.scan_all or is_in_video_prefix):
            video_paths.append(object_path)

    by_filename: dict[str, list[str]] = defaultdict(list)
    by_stem: dict[str, list[str]] = defaultdict(list)
    for object_path in video_paths:
        name = PurePosixPath(object_path).name
        by_filename[name.casefold()].append(object_path)
        by_stem[normalize_stem(name)].append(object_path)

    rows: list[dict[str, Any]] = []
    for client in clients:
        rows.extend(
            supabase.get(
                "video_summaries",
                {
                    "select": (
                        "summary_id,client_id,source_video_id,source_file,b2_path,"
                        "thumbnail_url,thumbnail_b2_path"
                    ),
                    "client_id": f"eq.{client['client_id']}",
                    "thumbnail_b2_path": "is.null",
                },
            )
        )

    matched = generated = linked = skipped = failed = 0
    print(f"Bucket: {args.bucket}")
    print(f"Workspaces: {', '.join(str(row['display_name']) for row in clients)}")
    print("Video scope:", "all bucket folders" if args.scan_all else args.video_prefix)
    print(f"B2 videos found: {len(video_paths)}")
    print(f"Database rows missing thumbnail paths: {len(rows)}")
    print("Mode:", "APPLY" if args.apply else "DRY RUN")

    for row in rows:
        source_file = str(row.get("source_file") or "").strip()
        current_video_path = str(row.get("b2_path") or "").strip()
        candidates: list[str] = []
        if current_video_path and current_video_path in existing_paths:
            candidates = [current_video_path]
        elif source_file:
            candidates = by_filename.get(PurePosixPath(source_file).name.casefold(), [])
            if not candidates:
                candidates = by_stem.get(normalize_stem(source_file), [])
        if not candidates and row.get("source_video_id"):
            candidates = by_stem.get(normalize_stem(str(row["source_video_id"])), [])

        candidates = list(dict.fromkeys(candidates))
        if len(candidates) != 1:
            reason = (
                "no B2 video match" if not candidates else "ambiguous B2 video match"
            )
            print(f"SKIP {source_file or row['source_video_id']}: {reason}")
            skipped += 1
            continue

        matched += 1
        video_path = candidates[0]
        thumbnail_path = thumbnail_path_for(
            video_path, args.video_prefix, args.thumbnail_prefix
        )
        already_exists = thumbnail_path in existing_paths
        action = "link existing" if already_exists else "generate"
        print(f"{action.upper()}: {video_path} -> {thumbnail_path}")

        if not args.apply:
            continue

        try:
            if not already_exists:
                with tempfile.TemporaryDirectory(prefix="vp-thumbnail-") as temp_dir:
                    video_local = Path(temp_dir) / PurePosixPath(video_path).name
                    thumbnail_local = Path(temp_dir) / "thumbnail.webp"
                    bucket.download_file_by_name(video_path).save_to(str(video_local))
                    generate_thumbnail(video_local, thumbnail_local, args.at_seconds)
                    bucket.upload_local_file(
                        local_file=str(thumbnail_local), file_name=thumbnail_path
                    )
                existing_paths.add(thumbnail_path)
                generated += 1
            else:
                linked += 1

            updates: dict[str, Any] = {"thumbnail_b2_path": thumbnail_path}
            if not current_video_path:
                updates["b2_path"] = video_path
            supabase.patch(
                "video_summaries", {"summary_id": f"eq.{row['summary_id']}"}, updates
            )

            # Keep the manifest in sync when one exists. A missing manifest is
            # harmless; the video_summaries row is what powers the library.
            supabase.patch(
                "ingestion_manifests",
                {
                    "client_id": f"eq.{row['client_id']}",
                    "source_video_id": f"eq.{row['source_video_id']}",
                },
                {"b2_path": video_path, "thumbnail_b2_path": thumbnail_path},
            )
        except Exception as error:  # noqa: BLE001 - continue processing the backlog
            failed += 1
            print(f"FAILED {video_path}: {error}")

    print(
        f"Summary: matched={matched}, generated={generated}, linked={linked}, "
        f"skipped={skipped}, failed={failed}"
    )
    if not args.apply:
        print(
            "No changes were made. Re-run with --apply after reviewing these matches."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
