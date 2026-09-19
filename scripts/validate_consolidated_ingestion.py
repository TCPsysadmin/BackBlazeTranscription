"""Static checks for the generated consolidated n8n ingestion workflow."""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
from collections import deque
from pathlib import Path

TRIGGER_TYPES = {
    "n8n-nodes-base.manualTrigger",
    "n8n-nodes-base.scheduleTrigger",
}
EXTERNAL_NODE_TYPES = {
    "n8n-nodes-base.googleDrive",
    "n8n-nodes-base.httpRequest",
    "n8n-nodes-base.postgres",
}


def validate(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")
    workflow = json.loads(raw)
    nodes = workflow["nodes"]
    connections = workflow["connections"]
    names = {node["name"] for node in nodes}

    assert workflow.get("active") is False, "generated workflow must be inactive"
    assert len(names) == len(nodes), "node names must be unique"
    assert "ingestion_sources" not in raw, "must use canonical clients_registry"
    assert "collaborative-process" not in raw, "must not hard-code a workspace"
    assert "test-company" not in raw, "must not hard-code a workspace"
    drive_search_nodes = {
        node["name"]: node for node in nodes if node["name"].startswith("Find ")
    }
    for name in ("Find transcript files", "Find summary files"):
        folder_expression = drive_search_nodes[name]["parameters"]["filter"][
            "folderId"
        ]["value"]
        assert folder_expression.startswith("={{"), (
            f"{name} has malformed folder expression: {folder_expression}"
        )
        assert "$('Loop Over Workspaces').item.json." in folder_expression, (
            f"{name} must read the active workspace-loop item"
        )

    referenced_nodes = set(re.findall(r"\$\('([^']+)'\)", raw))
    assert not referenced_nodes - names, (
        f"expressions reference missing nodes: {sorted(referenced_nodes - names)}"
    )

    targets: set[str] = set()
    graph: dict[str, list[str]] = {name: [] for name in names}
    for source, outputs in connections.items():
        assert source in names, f"connection source does not exist: {source}"
        for groups in outputs.values():
            for branch in groups:
                for item in branch:
                    target = item["node"]
                    assert target in names, (
                        f"connection target does not exist: {target}"
                    )
                    targets.add(target)
                    graph[source].append(target)

    triggers = [node for node in nodes if node["type"] in TRIGGER_TYPES]
    assert {node["name"] for node in triggers} == {"Manual Run", "Scheduled Run"}
    for trigger in triggers:
        assert graph[trigger["name"]], f"trigger is disconnected: {trigger['name']}"

    reachable: set[str] = set()
    queue = deque(node["name"] for node in triggers)
    while queue:
        name = queue.popleft()
        if name in reachable:
            continue
        reachable.add(name)
        queue.extend(graph[name])
    assert reachable == names, f"unreachable nodes: {sorted(names - reachable)}"

    missing_credentials = [
        node["name"]
        for node in nodes
        if node["type"] in EXTERNAL_NODE_TYPES and not node.get("credentials")
    ]
    assert not missing_credentials, (
        f"credential references missing: {missing_credentials}"
    )

    for node in nodes:
        if node["type"] != "n8n-nodes-base.code":
            continue
        code = node.get("parameters", {}).get("jsCode", "")
        encoded = base64.b64encode(code.encode()).decode()
        result = subprocess.run(
            [
                "node",
                "-e",
                "new Function(Buffer.from(process.argv[1], 'base64').toString('utf8'));",
                encoded,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"invalid JavaScript in {node['name']}: {result.stderr.strip()}"
        )

    required = {
        "Load Active Workspaces",
        "Loop Over Workspaces",
        "Resolve Ingestion Manifest",
        "Record Drive Artifact",
        "Build Failure Record",
        "Log Failure",
        "Execution Summary",
        "Drive Search Succeeded?",
    }
    assert required <= names, f"required nodes missing: {sorted(required - names)}"
    assert "public.clients_registry" in raw
    assert "public.ingestion_manifests" in raw
    assert "public.video_summaries" in raw
    assert "public.transcript_segments" in raw

    print(
        f"OK: {workflow['name']} ({len(nodes)} nodes, "
        f"{len(connections)} connected sources)"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workflow", type=Path)
    args = parser.parse_args()
    validate(args.workflow)


if __name__ == "__main__":
    main()
