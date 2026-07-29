"""Build importable n8n workflows for the Drive/B2/Supabase ingestion handoff.

The repository's Flow 3/4/5 export is upgraded in place. The authenticated n8n
export supplied by the operator is read from --scheduled-source and written as
an importable workflow alongside this script's repository.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5


ROOT = Path(__file__).resolve().parents[1]
IMMEDIATE_PATH = ROOT / "n8n-flow3-summarize-to-drive.json"
SCHEDULED_OUTPUT = ROOT / "n8n-vp-drive-to-supabase.json"


def stable_id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"vp-ingestion/{label}"))


def node_by_name(workflow: dict, name: str) -> dict:
    for node in workflow["nodes"]:
        if node["name"] == name:
            return node
    raise KeyError(f"Missing node: {name}")


def edge(node: str, index: int = 0) -> dict:
    return {"node": node, "type": "main", "index": index}


def add_node(workflow: dict, *, name: str, node_type: str, position: list[int], parameters: dict) -> dict:
    type_version = {
        "n8n-nodes-base.postgres": 2.6,
        "n8n-nodes-base.webhook": 2.1,
        "n8n-nodes-base.if": 2.2,
        "n8n-nodes-base.code": 2,
    }.get(node_type, 2)
    node = {
        "parameters": parameters,
        "id": stable_id(f"node/{name}"),
        "name": name,
        "type": node_type,
        "typeVersion": type_version,
        "position": position,
    }
    workflow["nodes"].append(node)
    return node


def upgrade_immediate(workflow: dict) -> dict:
    workflow["name"] = "VP Ingestion API / Drive + Media Manifest"

    # Client destinations now come from the canonical tenant registry instead of
    # joining Drive folders by display-name casing.
    add_node(
        workflow,
        name="List Registered Clients",
        node_type="n8n-nodes-base.postgres",
        position=[-560, -520],
        parameters={
            "operation": "executeQuery",
            "query": (
                "select coalesce(json_agg(json_build_object(\n"
                "  'client_id', client_id,\n"
                "  'name', display_name,\n"
                "  'transcripts_folder_id', drive_transcripts_intake_folder_id,\n"
                "  'summaries_folder_id', drive_summaries_intake_folder_id,\n"
                "  'transcripts_completed_folder_id', drive_transcripts_completed_folder_id,\n"
                "  'summaries_completed_folder_id', drive_summaries_completed_folder_id\n"
                ") order by display_name), '[]'::json) as clients\n"
                "from public.clients_registry\n"
                "where status = 'active'\n"
                "  and drive_transcripts_intake_folder_id is not null\n"
                "  and drive_summaries_intake_folder_id is not null\n"
                "  and drive_transcripts_completed_folder_id is not null\n"
                "  and drive_summaries_completed_folder_id is not null;"
            ),
            "options": {},
        },
    )
    workflow["connections"]["Webhook: List Clients"] = {
        "main": [[edge("List Registered Clients")]]
    }
    workflow["connections"]["List Registered Clients"] = {
        "main": [[edge("Respond: Client List")]]
    }
    for name in (
        "Search Transcripts Intake Folders",
        "Search Summaries Intake Folders",
        "Build Client List",
    ):
        workflow["connections"].pop(name, None)

    validate = node_by_name(workflow, "Validate Ingest Input")
    validate["parameters"]["jsCode"] = """const body = $input.first().json.body || {};

const required = [
  'filename', 'transcript', 'summary', 'client_id', 'idempotency_key',
  'source_video_id', 'transcripts_folder_id', 'summaries_folder_id'
];
for (const field of required) {
  if (!body[field] || (typeof body[field] === 'string' && !body[field].trim())) {
    throw new Error(`Missing required field: ${field}`);
  }
}

const baseName = body.filename.replace(/\.[^.]+$/, '');
return [{
  json: {
    filename: baseName,
    title: String(body.title || baseName).trim(),
    transcript: body.transcript,
    summary: body.summary,
    client_id: body.client_id,
    idempotency_key: body.idempotency_key,
    source_video_id: body.source_video_id,
    source_file: body.source_file || body.filename,
    b2_bucket: body.b2_bucket || null,
    b2_path: body.b2_path || null,
    thumbnail_b2_path: body.thumbnail_b2_path || null,
    transcripts_folder_id: body.transcripts_folder_id,
    summaries_folder_id: body.summaries_folder_id
  }
}];"""

    add_node(
        workflow,
        name="Check Completed Ingestion",
        node_type="n8n-nodes-base.postgres",
        position=[-80, 360],
        parameters={
            "operation": "executeQuery",
            "query": (
                "select\n"
                "  coalesce(m.status = 'completed', false) as already_completed,\n"
                "  coalesce(m.source_video_id, $2::text) as source_video_id,\n"
                "  m.transcript_drive_file_id as transcript_file_id,\n"
                "  m.transcript_url,\n"
                "  m.summary_drive_file_id as summary_file_id,\n"
                "  m.summary_url\n"
                "from (select 1) anchor\n"
                "left join lateral (\n"
                "  select * from public.ingestion_manifests\n"
                "  where idempotency_key = $1::text\n"
                "  limit 1\n"
                ") m on true;"
            ),
            "options": {
                "queryReplacement": "={{ [ $json.idempotency_key, $json.source_video_id ] }}"
            },
        },
    )
    add_node(
        workflow,
        name="Already Completed?",
        node_type="n8n-nodes-base.if",
        position=[140, 360],
        parameters={
            "conditions": {
                "options": {
                    "caseSensitive": True,
                    "leftValue": "",
                    "typeValidation": "strict",
                    "version": 3,
                },
                "conditions": [
                    {
                        "id": stable_id("condition/already-completed"),
                        "leftValue": "={{ $json.already_completed }}",
                        "rightValue": True,
                        "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                    }
                ],
                "combinator": "and",
            },
            "options": {},
        },
    )
    add_node(
        workflow,
        name="Reuse Completed Result",
        node_type="n8n-nodes-base.code",
        position=[380, 260],
        parameters={
            "jsCode": (
                "return [{ json: {\n"
                "  ok: true,\n"
                "  source_video_id: $json.source_video_id,\n"
                "  transcript_file_id: $json.transcript_file_id,\n"
                "  transcript_url: $json.transcript_url,\n"
                "  summary_file_id: $json.summary_file_id,\n"
                "  summary_url: $json.summary_url,\n"
                "  reused: true\n"
                "} }];"
            )
        },
    )

    prep = node_by_name(workflow, "Prep Summary Upload")
    prep["parameters"]["jsCode"] = """const upload = $input.first().json;
const validated = $('Validate Ingest Input').first().json;

return [{
  json: {
    ...validated,
    transcript_file_id: upload.id,
    transcript_url: upload.webViewLink || `https://drive.google.com/file/d/${upload.id}/view`
  }
}];"""

    add_node(
        workflow,
        name="Register Media Manifest",
        node_type="n8n-nodes-base.postgres",
        position=[980, 420],
        parameters={
            "operation": "executeQuery",
            "query": (
                "select * from public.admin_complete_ingestion(\n"
                "  $1::text, $2::uuid, $3::text, $4::text, $5::text,\n"
                "  nullif($6::text, ''), nullif($7::text, ''), nullif($8::text, ''),\n"
                "  $9::text, $10::text, $11::text, $12::text, $13::text, $14::text\n"
                ");"
            ),
            "options": {
                "queryReplacement": """={{ [
  $('Validate Ingest Input').first().json.idempotency_key,
  $('Validate Ingest Input').first().json.client_id,
  $('Validate Ingest Input').first().json.source_video_id,
  $('Validate Ingest Input').first().json.title,
  $('Validate Ingest Input').first().json.source_file,
  $('Validate Ingest Input').first().json.b2_bucket || '',
  $('Validate Ingest Input').first().json.b2_path || '',
  $('Validate Ingest Input').first().json.thumbnail_b2_path || '',
  $('Validate Ingest Input').first().json.transcripts_folder_id,
  $('Validate Ingest Input').first().json.summaries_folder_id,
  $('Prep Summary Upload').first().json.transcript_file_id,
  $('Prep Summary Upload').first().json.transcript_url,
  $('Upload Summary to Drive').first().json.id,
  $('Upload Summary to Drive').first().json.webViewLink || `https://drive.google.com/file/d/${$('Upload Summary to Drive').first().json.id}/view`
] }}"""
            },
        },
    )

    shape = node_by_name(workflow, "Shape Ingest Response")
    shape["parameters"]["jsCode"] = """const result = $input.first().json;
return [{
  json: {
    ok: true,
    source_video_id: result.source_video_id,
    transcript_file_id: result.transcript_file_id,
    transcript_url: result.transcript_url,
    summary_file_id: result.summary_file_id,
    summary_url: result.summary_url,
    reused: false
  }
}];"""

    workflow["connections"]["Validate Ingest Input"] = {
        "main": [[edge("Check Completed Ingestion")]]
    }
    workflow["connections"]["Check Completed Ingestion"] = {
        "main": [[edge("Already Completed?")]]
    }
    workflow["connections"]["Already Completed?"] = {
        "main": [
            [edge("Reuse Completed Result")],
            [edge("Upload Transcript to Drive")],
        ]
    }
    workflow["connections"]["Reuse Completed Result"] = {
        "main": [[edge("Respond: Ingest Result")]]
    }
    workflow["connections"]["Upload Summary to Drive"] = {
        "main": [[edge("Register Media Manifest")]]
    }
    workflow["connections"]["Register Media Manifest"] = {
        "main": [[edge("Shape Ingest Response")]]
    }
    return workflow


def upgrade_scheduled(workflow: dict) -> dict:
    workflow["name"] = "VP Drive to Supabase / Manifest Aware"
    # Credential references are installation-specific. Existing references remain
    # on original nodes; new Postgres nodes are intentionally unbound on import.

    add_node(
        workflow,
        name="Load Scheduled Intake Sources",
        node_type="n8n-nodes-base.postgres",
        position=[-940, -20],
        parameters={
            "operation": "executeQuery",
            "query": """select
  client_id,
  slug as client_slug,
  'transcripts'::text as ingestion_type,
  drive_transcripts_intake_folder_id as intake_folder_id,
  drive_transcripts_completed_folder_id as completed_folder_id
from public.clients_registry
where status = 'active'
  and drive_transcripts_intake_folder_id is not null
  and drive_transcripts_completed_folder_id is not null
union all
select
  client_id,
  slug as client_slug,
  'summaries'::text as ingestion_type,
  drive_summaries_intake_folder_id as intake_folder_id,
  drive_summaries_completed_folder_id as completed_folder_id
from public.clients_registry
where status = 'active'
  and drive_summaries_intake_folder_id is not null
  and drive_summaries_completed_folder_id is not null
order by client_slug, ingestion_type;""",
            "options": {},
        },
    )
    workflow["connections"]["Schedule Trigger"] = {
        "main": [[edge("Load Scheduled Intake Sources")]]
    }
    workflow["connections"]["Load Scheduled Intake Sources"] = {
        "main": [[edge("Search files and folders")]]
    }

    sort_files = node_by_name(workflow, "Sort files oldest first")
    sort_files["parameters"]["jsCode"] = """const inputItems = $input.all();
if (inputItems.length === 0) return [];

const scheduled = $('Load Scheduled Intake Sources').isExecuted;
const configs = scheduled
  ? $('Load Scheduled Intake Sources').all()
  : [$('Edit Fields1').first()];

return inputItems
  .map((item, index) => {
    const paired = Number(item.pairedItem?.item ?? 0);
    const config = (configs[paired] || configs[0]).json;
    return {
      file: item.json,
      config,
      originalIndex: index
    };
  })
  .sort((a, b) => new Date(a.file.createdTime) - new Date(b.file.createdTime))
  .map(({ file, config, originalIndex }) => ({
    json: {
      file_id: file.id,
      file_name: file.name,
      file_size: file.size,
      file_mime_type: file.mimeType,
      created_time: file.createdTime,
      client_id: config.client_id,
      client_slug: config.client_slug,
      ingestion_type: config.ingestion_type,
      intake_folder_id: config.intake_folder_id,
      completed_folder_id: config.completed_folder_id
    },
    pairedItem: { item: originalIndex }
  }));"""
    workflow["connections"]["Search files and folders"] = {
        "main": [[edge("Sort files oldest first")]]
    }
    workflow["connections"]["Sort files oldest first"] = {
        "main": [[edge("Limit")]]
    }
    workflow["connections"]["Limit"] = {
        "main": [[edge("Loop Over Items")]]
    }

    add_node(
        workflow,
        name="Webhook: Ingest Drive File",
        node_type="n8n-nodes-base.webhook",
        position=[-940, 620],
        parameters={
            "httpMethod": "POST",
            "path": "ingest-drive-file",
            "responseMode": "lastNode",
            "options": {},
        },
    )
    add_node(
        workflow,
        name="Normalize Webhook File",
        node_type="n8n-nodes-base.code",
        position=[-700, 620],
        parameters={
            "jsCode": """const body = $input.first().json.body || {};
const required = ['file_id', 'file_name', 'client_id', 'ingestion_type', 'completed_folder_id'];
for (const field of required) {
  if (!body[field]) throw new Error(`Missing required field: ${field}`);
}
return [{ json: {
  file_id: body.file_id,
  file_name: body.file_name,
  client_id: body.client_id,
  client_slug: body.client_slug || '',
  ingestion_type: String(body.ingestion_type).toLowerCase(),
  completed_folder_id: body.completed_folder_id,
  intake_folder_id: body.intake_folder_id || ''
} }];"""
        },
    )
    workflow["connections"]["Webhook: Ingest Drive File"] = {
        "main": [[edge("Normalize Webhook File")]]
    }
    workflow["connections"]["Normalize Webhook File"] = {
        "main": [[edge("Download file")]]
    }

    extract = node_by_name(workflow, "Extract Identity of File")
    extract["parameters"]["jsCode"] = """const fromWebhook = $('Normalize Webhook File').isExecuted;
const meta = fromWebhook
  ? $('Normalize Webhook File').first().json
  : $('Loop Over Items').item.json;
const rawText = String($input.first().json.data || '').trim();
if (!rawText) throw new Error(`Empty file: ${meta.file_name}`);

const fallbackId = meta.file_name
  .replace(/\.txt$/i, '')
  .replace(/[\s\-_]*(transcript|summary)\s*$/i, '')
  .trim();

return [{ json: {
  ...meta,
  source_video_id: fallbackId,
  title: fallbackId.replace(/[-_]+/g, ' ').trim(),
  source_file: `${fallbackId}.mp4`,
  raw_text: rawText,
  word_count_total: rawText.split(/\s+/).length
} }];"""

    add_node(
        workflow,
        name="Resolve Ingestion Manifest",
        node_type="n8n-nodes-base.postgres",
        position=[220, 80],
        parameters={
            "operation": "executeQuery",
            "query": """select
  coalesce(m.client_id, $1::uuid) as client_id,
  coalesce(m.source_video_id, $2::text) as source_video_id,
  coalesce(m.title, $3::text) as title,
  coalesce(m.source_file, $4::text) as source_file,
  m.b2_path,
  m.thumbnail_b2_path,
  case
    when m.transcript_drive_file_id = $5::text then 'transcripts'
    when m.summary_drive_file_id = $5::text then 'summaries'
    else lower($6::text)
  end as ingestion_type,
  coalesce(
    case
      when m.transcript_drive_file_id = $5::text then c.drive_transcripts_completed_folder_id
      when m.summary_drive_file_id = $5::text then c.drive_summaries_completed_folder_id
    end,
    $7::text
  ) as completed_folder_id,
  $5::text as file_id,
  $8::text as file_name,
  $9::text as raw_text,
  $10::int as word_count_total
from (select 1) anchor
left join lateral (
  select * from public.ingestion_manifests
  where transcript_drive_file_id = $5::text
     or summary_drive_file_id = $5::text
  limit 1
) m on true
left join public.clients_registry c on c.client_id = coalesce(m.client_id, $1::uuid);""",
            "options": {
                "queryReplacement": """={{ [
  $json.client_id,
  $json.source_video_id,
  $json.title,
  $json.source_file,
  $json.file_id,
  $json.ingestion_type,
  $json.completed_folder_id,
  $json.file_name,
  $json.raw_text,
  $json.word_count_total
] }}"""
            },
        },
    )
    workflow["connections"]["Extract Identity of File"] = {
        "main": [[edge("Resolve Ingestion Manifest")]]
    }
    workflow["connections"]["Resolve Ingestion Manifest"] = {
        "main": [[edge("Filter Ingestion Type")]]
    }

    http = node_by_name(workflow, "HTTP Request")
    http["parameters"]["jsonBody"] = """={
  "model": "text-embedding-3-small",
  "input": {{ JSON.stringify(
    $('Resolve Ingestion Manifest').item.json.ingestion_type === 'transcripts'
      ? $('Chunk transcripts').item.json.embedding_inputs
      : $('Prepare summaries').item.json.embedding_inputs
  ) }},
  "encoding_format": "float"
}"""

    fmt = node_by_name(workflow, "format embeddings")
    fmt["parameters"]["jsCode"] = """const apiResponse = $input.first().json;
const identity = $('Resolve Ingestion Manifest').item.json;
const parent = identity.ingestion_type === 'transcripts'
  ? $('Chunk transcripts').item.json
  : $('Prepare summaries').item.json;
const embeddings = apiResponse?.data;
if (!Array.isArray(embeddings) || embeddings.length === 0) {
  throw new Error('Missing embeddings in OpenAI response');
}
const fmt = arr => arr ? `[${arr.join(',')}]` : null;
let summary_embedding = null;
let chunkEmbeddings = embeddings;
if (parent.ingestion_type === 'summaries') {
  summary_embedding = fmt(embeddings[0].embedding);
  chunkEmbeddings = [];
}
let chunks_with_embeddings = [];
if (parent.chunks?.length) {
  if (chunkEmbeddings.length !== parent.chunks.length) {
    throw new Error(`Chunk embedding mismatch: got ${chunkEmbeddings.length}, expected ${parent.chunks.length}`);
  }
  chunks_with_embeddings = parent.chunks.map((chunk, index) => ({
    ...chunk,
    embedding: fmt(chunkEmbeddings[index].embedding)
  }));
}
return [{ json: {
  client_id: parent.client_id,
  source_video_id: parent.source_video_id,
  title: parent.title,
  source_file: parent.source_file,
  b2_path: parent.b2_path || null,
  thumbnail_b2_path: parent.thumbnail_b2_path || null,
  has_timestamps: parent.has_timestamps,
  summary_text: parent.summary_text || null,
  summary_embedding,
  topics: parent.topics?.length ? parent.topics : null,
  is_auto_summary: parent.is_auto_summary,
  chunks: chunks_with_embeddings,
  file_id: parent.file_id,
  file_name: parent.file_name,
  completed_folder_id: parent.completed_folder_id,
  ingestion_type: parent.ingestion_type
} }];"""

    upsert = node_by_name(workflow, "upsert video")
    upsert["parameters"]["query"] = """insert into public.video_summaries
  (client_id, source_video_id, title, source_file, has_timestamps,
   summary_text, summary_embedding, topics, b2_path, thumbnail_b2_path)
values
  ($1, $2, $3, $4, $5, nullif($6, 'null'), nullif($7, 'null')::vector,
   nullif($8, 'null')::text[], nullif($9, 'null'), nullif($10, 'null'))
on conflict (client_id, source_video_id) do update set
  title = excluded.title,
  source_file = coalesce(excluded.source_file, video_summaries.source_file),
  has_timestamps = case when $11::text = 'transcripts'
                        then excluded.has_timestamps
                        else video_summaries.has_timestamps end,
  summary_text = coalesce(excluded.summary_text, video_summaries.summary_text),
  summary_embedding = coalesce(excluded.summary_embedding, video_summaries.summary_embedding),
  topics = case when excluded.topics is not null and array_length(excluded.topics, 1) > 0
                then excluded.topics else video_summaries.topics end,
  b2_path = coalesce(excluded.b2_path, video_summaries.b2_path),
  thumbnail_b2_path = coalesce(excluded.thumbnail_b2_path, video_summaries.thumbnail_b2_path),
  updated_at = now()
returning summary_id;"""
    upsert["parameters"]["options"]["queryReplacement"] = """={{ [
  $json.client_id,
  $json.source_video_id,
  $json.title,
  $json.source_file,
  $json.has_timestamps,
  $json.summary_text,
  $json.summary_embedding,
  $json.topics,
  $json.b2_path,
  $json.thumbnail_b2_path,
  $json.ingestion_type
] }}"""

    add_node(
        workflow,
        name="Reset Transcript Chunks",
        node_type="n8n-nodes-base.postgres",
        position=[1160, 80],
        parameters={
            "operation": "executeQuery",
            "query": """delete from public.transcript_segments
where client_id = $1::uuid
  and source_video_id = $2::text
  and $3::text = 'transcripts';
select summary_id
from public.video_summaries
where client_id = $1::uuid and source_video_id = $2::text;""",
            "options": {
                "queryReplacement": """={{ [
  $('format embeddings').item.json.client_id,
  $('format embeddings').item.json.source_video_id,
  $('format embeddings').item.json.ingestion_type
] }}"""
            },
        },
    )
    workflow["connections"]["upsert video"] = {
        "main": [[edge("Reset Transcript Chunks")]]
    }
    workflow["connections"]["Reset Transcript Chunks"] = {
        "main": [[edge("If")]]
    }
    workflow["connections"]["If"] = {
        "main": [
            [edge("split chunks")],
            [edge("Move file")],
        ]
    }
    workflow["connections"]["split chunks"] = {
        "main": [[edge("Loop Over Items1")]]
    }

    move = node_by_name(workflow, "Move file")
    move["parameters"]["folderId"]["value"] = (
        "={{ $('format embeddings').item.json.completed_folder_id }}"
    )
    add_node(
        workflow,
        name="Continue Scheduled Loop?",
        node_type="n8n-nodes-base.if",
        position=[1660, 80],
        parameters={
            "conditions": {
                "options": {
                    "caseSensitive": True,
                    "leftValue": "",
                    "typeValidation": "strict",
                    "version": 3,
                },
                "conditions": [
                    {
                        "id": stable_id("condition/continue-scheduled-loop"),
                        "leftValue": "={{ !$('Normalize Webhook File').isExecuted }}",
                        "rightValue": True,
                        "operator": {"type": "boolean", "operation": "true", "singleValue": True},
                    }
                ],
                "combinator": "and",
            },
            "options": {},
        },
    )
    workflow["connections"]["Move file"] = {
        "main": [[edge("Continue Scheduled Loop?")]]
    }
    workflow["connections"]["Continue Scheduled Loop?"] = {
        "main": [[edge("Loop Over Items")], []]
    }
    return workflow


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheduled-source", required=True, type=Path)
    args = parser.parse_args()

    immediate = json.loads(IMMEDIATE_PATH.read_text(encoding="utf-8-sig"))
    if not any(node["name"] == "List Registered Clients" for node in immediate["nodes"]):
        immediate = upgrade_immediate(immediate)
    for node in immediate["nodes"]:
        if node["name"] in {
            "List Registered Clients",
            "Check Completed Ingestion",
            "Register Media Manifest",
        }:
            node["typeVersion"] = 2.6
        elif node["name"] == "Already Completed?":
            node["typeVersion"] = 2.2
    IMMEDIATE_PATH.write_text(
        json.dumps(immediate, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    scheduled = json.loads(args.scheduled_source.read_text(encoding="utf-8-sig"))
    upgraded_scheduled = upgrade_scheduled(scheduled)
    SCHEDULED_OUTPUT.write_text(
        json.dumps(upgraded_scheduled, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
