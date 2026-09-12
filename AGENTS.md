# MADSearcher development

Read `Docs/技术方案.md`, `Docs/设计文档.md`, and `Docs/tasks.json` before continuing development. Preserve the user's original `Docs/需求文档.md`.

Only start a DAG node after all dependencies have status `completed`. Update state using `python scripts/dag.py --task <id> --status <state> --evidence <reason>`. Do not mark real model or Adobe validation passed without running it.

User direction on 2026-09-10: finish code, build/package, and write usage documentation, but do not run new or repeat tests until the user supplies further test instructions. Historical test reports remain evidence only for the code and fixtures they actually exercised. The `acceptance` DAG node is deferred; do not treat it as passed or start it autonomously under this instruction.

The WPF frontend and Python worker communicate through UTF-8 NDJSON. stdout is protocol only. The user now requests plaintext API key persistence in the ignored local settings file; CLI may read that setting, with an environment variable override. Never log keys or put them in command arguments or version control. Original video files are read-only; all generated data stays in the repository by default.

Local feedback lives in `Docs/优化及bug修复.md`: do not track or upload this file. Mark each implemented item 已完成, and leave 待定 items unimplemented. Active tasks use captured settings and form inputs; browsing and editing should remain available while only new task actions are disabled. Do not add a task queue.

Build: `dotnet build src/MADSearcher.Desktop/MADSearcher.Desktop.csproj -c Release`. Test: `.venv/Scripts/python.exe -m unittest discover -s tests -v`. Reproducible project issues belong in `.agents/skills/` with narrow triggers and observed fixes.
