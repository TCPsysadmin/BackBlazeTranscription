# Consolidated ingestion v3 rollout

Import `n8n-vp-ingestion-v3-all-workspaces.json` as a **new**, inactive n8n
workflow. Do not overwrite or deactivate the current production workflow until
the manual tests below pass.

## What this workflow does

- Loads every active, configured workspace from `public.clients_registry`.
- Polls each workspace's transcript and summary intake folders.
- Processes at most 10 newest files per workspace per scheduled run so fresh
  uploads are not blocked by an old or repeatedly failing intake file.
- Ends at `Execution Summary` with exact counts and filenames for files
  ingested or failed during that n8n execution.
- Detects transcript versus summary from the filename, including
  `_SummaryPlaud`.
- Resolves the existing `ingestion_manifests` record before indexing so B2
  video paths, thumbnails, and the real source extension are preserved.
- Upserts `video_summaries` and transcript chunks without allowing a summary
  pass to erase transcript timestamp state.
- Removes stale tail chunks when a shorter transcript is re-ingested.
- Records the processed Drive file ID in `ingestion_manifests`, which allows
  the Knowledge Base deletion feature to move that artifact to Drive Trash.
- Moves successful files to the correct completed folder.
- Logs one bad file to `job_runs` and continues with the remaining batch.

The workflow does not create workspaces or Drive folders. Workspace storage
provisioning remains a separate workflow.

## Import and credential check

1. Import `n8n-vp-ingestion-v3-all-workspaces.json`.
2. Keep the imported workflow inactive.
3. Confirm every Google Drive node uses `Google Drive account`.
4. Confirm every Postgres node uses `Postgres account`.
5. Confirm **Create Embeddings** uses `OpenAi account`.
6. Save the workflow.

The export contains credential references copied from the supplied n8n export,
but n8n may require them to be selected again after import.

## Safe manual test

Use a test workspace with all four Drive folder IDs configured in
`clients_registry`.

1. Put one small transcript and its matching summary in the intake folders:

   ```text
   INGESTION_V3_TEST_transcript.txt
   INGESTION_V3_TEST_summary.txt
   ```

2. Run **Manual Run** once.
3. Confirm both files move to their correct completed folders.
4. Confirm `video_summaries` contains one `INGESTION_V3_TEST` row with summary
   text.
5. Confirm `transcript_segments` contains chunks for the same client and source
   ID.
6. Confirm `ingestion_manifests` contains both Drive file IDs on that source.
7. Confirm `job_runs` contains successful ingestion entries.
8. Open Knowledge Base and confirm the item appears only in that workspace.
9. Ask the agent a question whose answer exists only in the test transcript.

Use this audit query:

```sql
select
  v.client_id,
  v.source_video_id,
  length(coalesce(v.summary_text, '')) as summary_length,
  count(distinct t.segment_id) as transcript_segments,
  m.transcript_drive_file_id,
  m.summary_drive_file_id,
  m.b2_path,
  m.thumbnail_b2_path
from public.video_summaries v
left join public.transcript_segments t
  on t.client_id = v.client_id
 and t.source_video_id = v.source_video_id
left join public.ingestion_manifests m
  on m.client_id = v.client_id
 and m.source_video_id = v.source_video_id
where v.source_video_id = 'INGESTION_V3_TEST'
group by
  v.client_id,
  v.source_video_id,
  v.summary_text,
  m.transcript_drive_file_id,
  m.summary_drive_file_id,
  m.b2_path,
  m.thumbnail_b2_path;
```

## Failure and isolation tests

Before activation, also verify:

1. Run with another configured workspace containing no intake files. The first
   empty workspace must not prevent later workspaces from running.
2. Add an invalid or empty `.txt` file beside a valid file. The invalid file
   must create a failed `job_runs` entry while the valid file still completes.
3. Re-upload a shorter version of the transcript. Old tail chunks must be
   removed.
4. Upload matching transcript and summary files into the opposite intake
   folders. Filename detection must still route each file to the correct
   completed folder.
5. Verify `BURNANDRETURN_summary.txt` and `BURNANDRETURN_transcript.txt` resolve
   to the same `source_video_id`.

## Cutover

After the tests pass:

1. Deactivate the old scheduled Drive-to-Supabase workflow.
2. Activate this workflow.
3. Do not leave both schedules active; they would race over the same files.
4. The schedule runs every 50 minutes, matching the supplied production
   workflow. Use **Manual Run** when an immediate pass is required.
5. Monitor the first scheduled execution and inspect `job_runs` afterward.

The generated workflow is deliberately committed with `active: false` so an
import cannot silently start a second production scheduler.
