"""Flushed, secret-free index timings with inclusive and exclusive stage totals."""
import json
import time
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone

from .errors import UserError

LOG_SCHEMA_VERSION = 2


class IndexProfile:
    def __init__(self, workspace, video_id, secret=""):
        self.started = time.perf_counter()
        self.secret = str(secret or "").strip()
        self.video_id = video_id
        self.run_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:10]
        directory = workspace / "logs" / "index"
        self.path = directory / (self.run_id + ".jsonl")
        self.summary_path = directory / (self.run_id + ".summary.json")
        self.stages, self.stack, self.counters, self.usage = {}, [], {}, {}
        self.write_failed = False
        self.summary = {}
        self.last_validation_failure = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self.stream = self.path.open("x", encoding="utf-8", buffering=1)
        except OSError:
            raise UserError("无法创建索引耗时日志，请检查工作区logs目录权限和磁盘空间。")
        self.event("run_start", video_id=video_id, log_schema_version=LOG_SCHEMA_VERSION)

    def _safe(self, value):
        if isinstance(value, str):
            return value.replace(self.secret, "[已隐藏密钥]") if self.secret else value
        if isinstance(value, dict):
            return {self._safe(k): self._safe(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._safe(v) for v in value]
        return value

    def _context(self):
        context = {}
        for block in self.stack:
            context.update(block["metadata"])
        return context

    def event(self, event, **metadata):
        item = {"utc": datetime.now(timezone.utc).isoformat(), "run_id": self.run_id,
                "event": event, "elapsed_ms": round((time.perf_counter() - self.started) * 1000, 3),
                **self._context(), **metadata}
        try:
            self.stream.write(json.dumps(self._safe(item), ensure_ascii=False, allow_nan=False) + "\n")
            self.stream.flush()
        except OSError:
            self.write_failed = True

    def count(self, name, amount=1):
        self.counters[name] = self.counters.get(name, 0) + amount

    def validation_failure(self, **metadata):
        # Keep bounded, structured diagnostics available even after stage scopes unwind.
        self.last_validation_failure = self._safe({**self._context(), **metadata})
        self.event("validation_failed", **self.last_validation_failure)

    @contextmanager
    def span(self, stage, **metadata):
        block = {"start": time.perf_counter(), "children": 0.0, "metadata": metadata}
        self.stack.append(block)
        self.event("stage_start", stage=stage, depth=len(self.stack) - 1, **metadata)
        status, error = "completed", {}
        try:
            yield
        except BaseException as exc:
            status = "failed"
            error = {"error_type": type(exc).__name__, "error_code": getattr(exc, "code", None)}
            raise
        finally:
            elapsed = time.perf_counter() - block["start"]
            own = max(0.0, elapsed - block["children"])
            self.stack.pop()
            if self.stack:
                self.stack[-1]["children"] += elapsed
            total = self.stages.setdefault(stage, {"count": 0, "failed": 0, "inclusive_ms": 0.0, "self_ms": 0.0})
            total["count"] += 1
            total["failed"] += status != "completed"
            total["inclusive_ms"] += elapsed * 1000
            total["self_ms"] += own * 1000
            self.event("stage_end", stage=stage, status=status, duration_ms=round(elapsed * 1000, 3),
                       self_ms=round(own * 1000, 3), **metadata, **error)

    def api_usage(self, endpoint, model, response):
        usage = response.get("usage")
        values = {}
        if isinstance(usage, dict):
            for target, source in (("input_tokens", "input_tokens" if endpoint == "responses" else "prompt_tokens"),
                                   ("output_tokens", "output_tokens"), ("total_tokens", "total_tokens")):
                value = usage.get(source)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    values[target] = value
            detail = usage.get("input_tokens_details") or {}
            value = detail.get("cached_tokens") if isinstance(detail, dict) else None
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                values["cached_tokens"] = value
        self.event("api_usage", endpoint=endpoint, model=model, available=bool(values), **values)
        totals = self.usage.setdefault(endpoint + ":" + model, {"requests": 0, "usage_missing": 0})
        totals["requests"] += 1
        totals["usage_missing"] += not bool(values)
        for key, value in values.items():
            totals[key] = totals.get(key, 0) + value

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, traceback):
        status = "completed" if kind is None else "cancelled" if issubclass(kind, KeyboardInterrupt) else "failed"
        self.summary = {"run_id": self.run_id, "video_id": self.video_id, "status": status, "log_schema_version": LOG_SCHEMA_VERSION,
                        "total_ms": round((time.perf_counter() - self.started) * 1000, 3),
                        "stages": [{"stage": name, **{k: round(v, 3) if isinstance(v, float) else v for k, v in data.items()}}
                                   for name, data in sorted(self.stages.items(), key=lambda pair: -pair[1]["self_ms"])],
                        "counters": self.counters, "api_usage": self.usage,
                        "error_type": kind.__name__ if kind else None, "log_incomplete": self.write_failed,
                        "validation_failure": self.last_validation_failure,
                        "note": "inclusive_ms includes child spans; sum self_ms instead. Missing run_end means interruption."}
        self.summary = self._safe(self.summary)
        self.event("run_end", **self.summary)
        try:
            self.stream.close()
            self.summary["log_incomplete"] = self.write_failed
            self.summary_path.write_text(json.dumps(self._safe(self.summary), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            self.write_failed = True
        return False


def measure(profile, name, **metadata):
    return profile.span(name, **metadata) if profile else nullcontext()
