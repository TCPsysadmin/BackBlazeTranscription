"""Find and repair videos that are missing a transcript or summary.

The default mode is read-only.  ``--apply`` retranscribes the matched B2 video,
generates a summary when needed, and sends only the missing artifact to the
repair webhook.  The normal scheduled Drive ingestion workflow then embeds the
new file and moves it to Completed.
"""

from __future__ import annotations

import argparse
import os
import re
import time
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
TYPE_SUFFIX = re.compile(
    r"[\s_-]*(?:transcript|summary|summaryplaud)\s*$", re.IGNORECASE
)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def normalize_stem(value: str) -> str:
    stem = TYPE_SUFFIX.sub("", Path(value).stem).casefold()
    return "".join(character for character in stem if character.isalnum())


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

    def get_all(self, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page_size = 1000
        offset = 0
        while True:
            response = self.session.get(
                f"{self.base_url}/rest/v1/{table}",
                params=params,
                headers={"Range": f"{offset}-{offset + page_size - 1}"},
                timeout=60,
            )
            response.raise_for_status()
            page = response.json()
            if not isinstance(page, list):
                raise TypeError(f"Unexpected Supabase response for {table}")
            rows.extend(row for row in page if isinstance(row, dict))
            if len(page) < page_size:
                return rows
            offset += page_size


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-slug", default="collaborative-process")
    parser.add_argument("--bucket", default="TCP-MASTER")
    parser.add_argument("--only", help="Repair one source_video_id")
    parser.add_argument("--limit", type=int, help="Maximum gaps to process")
    parser.add_argument("--poll-seconds", type=float, default=10)
    parser.add_argument("--job-timeout-minutes", type=float, default=180)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Process and upload missing artifacts. Default is dry-run.",
    )
    return parser.parse_args()


def find_video_paths(bucket: Any) -> list[str]:
    return [
        file_version.file_name
        for file_version, _folder_name in bucket.ls(recursive=True)
        if PurePosixPath(file_version.file_name).suffix.casefold() in VIDEO_EXTENSIONS
    ]


def match_video_path(
    row: dict[str, Any],
    existing_paths: set[str],
    by_filename: dict[str, list[str]],
    by_stem: dict[str, list[str]],
) -> tuple[str | None, str | None]:
    current = str(row.get("b2_path") or "").strip()
    if current and current in existing_paths:
        return current, None

    source_file = PurePosixPath(str(row.get("source_file") or "")).name
    candidates = by_filename.get(source_file.casefold(), []) if source_file else []
    if not candidates:
        key = normalize_stem(source_file or str(row.get("source_video_id") or ""))
        candidates = by_stem.get(key, [])
    candidates = list(dict.fromkeys(candidates))
    if len(candidates) == 1:
        return candidates[0], None
    if not candidates:
        return None, "no B2 video match"
    return None, f"ambiguous B2 match ({len(candidates)} files)"


def submit_and_wait(
    session: requests.Session,
    *,
    api_url: str,
    api_key: str,
    bucket: str,
    object_path: str,
    b2_key_id: str,
    b2_application_key: str,
    poll_seconds: float,
    timeout_minutes: float,
) -> str:
    response = session.post(
        f"{api_url.rstrip('/')}/transcribe",
        headers={"X-API-KEY": api_key},
        json={
            "b2_bucket": bucket,
            "b2_file_path": object_path,
            "b2_key_id": b2_key_id,
            "b2_application_key": b2_application_key,
        },
        timeout=60,
    )
    response.raise_for_status()
    job_id = str(response.json()["job_id"])
    deadline = time.monotonic() + timeout_minutes * 60

    while time.monotonic() < deadline:
        status_response = session.get(
            f"{api_url.rstrip('/')}/jobs/{job_id}",
            headers={"X-API-KEY": api_key},
            timeout=60,
        )
        status_response.raise_for_status()
        job = status_response.json()
        status = job.get("status")
        if status == "completed":
            if job.get("has_audio") is False:
                raise RuntimeError("source video has no audio")
            transcript = str(job.get("transcript") or "").strip()
            if not transcript:
                raise RuntimeError("transcription completed without text")
            return transcript
        if status == "failed":
            raise RuntimeError(str(job.get("error") or "transcription failed"))
        time.sleep(poll_seconds)
    raise TimeoutError(f"transcription job {job_id} did not finish before timeout")


def n8n_post(
    session: requests.Session,
    *,
    base_url: str,
    path: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    response = session.post(
        f"{base_url.rstrip('/')}/{path.lstrip('/')}", json=body, timeout=180
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise TypeError(f"Unexpected n8n response from {path}")
    return payload


def main() -> int:
    args = parse_args()
    supabase = SupabaseRest(
        required_env("SUPABASE_URL"), required_env("SUPABASE_SERVICE_KEY")
    )
    clients = supabase.get_all(
        "clients_registry",
        {
            "select": (
                "client_id,slug,display_name,b2_bucket,"
                "drive_transcripts_intake_folder_id,drive_summaries_intake_folder_id"
            ),
            "slug": f"eq.{args.client_slug}",
            "status": "eq.active",
        },
    )
    if len(clients) != 1:
        raise SystemExit(
            f"Expected one active client for slug {args.client_slug!r}; found {len(clients)}"
        )
    client = clients[0]
    if client.get("b2_bucket") != args.bucket:
        raise SystemExit(
            f"Workspace bucket is {client.get('b2_bucket')!r}, not {args.bucket!r}"
        )

    client_id = str(client["client_id"])
    videos = supabase.get_all(
        "video_summaries",
        {
            "select": "summary_id,source_video_id,title,source_file,b2_path,summary_text",
            "client_id": f"eq.{client_id}",
            "order": "updated_at.asc",
        },
    )
    segments = supabase.get_all(
        "transcript_segments",
        {
            "select": "source_video_id,chunk_index,transcript_text",
            "client_id": f"eq.{client_id}",
            "order": "source_video_id.asc,chunk_index.asc",
        },
    )
    transcript_ids = {
        str(row["source_video_id"]) for row in segments if row.get("source_video_id")
    }
    transcript_parts: dict[str, list[str]] = defaultdict(list)
    for segment in segments:
        source_id = str(segment.get("source_video_id") or "")
        text = str(segment.get("transcript_text") or "").strip()
        if source_id and text:
            transcript_parts[source_id].append(text)
    existing_transcripts = {
        source_id: "\n\n".join(parts) for source_id, parts in transcript_parts.items()
    }
    video_ids = {
        str(row.get("source_video_id") or "")
        for row in videos
        if row.get("source_video_id")
    }
    # A transcript can be indexed even when the corresponding summary upsert
    # never completed. Include those sources in the repair inventory instead
    # of limiting the audit to existing video_summaries rows.
    for transcript_only_id in sorted(transcript_ids - video_ids):
        videos.append(
            {
                "summary_id": None,
                "source_video_id": transcript_only_id,
                "title": transcript_only_id,
                "source_file": transcript_only_id,
                "b2_path": None,
                "summary_text": None,
            }
        )

    gaps = []
    for video in videos:
        source_id = str(video.get("source_video_id") or "")
        missing_transcript = source_id not in transcript_ids
        missing_summary = not str(video.get("summary_text") or "").strip()
        if missing_transcript or missing_summary:
            gaps.append((video, missing_transcript, missing_summary))
    if args.only:
        gaps = [gap for gap in gaps if str(gap[0].get("source_video_id")) == args.only]
    if args.limit is not None:
        gaps = gaps[: max(0, args.limit)]

    key_id = required_env("B2_KEY_ID")
    application_key = required_env("B2_APPLICATION_KEY")
    account_info = InMemoryAccountInfo()
    b2_api = B2Api(account_info)
    b2_api.authorize_account("production", key_id, application_key)
    bucket = b2_api.get_bucket_by_name(args.bucket)
    paths = find_video_paths(bucket)
    existing_paths = set(paths)
    by_filename: dict[str, list[str]] = defaultdict(list)
    by_stem: dict[str, list[str]] = defaultdict(list)
    for object_path in paths:
        filename = PurePosixPath(object_path).name
        by_filename[filename.casefold()].append(object_path)
        by_stem[normalize_stem(filename)].append(object_path)

    print(f"Workspace: {client['display_name']} ({client_id})")
    print(f"Bucket: {args.bucket}; B2 videos: {len(paths)}")
    print(f"Database sources: {len(videos)}; incomplete sources: {len(gaps)}")
    print("Mode:", "APPLY" if args.apply else "DRY RUN")

    matches: list[tuple[dict[str, Any], bool, bool, str | None]] = []
    for video, missing_transcript, missing_summary in gaps:
        source_id = str(video.get("source_video_id") or "")
        status = ", ".join(
            label
            for label, missing in (
                ("transcript", missing_transcript),
                ("summary", missing_summary),
            )
            if missing
        )
        if not missing_transcript and existing_transcripts.get(source_id):
            print(f"MATCH {source_id}: missing {status}; using indexed transcript")
            matches.append((video, missing_transcript, missing_summary, None))
            continue

        object_path, reason = match_video_path(
            video, existing_paths, by_filename, by_stem
        )
        if not object_path:
            print(f"SKIP {source_id}: missing {status}; {reason}")
            continue
        print(f"MATCH {source_id}: missing {status}; B2={object_path}")
        matches.append((video, missing_transcript, missing_summary, object_path))

    if not args.apply:
        print("No changes were made. Review the matches, then re-run with --apply.")
        return 0

    transcript_folder = str(client.get("drive_transcripts_intake_folder_id") or "")
    summary_folder = str(client.get("drive_summaries_intake_folder_id") or "")
    if not transcript_folder or not summary_folder:
        raise SystemExit("Workspace intake folders are not fully configured")

    api_url = required_env("TRANSCRIPTION_API_URL")
    api_key = required_env("API_KEY")
    n8n_url = required_env("N8N_BASE_URL")
    session = requests.Session()
    repaired = failed = 0
    for video, missing_transcript, missing_summary, object_path in matches:
        source_id = str(video["source_video_id"])
        try:
            transcript = existing_transcripts.get(source_id, "")
            if missing_transcript or not transcript:
                if not object_path:
                    raise RuntimeError("no B2 video available for transcription")
                transcript = submit_and_wait(
                    session,
                    api_url=api_url,
                    api_key=api_key,
                    bucket=args.bucket,
                    object_path=object_path,
                    b2_key_id=key_id,
                    b2_application_key=application_key,
                    poll_seconds=args.poll_seconds,
                    timeout_minutes=args.job_timeout_minutes,
                )
            if missing_transcript:
                n8n_post(
                    session,
                    base_url=n8n_url,
                    path="repair-ingestion-file",
                    body={
                        "kind": "transcript",
                        "filename": source_id,
                        "content": transcript,
                        "folder_id": transcript_folder,
                    },
                )
            if missing_summary:
                summary_payload = n8n_post(
                    session,
                    base_url=n8n_url,
                    path="summarize-text",
                    body={"transcript_text": transcript},
                )
                summary = str(summary_payload.get("summary") or "").strip()
                if not summary:
                    raise RuntimeError("summarization returned no text")
                n8n_post(
                    session,
                    base_url=n8n_url,
                    path="repair-ingestion-file",
                    body={
                        "kind": "summary",
                        "filename": source_id,
                        "content": summary,
                        "folder_id": summary_folder,
                    },
                )
            repaired += 1
            print(f"QUEUED REPAIR {source_id}")
        except Exception as error:  # noqa: BLE001 - continue the repair batch
            failed += 1
            print(f"FAILED {source_id}: {error}")

    print(f"Repair result: queued={repaired}, failed={failed}")
    print("Run the scheduled Drive ingestion workflow, then repeat the dry-run audit.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
