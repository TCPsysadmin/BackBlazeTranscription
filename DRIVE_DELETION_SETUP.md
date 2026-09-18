# Google Drive media deletion workflow

`n8n-delete-media-drive-files.json` is the private server-to-server workflow
used when a workspace owner or admin deletes Knowledge Base media. It uses the
same `Google Drive account` OAuth credential as the existing ingestion flows
and moves the manifest's transcript and summary files to Google Drive Trash.

It does not permanently erase Drive files. It does not expose the Drive OAuth
credential or deletion secret to the browser.

## 1. Import the workflow

1. In GitHub, switch `BackBlazeTranscription` to branch
   `task/drive-media-deletion`.
2. Download `n8n-delete-media-drive-files.json`.
3. In the same n8n project as the ingestion workflows, choose **Workflows**,
   **Add workflow**, then **Import from File**.
4. Select the downloaded JSON and save the workflow.

The imported canvas includes a **Setup Notes** card explaining its request and
response. Do not activate the workflow until both credentials below are set.

## 2. Connect the existing Google Drive credential

1. Open **Move File to Trash**.
2. Confirm **Credential to connect with** is `Google Drive account`.
3. If n8n shows a credential warning after import, select the same Google Drive
   OAuth credential used by `Upload Transcript to Drive`, `Upload Summary to
   Drive`, and the other ingestion nodes.
4. Confirm **Delete Permanently** is disabled. This must remain disabled so the
   files go to Trash.

The node retries Drive failures up to three times. Do not configure it to
continue after an error: the Render backend must receive an error if any file
was not moved to Trash.

## 3. Create the private webhook credential

1. In n8n, open **Credentials** and create a **Header Auth** credential.
2. Name the credential `VP Media Delete Webhook`.
3. Set the header name to `X-VP-Media-Delete-Secret`.
4. Set the header value to a new random secret from a password manager.
5. Save it, then open **Delete Media Drive Files** and select that credential.

Keep the secret private. It belongs only in n8n and the Render backend—not in
Vercel and not in any `NEXT_PUBLIC_` variable.

## 4. Test in n8n before activation

Use a disposable Drive file because this test moves the file to Trash.

1. Open **Delete Media Drive Files** and select **Listen for test event**.
2. Copy the node's test URL. It contains `/webhook-test/`.
3. In PowerShell, replace the three placeholders and run:

```powershell
$deleteUrl = 'PASTE_N8N_TEST_URL'
$deleteSecret = 'PASTE_HEADER_AUTH_SECRET'
$driveFileId = 'PASTE_DISPOSABLE_DRIVE_FILE_ID'
$deleteBody = @{ file_ids = @($driveFileId) } | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri $deleteUrl `
  -Headers @{ 'X-VP-Media-Delete-Secret' = $deleteSecret } `
  -ContentType 'application/json' `
  -Body $deleteBody
```

Expected response:

```json
{
  "ok": true,
  "trashed": 1
}
```

Confirm the disposable file is in Google Drive Trash. Restore it if desired.
Also try the request with the wrong secret and confirm n8n rejects it.

## 5. Activate and connect Render

1. Save and activate/publish **VP Delete Media Drive Files**.
2. Open **Delete Media Drive Files** and copy its **Production URL**. Use the
   URL containing `/webhook/`, not `/webhook-test/`. It should end with:

   ```text
   /webhook/delete-media-drive-files
   ```

3. In the Render dashboard, open the `-vp-collaborative` backend service and
   add these secret environment variables:

   ```text
   MEDIA_DELETION_WEBHOOK_URL=<the n8n production URL>
   MEDIA_DELETION_WEBHOOK_SECRET=<the exact Header Auth secret>
   ```

4. Remove the unused direct-Google variables if they exist:

   ```text
   GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON
   GOOGLE_DRIVE_DELEGATED_USER
   ```

5. Deploy the backend's `task/bulk-media-management` branch after its n8n
   restoration commit is pushed. No Vercel environment change is required.

## 6. End-to-end test

1. Sign in as an owner or admin of the test workspace.
2. Choose a disposable video whose ingestion manifest contains transcript and
   summary Drive file IDs.
3. In Knowledge Base, select the video and click **Delete**.
4. Confirm both Drive exports are in Trash.
5. Confirm the source video and thumbnail are gone from the workspace's B2
   bucket.
6. Refresh Knowledge Base and confirm the video is gone.

The operation order is Drive Trash, B2 cleanup, then the atomic Supabase record
deletion. If Drive or B2 cleanup fails, the database records remain so the
delete can be retried. No additional SQL migration is required beyond the
media-management migration.

## Troubleshooting

- **401 from n8n:** the Render secret does not exactly match the Header Auth
  value, or the credential is not selected on the webhook node.
- **404 from n8n:** the test URL was used after test mode stopped, the production
  workflow is inactive, or Render has the wrong webhook URL.
- **Google credential error:** reselect `Google Drive account` on **Move File to
  Trash** and reconnect it if n8n reports expired OAuth access.
- **Drive permission error:** verify the connected Google account can move the
  target Shared Drive files to Trash.
- **Backend returns 502:** inspect the matching n8n execution first; the backend
  intentionally leaves B2 and Supabase data intact when Drive cleanup fails.
