"""Build the production, all-workspace Drive-to-Supabase ingestion workflow.

The input is an n8n export containing the v3 processing nodes.  The builder
deliberately discards its duplicate onboarding and legacy ingestion graphs and
emits one inactive workflow that can be reviewed before it is published.
"""

from __future__ import annotations

import argparse
import copy
import json
import uuid
from pathlib import Path

OUTPUT_NAME = "VP Ingestion v3 - All Workspaces (Manifest Aware)"

PROCESSING_NODES = {
    "Find transcript files",
    "Find summary files",
    "tag transcript files",
    "tag summary files",
    "Merge intake",
    "Sort files oldest first1",
    "Limit1",
    "Loop Over Items2",
    "Extract Identity of File1",
    "Download file1",
    "Extract from File1",
    "Chunk transcripts1",
    "embed with OpenAI",
    "format embeddings1",
    "upsert video2",
    "write transcript chunks",
    "log success",
    "Move file1",
    "build failure record",
    "log failure",
}

WORKSPACE_QUERY = """select
  c.client_id::text as client_id,
  c.slug as client_slug,
  c.display_name,
  replace(coalesce(c.drive_transcripts_intake_folder_id, ''), E'\\\\_', '_')
    as transcripts_intake_folder_id,
  replace(coalesce(c.drive_transcripts_completed_folder_id, ''), E'\\\\_', '_')
    as transcripts_done_folder_id,
  replace(coalesce(c.drive_summaries_intake_folder_id, ''), E'\\\\_', '_')
    as summaries_intake_folder_id,
  replace(coalesce(c.drive_summaries_completed_folder_id, ''), E'\\\\_', '_')
    as summaries_done_folder_id,
  10::int as max_files_per_run
from public.clients_registry c
where c.status = 'active'
  and (
    (c.drive_transcripts_intake_folder_id is not null
     and c.drive_transcripts_completed_folder_id is not null)
    or
    (c.drive_summaries_intake_folder_id is not null
     and c.drive_summaries_completed_folder_id is not null)
  )
order by lower(c.display_name), c.client_id;"""

RESOLVE_QUERY = """select
  coalesce(m.client_id, $1::uuid)::text as client_id,
  coalesce(nullif(m.source_video_id, ''), $2::text) as source_video_id,
  coalesce(nullif(m.title, ''), $3::text) as title,
  coalesce(nullif(m.source_file, ''), $4::text) as source_file,
  m.b2_bucket,
  m.b2_path,
  m.thumbnail_b2_path,
  case
    when m.transcript_drive_file_id = $5::text then 'transcripts'
    when m.summary_drive_file_id = $5::text then 'summaries'
    else $6::text
  end as ingestion_type,
  $5::text as file_id,
  $7::text as file_name,
  nullif($8::text, '') as file_size,
  nullif($9::text, '') as file_mime_type,
  nullif($10::text, '') as created_time,
  case
    when m.transcript_drive_file_id = $5::text then nullif($11::text, '')
    when m.summary_drive_file_id = $5::text then nullif($12::text, '')
    else nullif($13::text, '')
  end as done_folder_id,
  (m.manifest_id is not null) as manifest_found
from (select 1) anchor
left join lateral (
  select *
  from public.ingestion_manifests
  where transcript_drive_file_id = $5::text
     or summary_drive_file_id = $5::text
     or (client_id = $1::uuid and source_video_id = $2::text)
  order by
    case when transcript_drive_file_id = $5::text
           or summary_drive_file_id = $5::text then 0 else 1 end,
    updated_at desc
  limit 1
) m on true;"""

RESOLVE_REPLACEMENTS = """={{ [
  $('Extract File Identity').first().json.client_id,
  $('Extract File Identity').first().json.source_video_id,
  $('Extract File Identity').first().json.title,
  $('Extract File Identity').first().json.source_file,
  $('Extract File Identity').first().json.file_id,
  $('Extract File Identity').first().json.ingestion_type,
  $('Extract File Identity').first().json.file_name,
  $('Extract File Identity').first().json.file_size || '',
  $('Extract File Identity').first().json.file_mime_type || '',
  $('Extract File Identity').first().json.created_time || '',
  $('Extract File Identity').first().json.transcripts_done_folder_id || '',
  $('Extract File Identity').first().json.summaries_done_folder_id || '',
  $('Extract File Identity').first().json.done_folder_id || ''
] }}"""

UPSERT_QUERY = """insert into public.video_summaries
  (client_id, source_video_id, title, source_file, has_timestamps,
   summary_text, summary_embedding, topics, b2_path, thumbnail_b2_path)
values
  ($1, $2, $3, $4, $5, nullif($6, 'null'), nullif($7, 'null')::vector,
   nullif($8, 'null')::text[], nullif($9, 'null'), nullif($10, 'null'))
on conflict (client_id, source_video_id) do update set
  title = excluded.title,
  source_file = case
    when excluded.b2_path is not null then excluded.source_file
    else coalesce(video_summaries.source_file, excluded.source_file)
  end,
  has_timestamps = coalesce(excluded.has_timestamps, false)
                   or coalesce(video_summaries.has_timestamps, false),
  summary_text = case
    when $11::boolean = false and excluded.summary_text is not null
      then excluded.summary_text
    when video_summaries.summary_text is null then excluded.summary_text
    else video_summaries.summary_text
  end,
  summary_embedding = case
    when $11::boolean = false and excluded.summary_embedding is not null
      then excluded.summary_embedding
    when video_summaries.summary_embedding is null then excluded.summary_embedding
    else video_summaries.summary_embedding
  end,
  topics = case
    when excluded.topics is not null and array_length(excluded.topics, 1) > 0
      then excluded.topics
    else video_summaries.topics
  end,
  b2_path = coalesce(excluded.b2_path, video_summaries.b2_path),
  thumbnail_b2_path = coalesce(
    excluded.thumbnail_b2_path,
    video_summaries.thumbnail_b2_path
  ),
  updated_at = now()
returning summary_id;"""

MANIFEST_QUERY = """insert into public.ingestion_manifests (
  idempotency_key, client_id, source_video_id, title, source_file,
  b2_bucket, b2_path, thumbnail_b2_path,
  transcript_drive_file_id, summary_drive_file_id, status, error
)
values (
  'drive-index:' || $1::text || ':' || $2::text,
  $1::uuid, $2::text, $3::text, $4::text,
  nullif($5::text, ''), nullif($6::text, ''), nullif($7::text, ''),
  case when $8::text = 'transcripts' then $9::text else null end,
  case when $8::text = 'summaries' then $9::text else null end,
  'completed', null
)
on conflict (client_id, source_video_id) do update set
  title = coalesce(nullif(ingestion_manifests.title, ''), excluded.title),
  source_file = coalesce(
    nullif(ingestion_manifests.source_file, ''),
    excluded.source_file
  ),
  b2_bucket = coalesce(ingestion_manifests.b2_bucket, excluded.b2_bucket),
  b2_path = coalesce(ingestion_manifests.b2_path, excluded.b2_path),
  thumbnail_b2_path = coalesce(
    ingestion_manifests.thumbnail_b2_path,
    excluded.thumbnail_b2_path
  ),
  transcript_drive_file_id = case
    when $8::text = 'transcripts' then $9::text
    else ingestion_manifests.transcript_drive_file_id
  end,
  summary_drive_file_id = case
    when $8::text = 'summaries' then $9::text
    else ingestion_manifests.summary_drive_file_id
  end,
  status = 'completed',
  error = null,
  updated_at = now()
returning manifest_id;"""

SUMMARY_QUERY = """select
  $1::text as execution_id,
  case
    when count(*) filter (where status = 'failed') > 0 then 'completed_with_errors'
    when count(*) filter (where status = 'succeeded') > 0 then 'completed'
    else 'no_files_processed'
  end as result,
  count(*) filter (where status = 'succeeded')::int as files_ingested,
  count(*) filter (where status = 'failed')::int as files_failed,
  coalesce(
    jsonb_agg(payload->>'file_name' order by finished_at)
      filter (where status = 'succeeded'),
    '[]'::jsonb
  ) as ingested_files,
  coalesce(
    jsonb_agg(
      jsonb_build_object(
        'file_name', payload->>'file_name',
        'error', error
      ) order by finished_at
    ) filter (where status = 'failed'),
    '[]'::jsonb
  ) as failed_files
from public.job_runs
where kind = 'ingest'
  and payload->>'execution_id' = $1::text;"""


def stable_id(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"vp-consolidated-ingestion/{label}"))


def edge(node: str, index: int = 0) -> dict[str, object]:
    return {"node": node, "type": "main", "index": index}


def node_by_name(nodes: list[dict], name: str) -> dict:
    return next(node for node in nodes if node["name"] == name)


def clone_node(
    source_nodes: list[dict], name: str, new_name: str | None = None
) -> dict:
    node = copy.deepcopy(node_by_name(source_nodes, name))
    if new_name:
        node["name"] = new_name
        node["id"] = stable_id(new_name)
    return node


def replace_reference(value: object, old: str, new: str) -> object:
    if isinstance(value, str):
        return value.replace(f"$('{old}')", f"$('{new}')")
    if isinstance(value, list):
        return [replace_reference(item, old, new) for item in value]
    if isinstance(value, dict):
        return {key: replace_reference(item, old, new) for key, item in value.items()}
    return value


def postgres_node(
    template: dict,
    name: str,
    position: list[int],
    query: str,
    replacements: str | None = None,
) -> dict:
    node = copy.deepcopy(template)
    node.update({"id": stable_id(name), "name": name, "position": position})
    options = {"queryReplacement": replacements} if replacements else {}
    node["parameters"] = {
        "operation": "executeQuery",
        "query": query,
        "options": options,
    }
    node.pop("onError", None)
    node.pop("alwaysOutputData", None)
    return node


def build(source: Path, output: Path) -> None:
    workflow = json.loads(source.read_text(encoding="utf-8-sig"))
    source_nodes = workflow["nodes"]
    nodes = [
        copy.deepcopy(node) for node in source_nodes if node["name"] in PROCESSING_NODES
    ]

    postgres_template = clone_node(source_nodes, "Load ingestion config")
    manual = copy.deepcopy(
        next(
            node
            for node in source_nodes
            if node["type"] == "n8n-nodes-base.manualTrigger"
        )
    )
    manual.update(
        {"id": stable_id("Manual Run"), "name": "Manual Run", "position": [-2400, 1840]}
    )
    schedule = clone_node(source_nodes, "Schedule Trigger1", "Scheduled Run")
    schedule["position"] = [-2400, 2000]

    load_workspaces = postgres_node(
        postgres_template,
        "Load Active Workspaces",
        [-2176, 1920],
        WORKSPACE_QUERY,
    )
    workspace_loop = clone_node(
        source_nodes, "Loop Over Items2", "Loop Over Workspaces"
    )
    workspace_loop["position"] = [-1952, 1920]
    workspace_loop["parameters"] = {"options": {}}

    nodes.extend([manual, schedule, load_workspaces, workspace_loop])

    renames = {
        "Sort files oldest first1": "Sort Files Newest First",
        "Limit1": "Limit Workspace Batch",
        "Loop Over Items2": "Loop Over Files",
        "Extract Identity of File1": "Extract File Identity",
        "Download file1": "Download Drive File",
        "Extract from File1": "Extract Text",
        "Chunk transcripts1": "Prepare Content",
        "embed with OpenAI": "Create Embeddings",
        "format embeddings1": "Format Embeddings",
        "upsert video2": "Upsert Video Summary",
        "write transcript chunks": "Write Transcript Chunks",
        "Move file1": "Move File to Completed",
        "log success": "Log Success",
        "build failure record": "Build Failure Record",
        "log failure": "Log Failure",
    }
    for node in nodes:
        old_name = node["name"]
        if old_name in renames:
            node["name"] = renames[old_name]
            node["id"] = stable_id(node["name"])
    for node in nodes:
        for old_name, new_name in renames.items():
            node["parameters"] = replace_reference(
                node.get("parameters", {}), old_name, new_name
            )

    for name, folder_field in (
        ("Find transcript files", "transcripts_intake_folder_id"),
        ("Find summary files", "summaries_intake_folder_id"),
    ):
        node = node_by_name(nodes, name)
        node["parameters"]["filter"]["folderId"]["value"] = (
            "={{ $('Loop Over Workspaces').item.json."
            + folder_field
            + " || 'UNCONFIGURED' }}"
        )
        node["onError"] = "continueRegularOutput"
        node["alwaysOutputData"] = True

    tag_code = r"""const cfg = $('Loop Over Workspaces').item.json;
const folderId = cfg.%s;
const sourceBranch = '%s';
if (!folderId) return [];
const all = $input.all().map(item => item.json || {});
const failed = all.find(file => file.error && !file.id);
if (failed) {
  // Log this as a failed queue item without blocking the other workspaces.
  return [{ json: {
    drive_search_error: JSON.stringify(failed.error).slice(0, 1000),
    file_id: `drive-search-error:${cfg.client_id}:${sourceBranch}`,
    file_name: `[Drive search failed: ${cfg.display_name} / ${sourceBranch}]`,
    created_time: new Date().toISOString(),
    client_id: cfg.client_id,
    client_slug: cfg.client_slug,
    transcripts_done_folder_id: cfg.transcripts_done_folder_id,
    summaries_done_folder_id: cfg.summaries_done_folder_id,
    max_files_per_run: cfg.max_files_per_run,
    source_branch: sourceBranch
  }, pairedItem: { item: 0 } }];
}
const textFile = /\.(txt|md)$/i;
return all
  .filter(file => file.id && file.mimeType !== 'application/vnd.google-apps.folder' && (
    textFile.test(file.name || '') || file.mimeType === 'text/plain' ||
    file.mimeType === 'application/vnd.google-apps.document'
  ))
  .map(file => ({ json: {
    file_id: file.id,
    file_name: file.name,
    file_size: file.size ?? null,
    file_mime_type: file.mimeType,
    created_time: file.createdTime || file.modifiedTime || null,
    client_id: cfg.client_id,
    client_slug: cfg.client_slug,
    transcripts_done_folder_id: cfg.transcripts_done_folder_id,
    summaries_done_folder_id: cfg.summaries_done_folder_id,
    max_files_per_run: cfg.max_files_per_run,
    source_branch: sourceBranch
  }, pairedItem: { item: 0 } }));"""
    node_by_name(nodes, "tag transcript files")["parameters"]["jsCode"] = tag_code % (
        "transcripts_intake_folder_id",
        "transcripts",
    )
    node_by_name(nodes, "tag summary files")["parameters"]["jsCode"] = tag_code % (
        "summaries_intake_folder_id",
        "summaries",
    )

    node_by_name(nodes, "Limit Workspace Batch")["parameters"]["maxItems"] = (
        "={{ $json.max_files_per_run || 10 }}"
    )

    sorter = node_by_name(nodes, "Sort Files Newest First")
    sorter["parameters"]["jsCode"] = r"""// Search returns every direct child from BOTH intake folders. Prioritize
// recent uploads before the per-workspace Limit node so an old backlog or a
// repeatedly failing legacy file cannot prevent a new transcript/summary pair
// from being ingested.
const files = $input.all()
  .map(i => i.json)
  .filter(f => f && f.file_id);

if (files.length === 0) {
  return [{ json: { no_files: true } }];
}

const timestamp = (file) => {
  const parsed = Date.parse(file.created_time || '');
  return Number.isFinite(parsed) ? parsed : 0;
};

return files
  .sort((a, b) => timestamp(b) - timestamp(a))
  .map(f => ({ json: f, pairedItem: { item: 0 } }));
"""

    has_files = {
        "parameters": {
            "conditions": {
                "options": {
                    "caseSensitive": True,
                    "leftValue": "",
                    "typeValidation": "strict",
                    "version": 3,
                },
                "conditions": [
                    {
                        "id": stable_id("condition/workspace-has-files"),
                        "leftValue": "={{ $json.no_files !== true }}",
                        "rightValue": True,
                        "operator": {
                            "type": "boolean",
                            "operation": "true",
                            "singleValue": True,
                        },
                    }
                ],
                "combinator": "and",
            },
            "options": {},
        },
        "id": stable_id("Workspace Has Files?"),
        "name": "Workspace Has Files?",
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.3,
        "position": [-928, 1920],
    }
    nodes.append(has_files)

    identity = node_by_name(nodes, "Extract File Identity")
    identity["parameters"]["jsCode"] = (
        identity["parameters"]["jsCode"]
        .replace(
            "const cfg  = $('Load ingestion config').first().json;",
            "const cfg = meta;",
        )
        .replace(
            "(set ${ingestionType}_done_folder_id in public.ingestion_sources)",
            "(configure the completed folder in public.clients_registry)",
        )
    )
    identity["parameters"]["jsCode"] = identity["parameters"]["jsCode"].replace(
        "failed_folder_id: cfg.failed_folder_id || null,",
        "failed_folder_id: null,",
    )
    identity["parameters"]["jsCode"] = identity["parameters"]["jsCode"].replace(
        "const cfg = meta;",
        "const cfg = meta;\n\n"
        "if (meta.drive_search_error) {\n"
        "  return [{ json: {\n"
        "    ...meta,\n"
        "    ingestion_type: meta.source_branch,\n"
        "    error: `Drive search failed: ${meta.drive_search_error}`\n"
        "  }, pairedItem: { item: 0 } }];\n"
        "}",
    )

    prepare = node_by_name(nodes, "Prepare Content")
    prepare_code = prepare["parameters"]["jsCode"].replace(
        "    chunkIndex++;\n    const nextCursor = targetEnd - OVERLAP_WORDS;",
        "    chunkIndex++;\n    if (targetEnd === words.length) break;\n"
        "    const nextCursor = targetEnd - OVERLAP_WORDS;",
    )
    timestamp_start = prepare_code.index(
        "// Group segments into chunks of ~TARGET_WORDS"
    )
    timestamp_end = prepare_code.index("return finish(chunks, true);", timestamp_start)
    timestamp_chunker = r"""// Group segments into chunks while guaranteeing that each pass advances.
const chunks = [];
let chunkIndex = 0;
let segIdx = 0;

function countWords(s) {
  return s.replace(/\[\d{1,2}:\d{2}(?::\d{2})?\]/g, '')
          .split(/\s+/)
          .filter(Boolean).length;
}

while (segIdx < segments.length) {
  const chunkStart = segIdx;
  const current = [];
  let currentWords = 0;
  let currentEnd = segments[segIdx].end_seconds;

  while (segIdx < segments.length) {
    const segmentWords = countWords(segments[segIdx].text);
    if (currentWords > 0 && currentWords + segmentWords > TARGET_WORDS) break;
    current.push(segments[segIdx].text);
    currentWords += segmentWords;
    currentEnd = segments[segIdx].end_seconds;
    segIdx++;
  }

  chunks.push({
    chunk_index: chunkIndex++,
    start_seconds: segments[chunkStart].start_seconds,
    end_seconds: currentEnd,
    speaker: null,
    transcript_text: current.join(' ').replace(/\s+/g, ' ').trim(),
    word_count: currentWords,
  });

  if (segIdx >= segments.length) break;

  let overlapStart = segIdx;
  let overlapWords = 0;
  while (overlapStart > chunkStart && overlapWords < OVERLAP_WORDS) {
    overlapStart--;
    overlapWords += countWords(segments[overlapStart].text);
  }
  // A single oversized timestamp segment cannot be overlapped safely. Move
  // to the next segment instead of processing the same segment forever.
  segIdx = overlapStart > chunkStart ? overlapStart : Math.max(segIdx, chunkStart + 1);
}

"""
    prepare["parameters"]["jsCode"] = (
        prepare_code[:timestamp_start]
        + timestamp_chunker
        + prepare_code[timestamp_end:]
    )

    resolve = postgres_node(
        postgres_template,
        "Resolve Ingestion Manifest",
        [-192, 2112],
        RESOLVE_QUERY,
        RESOLVE_REPLACEMENTS,
    )
    resolve["onError"] = "continueErrorOutput"
    nodes.append(resolve)

    for node in nodes:
        if node["name"] != "Resolve Ingestion Manifest":
            node["parameters"] = replace_reference(
                node.get("parameters", {}),
                "Extract File Identity",
                "Resolve Ingestion Manifest",
            )
    resolve["parameters"]["options"]["queryReplacement"] = RESOLVE_REPLACEMENTS

    formatter = node_by_name(nodes, "Format Embeddings")
    formatter["parameters"]["jsCode"] = formatter["parameters"]["jsCode"].replace(
        "    has_timestamps: parent.has_timestamps,",
        "    has_timestamps: parent.has_timestamps,\n"
        "    b2_bucket: parent.b2_bucket || null,\n"
        "    b2_path: parent.b2_path || null,\n"
        "    thumbnail_b2_path: parent.thumbnail_b2_path || null,",
    )

    upsert = node_by_name(nodes, "Upsert Video Summary")
    upsert["parameters"]["query"] = UPSERT_QUERY
    upsert["parameters"]["options"]["queryReplacement"] = """={{ [
  $json.client_id, $json.source_video_id, $json.title, $json.source_file,
  $json.has_timestamps, $json.summary_text, $json.summary_embedding,
  $json.topics, $json.b2_path, $json.thumbnail_b2_path, $json.is_auto_summary
] }}"""

    record_manifest = postgres_node(
        postgres_template,
        "Record Drive Artifact",
        [1536, 2112],
        MANIFEST_QUERY,
        """={{ [
  $('Format Embeddings').first().json.client_id,
  $('Format Embeddings').first().json.source_video_id,
  $('Format Embeddings').first().json.title,
  $('Format Embeddings').first().json.source_file,
  $('Format Embeddings').first().json.b2_bucket || '',
  $('Format Embeddings').first().json.b2_path || '',
  $('Format Embeddings').first().json.thumbnail_b2_path || '',
  $('Format Embeddings').first().json.ingestion_type,
  $('Format Embeddings').first().json.file_id
] }}""",
    )
    record_manifest["onError"] = "continueErrorOutput"
    nodes.append(record_manifest)

    move = node_by_name(nodes, "Move File to Completed")
    move["parameters"]["folderId"]["value"] = (
        "={{ $('Resolve Ingestion Manifest').first().json.done_folder_id }}"
    )
    move["onError"] = "continueErrorOutput"
    move["retryOnFail"] = True
    move["maxTries"] = 3

    failure = node_by_name(nodes, "Build Failure Record")
    failure["parameters"]["jsCode"] = """let meta = {};
try { meta = $('Resolve Ingestion Manifest').first().json || {}; }
catch (_) {
  try { meta = $('Extract File Identity').first().json || {}; } catch (_) {}
}
const incoming = $input.first().json || {};
const original = incoming.json || incoming.item || incoming;
const raw = incoming.error || incoming.message || original.error || 'Unknown ingestion error';
let message;
try { message = typeof raw === 'string' ? raw : JSON.stringify(raw); }
catch (_) { message = String(raw); }
return [{ json: {
  client_id: original.client_id || meta.client_id || null,
  file_id: original.file_id || meta.file_id || null,
  file_name: original.file_name || meta.file_name || null,
  source_video_id: original.source_video_id || meta.source_video_id || null,
  ingestion_type: original.ingestion_type || meta.ingestion_type || null,
  error: message.slice(0, 2000)
}, pairedItem: { item: 0 } }];"""

    success_log = node_by_name(nodes, "Log Success")
    success_replacements = success_log["parameters"]["options"][
        "queryReplacement"
    ]
    success_log["parameters"]["options"]["queryReplacement"] = (
        success_replacements.replace(
            "JSON.stringify({\n    file_id:",
            "JSON.stringify({\n    execution_id: $execution.id,\n    file_id:",
        )
    )

    failure_log = node_by_name(nodes, "Log Failure")
    failure_replacements = failure_log["parameters"]["options"][
        "queryReplacement"
    ]
    failure_log["parameters"]["options"]["queryReplacement"] = (
        failure_replacements.replace(
            "JSON.stringify({\n    file_id:",
            "JSON.stringify({\n    execution_id: $execution.id,\n    file_id:",
        )
    )

    execution_summary = postgres_node(
        postgres_template,
        "Execution Summary",
        [-1712, 1632],
        SUMMARY_QUERY,
        "={{ [$execution.id] }}",
    )
    nodes.append(execution_summary)

    drive_search_succeeded = {
        "parameters": {
            "conditions": {
                "options": {
                    "caseSensitive": True,
                    "leftValue": "",
                    "typeValidation": "strict",
                    "version": 3,
                },
                "conditions": [
                    {
                        "id": stable_id("condition/drive-search-succeeded"),
                        "leftValue": "={{ !$json.drive_search_error }}",
                        "rightValue": True,
                        "operator": {
                            "type": "boolean",
                            "operation": "true",
                            "singleValue": True,
                        },
                    }
                ],
                "combinator": "and",
            },
            "options": {},
        },
        "id": stable_id("Drive Search Succeeded?"),
        "name": "Drive Search Succeeded?",
        "type": "n8n-nodes-base.if",
        "typeVersion": 2.3,
        "position": [-256, 2240],
    }
    nodes.append(drive_search_succeeded)

    # One explicit graph. Split-in-batches output 0 is "done" and output 1 is
    # the current loop item in the n8n version used by the existing workflow.
    connections = {
        "Manual Run": {"main": [[edge("Load Active Workspaces")]]},
        "Scheduled Run": {"main": [[edge("Load Active Workspaces")]]},
        "Load Active Workspaces": {"main": [[edge("Loop Over Workspaces")]]},
        "Loop Over Workspaces": {
            "main": [
                [edge("Execution Summary")],
                [edge("Find transcript files"), edge("Find summary files")],
            ]
        },
        "Find transcript files": {"main": [[edge("tag transcript files")]]},
        "Find summary files": {"main": [[edge("tag summary files")]]},
        "tag transcript files": {"main": [[edge("Merge intake", 0)]]},
        "tag summary files": {"main": [[edge("Merge intake", 1)]]},
        "Merge intake": {"main": [[edge("Sort Files Newest First")]]},
        "Sort Files Newest First": {"main": [[edge("Workspace Has Files?")]]},
        "Workspace Has Files?": {
            "main": [
                [edge("Limit Workspace Batch")],
                [edge("Loop Over Workspaces")],
            ]
        },
        "Limit Workspace Batch": {"main": [[edge("Loop Over Files")]]},
        "Loop Over Files": {
            "main": [[edge("Loop Over Workspaces")], [edge("Extract File Identity")]]
        },
        "Extract File Identity": {
            "main": [
                [edge("Drive Search Succeeded?")],
                [edge("Build Failure Record")],
            ]
        },
        "Drive Search Succeeded?": {
            "main": [
                [edge("Resolve Ingestion Manifest")],
                [edge("Build Failure Record")],
            ]
        },
        "Resolve Ingestion Manifest": {
            "main": [[edge("Download Drive File")], [edge("Build Failure Record")]]
        },
        "Download Drive File": {
            "main": [[edge("Extract Text")], [edge("Build Failure Record")]]
        },
        "Extract Text": {
            "main": [[edge("Prepare Content")], [edge("Build Failure Record")]]
        },
        "Prepare Content": {
            "main": [[edge("Create Embeddings")], [edge("Build Failure Record")]]
        },
        "Create Embeddings": {
            "main": [[edge("Format Embeddings")], [edge("Build Failure Record")]]
        },
        "Format Embeddings": {
            "main": [[edge("Upsert Video Summary")], [edge("Build Failure Record")]]
        },
        "Upsert Video Summary": {
            "main": [[edge("Write Transcript Chunks")], [edge("Build Failure Record")]]
        },
        "Write Transcript Chunks": {
            "main": [[edge("Record Drive Artifact")], [edge("Build Failure Record")]]
        },
        "Record Drive Artifact": {
            "main": [[edge("Move File to Completed")], [edge("Build Failure Record")]]
        },
        "Move File to Completed": {
            "main": [[edge("Log Success")], [edge("Build Failure Record")]]
        },
        "Log Success": {"main": [[edge("Loop Over Files")]]},
        "Build Failure Record": {"main": [[edge("Log Failure")]]},
        "Log Failure": {"main": [[edge("Loop Over Files")]]},
    }

    positions = {
        "Find transcript files": [-1712, 1840],
        "Find summary files": [-1712, 2080],
        "tag transcript files": [-1488, 1840],
        "tag summary files": [-1488, 2080],
        "Merge intake": [-1264, 1920],
        "Sort Files Newest First": [-1040, 1920],
        "Workspace Has Files?": [-928, 1920],
        "Limit Workspace Batch": [-816, 1920],
        "Loop Over Files": [-592, 1920],
        "Extract File Identity": [-368, 2112],
        "Drive Search Succeeded?": [-256, 2240],
        "Resolve Ingestion Manifest": [-144, 2112],
        "Download Drive File": [80, 2112],
        "Extract Text": [304, 2112],
        "Prepare Content": [528, 2112],
        "Create Embeddings": [752, 2112],
        "Format Embeddings": [976, 2112],
        "Upsert Video Summary": [1200, 2112],
        "Write Transcript Chunks": [1424, 2112],
        "Record Drive Artifact": [1648, 2112],
        "Move File to Completed": [1872, 2112],
        "Log Success": [2096, 2112],
        "Build Failure Record": [1200, 2448],
        "Log Failure": [1424, 2448],
        "Execution Summary": [-1712, 1632],
    }
    for node in nodes:
        if node["name"] in positions:
            node["position"] = positions[node["name"]]

    generated = {
        "name": OUTPUT_NAME,
        "nodes": nodes,
        "connections": connections,
        "pinData": {},
        "settings": {"executionOrder": "v1", "binaryMode": "separate"},
        "active": False,
        "tags": [],
    }
    output.write_text(
        json.dumps(generated, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.source, args.output)


if __name__ == "__main__":
    main()
