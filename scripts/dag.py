"""Validate the development DAG and enforce prerequisites when changing status."""
import argparse
import datetime
import json
from pathlib import Path

PATH = Path(__file__).resolve().parents[1] / "Docs" / "tasks.json"


def validate(data):
    nodes = data["nodes"]
    by_id = {n["id"]: n for n in nodes}
    if len(by_id) != len(nodes):
        raise ValueError("Duplicate task IDs")
    ordered, visiting, seen = [], set(), set()
    def visit(node):
        key = node["id"]
        if key in visiting:
            raise ValueError("Cycle at " + key)
        if key in seen:
            return
        visiting.add(key)
        for dependency in node["depends_on"]:
            if dependency not in by_id:
                raise ValueError("Missing dependency " + dependency)
            visit(by_id[dependency])
            if node["status"] in ("in_progress", "completed") and by_id[dependency]["status"] != "completed":
                raise ValueError(f"{key} started before {dependency} completed")
        if node["status"] not in ("pending", "in_progress", "completed", "deferred"):
            raise ValueError("Unknown status " + node["status"])
        visiting.remove(key)
        seen.add(key)
        ordered.append(key)
    for node in nodes:
        visit(node)
        if "subdag" in node:
            validate(node["subdag"])
            if node["status"] == "completed" and any(child["status"] != "completed" for child in node["subdag"]["nodes"]):
                raise ValueError(f"{node['id']} has unfinished subtasks")
    return ordered


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--task")
    parser.add_argument("--status", choices=["in_progress", "completed", "deferred"])
    parser.add_argument("--subtask", help="Update a node inside the selected task's subdag")
    parser.add_argument("--evidence")
    args = parser.parse_args()
    data = json.loads(PATH.read_text(encoding="utf-8-sig"))
    if args.task:
        if not args.status:
            parser.error("--task requires --status")
        node = next(n for n in data["nodes"] if n["id"] == args.task)
        if args.subtask:
            node = next(n for n in node["subdag"]["nodes"] if n["id"] == args.subtask)
        node["status"] = args.status
        node["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if args.evidence:
            node["evidence"] = args.evidence
    print(" -> ".join(validate(data)))
    if args.task:
        PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
