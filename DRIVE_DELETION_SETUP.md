# Google Drive media deletion workflow

The `n8n-delete-media-drive-files.json` workflow is the private server-to-server
workflow used by `-vp-collaborative` when an owner or admin deletes Knowledge
Base media. It moves the manifest's transcript and summary files to Google
Drive Trash.

## Install

1. Import the JSON workflow into the same n8n project that owns the existing
   `Google Drive account` OAuth credential.
2. Create a Header Auth credential named `VP Media Delete Webhook`:
   - Header name: `X-VP-Media-Delete-Secret`
   - Header value: generate a new high-entropy secret.
3. Select that Header Auth credential on `Delete Media Drive Files`.
4. Select the existing Drive OAuth credential on `Move File to Trash`.
5. Publish the workflow and copy its production webhook URL. It should end in
   `/webhook/delete-media-drive-files`.
6. In Render, set `MEDIA_DELETION_WEBHOOK_URL` to that production URL and
   `MEDIA_DELETION_WEBHOOK_SECRET` to the same Header Auth value, then redeploy.

The workflow is intentionally recoverable: Drive files are moved to Trash
rather than permanently erased. If this workflow fails, the backend leaves the
B2 objects and Supabase knowledge records in place so deletion can be retried.
