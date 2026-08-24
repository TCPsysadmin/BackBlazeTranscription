# Ingestion gap reconciliation

This repair is intentionally two-stage. The audit is read-only; `--apply` can
retranscribe videos and write missing files, so review the dry-run output first.

## 1. Import the repair webhook

Import `n8n-repair-ingestion-file.json` into n8n, verify that both Google Drive
nodes use the existing shared-drive credential, and publish the workflow. It
creates the production endpoint:

```text
POST <N8N_BASE_URL>/repair-ingestion-file
```

The endpoint checks for the exact target filename before uploading. Repeating a
repair therefore reuses the intake file instead of creating another copy.

## 2. Configure the Render shell

Run the script on the BackBlazeTranscription Render service, where B2 and
transcription dependencies are already installed. It requires:

```text
SUPABASE_URL
SUPABASE_SERVICE_KEY
B2_KEY_ID
B2_APPLICATION_KEY
API_KEY
TRANSCRIPTION_API_URL=https://backblazetranscription.onrender.com
N8N_BASE_URL=https://thecollaborativeprocess.app.n8n.cloud/webhook
```

`API_KEY` must be the service's existing transcription API key. Do not paste
credentials into command history.

## 3. Audit TCP without changing anything

```bash
python scripts/reconcile_ingestion_gaps.py \
  --client-slug collaborative-process \
  --bucket TCP-MASTER
```

The script lists every database video missing transcript chunks, summary text,
or both. It also matches the row to a B2 video. No changes occur without
`--apply`; missing and ambiguous B2 matches are always skipped.

## 4. Repair or restore BURNANDRETURN first

If the database audit reports a missing transcript, use the exact
`source_video_id` printed by the audit:

```bash
python scripts/reconcile_ingestion_gaps.py \
  --client-slug collaborative-process \
  --bucket TCP-MASTER \
  --only "TCP003_MEETINGS_20251104 - BURNANDRETURN - MEETING RECORDING" \
  --apply
```

Run the scheduled Drive-to-Supabase ingestion workflow. Confirm that the new
transcript reached Completed, transcript chunks exist, and the agent can find
the source before processing the remaining backlog.

If Supabase already has transcript chunks but the completed Drive transcript
file is missing, restore that artifact from the indexed transcript instead:

```bash
python scripts/reconcile_ingestion_gaps.py \
  --client-slug collaborative-process \
  --bucket TCP-MASTER \
  --only "TCP003_MEETINGS_20251104 - BURNANDRETURN - MEETING RECORDING" \
  --restore-drive-artifact transcript \
  --apply
```

`--restore-drive-artifact` requires `--only`; it cannot recreate files for a
whole workspace based solely on unequal folder counts.

## 5. Repair the remaining unambiguous matches

```bash
python scripts/reconcile_ingestion_gaps.py \
  --client-slug collaborative-process \
  --bucket TCP-MASTER \
  --apply
```

Run scheduled ingestion until both intake folders are empty, then repeat the
dry-run. A clean result reports zero incomplete database videos. Silent videos
and rows with missing or ambiguous B2 matches need manual review and are never
fabricated by this script.
