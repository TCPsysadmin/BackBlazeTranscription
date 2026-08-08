# VP media ingestion rollout

This repository now contains two importable n8n workflows:

- `n8n-flow3-summarize-to-drive.json` — client list, summary generation,
  idempotent Drive upload, and B2/Drive manifest registration.
- `n8n-vp-drive-to-supabase.json` — immediate single-file webhook plus scheduled
  recovery scan, embeddings, video upsert, transcript chunk replacement, and
  completed-folder moves.

## Required order

1. Run `-vp-collaborative/ingestion_manifest_migration.sql` in Supabase.
2. Fill the test-company completed-folder IDs in `clients_registry`.
3. Import both workflow JSON files into n8n.
4. On every newly added Postgres node, select the same Supabase Postgres
   credential used by the existing ingestion workflow.
5. On the two existing Drive upload nodes, retain/select the current Google Drive
   OAuth credential. Retain the existing OpenAI/xAI credentials.
6. Activate both workflows and confirm these production webhook paths:
   - `/webhook/list-clients`
   - `/webhook/summarize-text`
   - `/webhook/ingest-to-drive`
   - `/webhook/ingest-drive-file`
7. Deploy the IngestionHub feature branch with `VITE_N8N_URL` set to the n8n
   `/webhook` base URL.
8. Deploy BackBlazeTranscription with the archive environment variables below.
9. Deploy `-vp-collaborative`, then `vp_frontend`.

## Render environment

```text
B2_KEY_ID=<restricted server-side key ID>
B2_APPLICATION_KEY=<restricted server-side application key>
B2_ARCHIVE_BUCKET=VPStorage-testcompany
B2_VIDEO_PREFIX=videos
B2_THUMBNAIL_PREFIX=thumbnails
THUMBNAIL_AT_SECONDS=3
```

The archive key needs read/write access to `VPStorage-testcompany`. Do not expose
these values through a `VITE_` or `NEXT_PUBLIC_` variable.

## Test-company mapping

Known values:

```text
client_id=fdefa887-b08f-43ba-8efc-b5c6bcf3e25a
transcripts_intake=151MoUx2IgMFmgFQ-fo-yyncJf865m1Sk
summaries_intake=150mp-jn2U6O8YT_KjPNqkYImGXDLsdzE
b2_bucket=VPStorage-testcompany
```

The transcript and summary **Completed** folder IDs still need to be copied from
Google Drive into the migration's commented `update clients_registry` statement.

## Expected test

1. Upload one uniquely named video in IngestionHub and select only test-company.
2. Confirm the original and WebP thumbnail appear in B2.
3. Edit and approve the transcript and summary.
4. Confirm exactly one transcript and one summary are created in Drive.
5. Confirm both Drive files move to their corresponding Completed folders.
6. Confirm one `ingestion_manifests` row and one `video_summaries` row exist.
7. Confirm transcript segments exist and no stale higher chunk indexes remain.
8. Sign in as a test-company user, open Knowledge Base, and verify the thumbnail
   and all three downloads.
9. Retry the approved queue item and verify the Drive result is reused instead of
   creating duplicate documents.
