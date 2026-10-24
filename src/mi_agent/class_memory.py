from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import math
import os
import re
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mi_agent.workflow.models import Diagnosis, DynamicToolSpec

_SCHEMA_VERSION = 1
_TOKEN_SPLIT = re.compile(r"\s+")
_NUMBER_TOKEN = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_HEX_TOKEN = re.compile(r"^(?:0x)?[0-9a-f]{8,}$", re.IGNORECASE)
_PATH_TOKEN = re.compile(r"[/\\]")
_QUOTED_TOKEN = re.compile(r"^['`\"].*['`\"]$")
_ALNUM_DIGIT_TOKEN = re.compile(r"^[a-z_\-]*\d+[a-z_\-\d]*$", re.IGNORECASE)
_UUID_TOKEN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_PLACEHOLDER = "<*>"
_SQL_LINE_COMMENT = re.compile(r"--[^\n]*")
_SQL_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_SQL_WORD = re.compile(r"[a-z_][a-z0-9_]*", re.IGNORECASE)


@dataclass(slots=True)
class IncidentSignature:
    template_ids: list[str]
    embedding: list[float]
    diag_features: dict[str, float]
    slots: dict[str, str]


@dataclass(slots=True)
class ClassMatch:
    class_id: str
    signature: IncidentSignature
    s1: float
    s2: float
    margin: float
    created_new: bool


@dataclass(slots=True)
class FastPathDecision:
    eligible: bool
    gate_reason: str
    source: str
    class_id: str | None
    s1: float
    s2: float
    margin: float
    action_key: str | None
    candidate: DynamicToolSpec | None


@dataclass(slots=True)
class OutcomeUpdate:
    match: ClassMatch
    spec: DynamicToolSpec
    latency_ms: float
    success: bool
    source: str
    ts: float


@dataclass(slots=True)
class ClassMemoryConfig:
    store_path: Path
    join_threshold: float
    margin_delta: float
    tau_conf: float
    n_min: int
    cooldown_seconds: float
    embedding_dim: int
    min_similarity_for_id: float


def canonical_action_key(spec: DynamicToolSpec) -> str:
    params = spec.params if isinstance(spec.params, dict) else {}
    payload = {
        "effect_type": spec.operation,
        "target_kind": _target_kind(spec.operation, params),
        "normalized_arg_schema": _canonical_params_for_action(spec.operation, params),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


def dedupe_candidates_by_action_key(candidates: list[DynamicToolSpec]) -> list[DynamicToolSpec]:
    by_key: dict[str, DynamicToolSpec] = {}
    for candidate in candidates:
        key = canonical_action_key(candidate)
        if key not in by_key:
            by_key[key] = candidate
    return [by_key[key] for key in sorted(by_key)]


def _canonical_params_for_action(operation: str, params: dict[str, Any]) -> Any:
    op = operation.strip().lower()

    if op == "run_sql":
        sql_text = _extract_sql_text(params)
        return {
            "db_target": _canonical_target_value(
                params.get("db_path") or params.get("database_path")
            ),
            "sql_signature": _sql_signature(sql_text),
        }

    if op == "replace_file":
        source = params.get("source_path")
        if not isinstance(source, str):
            source = params.get("source")
        destination = params.get("destination_path")
        if not isinstance(destination, str):
            destination = params.get("target_path")
        return {
            "source": _canonical_target_value(source),
            "destination": _canonical_target_value(destination),
            "backup_current": bool(params.get("backup_current", False)),
        }

    if op == "request_backup_restore":
        backup_id = params.get("backup_id")
        if not isinstance(backup_id, str) or backup_id.strip() == "":
            backup_id = params.get("selection") or params.get("restore_point")
        return {
            "backup": _normalize_arg_schema(backup_id),
            "scope": _normalize_arg_schema(params.get("scope") or params.get("restore_scope")),
        }

    if op == "restart_service":
        return {"service_name": _normalize_arg_schema(params.get("service_name"))}

    return _normalize_arg_schema(params)


def _extract_sql_text(params: dict[str, Any]) -> str:
    sql = params.get("sql")
    if isinstance(sql, str) and sql.strip() != "":
        return sql
    statements = params.get("statements")
    if isinstance(statements, list):
        chunks = [str(item).strip() for item in statements if str(item).strip()]
        if chunks:
            return "; ".join(chunks)
    return ""


def _sql_signature(sql_text: str) -> list[str]:
    if not sql_text.strip():
        return ["<empty_sql>"]

    lowered = _SQL_BLOCK_COMMENT.sub(" ", sql_text)
    lowered = _SQL_LINE_COMMENT.sub(" ", lowered)
    statements = [" ".join(part.strip().lower().split()) for part in lowered.split(";")]

    signatures: list[str] = []
    for stmt in statements:
        if not stmt:
            continue
        if stmt in {"begin", "begin transaction", "commit", "rollback", "end"}:
            continue
        signatures.append(_sql_statement_signature(stmt))

    return signatures or ["<empty_sql>"]


def _sql_statement_signature(statement: str) -> str:
    tokens = _SQL_WORD.findall(statement)
    if not tokens:
        return "sql:other"

    words = [token.lower() for token in tokens]
    if len(words) >= 2 and words[0] == "create" and words[1] == "table":
        name = _sql_object_name(words, 2)
        shape = _sql_table_shape(statement)
        if shape is not None:
            return f"create_table:{name}:{shape}"
        return f"create_table:{name}"
    if len(words) >= 2 and words[0] == "drop" and words[1] == "table":
        name = _sql_object_name(words, 2)
        return f"drop_table:{name}"
    if len(words) >= 2 and words[0] == "alter" and words[1] == "table":
        name = _sql_object_name(words, 2)
        return f"alter_table:{name}"
    if words[0] in {"insert", "update", "delete"}:
        name = _sql_object_name(words, 1)
        return f"{words[0]}:{name}"
    if words[0] == "pragma":
        pragma = words[1] if len(words) > 1 else "unknown"
        return f"pragma:{pragma}"
    if len(words) > 1:
        return f"{words[0]}:{words[1]}"
    return f"{words[0]}:stmt"


def _sql_object_name(words: list[str], start_index: int) -> str:
    skip = {"if", "not", "exists", "temporary", "temp", "table", "into", "from", "set", "where"}
    for token in words[start_index:]:
        if token in skip:
            continue
        return token
    return "unknown"


def _sql_parenthesized_shape(statement: str) -> str | None:
    start = statement.find("(")
    end = statement.rfind(")")
    if start < 0 or end <= start:
        return None
    inner = statement[start + 1 : end]
    compact = " ".join(inner.split())
    compact = compact.replace(" ,", ",").replace(", ", ",")
    compact = compact.replace(" (", "(").replace(" )", ")")
    return compact


def _sql_table_shape(statement: str) -> str | None:
    start = statement.find("(")
    end = statement.rfind(")")
    if start < 0 or end <= start:
        return None

    inner = statement[start + 1 : end]
    parts = _split_top_level_csv(inner)
    columns: list[str] = []
    for part in parts:
        tokens = _SQL_WORD.findall(part)
        if not tokens:
            continue
        lowered = [item.lower() for item in tokens]
        head = lowered[0]
        if head in {"primary", "foreign", "unique", "constraint", "check"}:
            continue
        column_name = head
        column_type = "unknown"
        if len(lowered) > 1:
            column_type = lowered[1]
        columns.append(f"{column_name}:{column_type}")

    if not columns:
        return _sql_parenthesized_shape(statement)
    return ",".join(columns)


def _split_top_level_csv(value: str) -> list[str]:
    chunks: list[str] = []
    depth = 0
    current: list[str] = []
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")" and depth > 0:
            depth -= 1
        elif char == "," and depth == 0:
            part = "".join(current).strip()
            if part:
                chunks.append(part)
            current = []
            continue
        current.append(char)

    tail = "".join(current).strip()
    if tail:
        chunks.append(tail)
    return chunks


def _canonical_target_value(value: Any) -> str:
    if not isinstance(value, str):
        return "<none>"
    compact = " ".join(value.strip().split())
    if compact == "":
        return "<none>"
    if _is_variable_token(compact):
        return "<string_var>"
    if "/" in compact or "\\" in compact:
        name = os.path.basename(compact).lower()
        if name.endswith(".db"):
            return "*.db"
        if "." in name:
            return f"*.{name.rsplit('.', 1)[1]}"
        return "<path>"
    return compact.lower()


class ClassMemory:
    def __init__(self, config: ClassMemoryConfig) -> None:
        self._config = config
        self._lock_path = config.store_path.with_suffix(f"{config.store_path.suffix}.lock")
        self._state = self._load_state()

    def build_signature(
        self,
        *,
        diagnostics: dict[str, Any],
        diagnosis: Diagnosis,
        attempts: int,
    ) -> IncidentSignature:
        lines = _collect_normalization_lines(diagnostics, diagnosis)
        templates: list[str] = []
        slots: dict[str, str] = {}
        for line in lines:
            template_id, extracted_slots = _drain_template(line)
            templates.append(template_id)
            for key, value in extracted_slots.items():
                slots.setdefault(key, value)

        template_ids = sorted(set(templates))
        if not template_ids:
            template_ids = [self._default_template_id(diagnosis.failure_type)]

        embedding = _hashed_embedding(" | ".join(template_ids), dim=self._config.embedding_dim)
        features = {
            "attempts": float(max(0, attempts)),
            "has_last_error": 1.0 if bool(diagnostics.get("health_deep")) else 0.0,
            "template_count": float(len(template_ids)),
        }
        return IncidentSignature(
            template_ids=template_ids,
            embedding=embedding,
            diag_features=features,
            slots=slots,
        )

    def assign_class(self, signature: IncidentSignature) -> ClassMatch:
        classes = self._state.get("classes", {})
        best_id: str | None = None
        best_s1 = -1.0
        second = -1.0
        for class_id, record in classes.items():
            centroid = _as_float_vector(record.get("centroid"))
            if not centroid:
                continue
            score = _cosine(signature.embedding, centroid)
            if score > best_s1:
                second = best_s1
                best_s1 = score
                best_id = class_id
            elif score > second:
                second = score

        created_new = False
        if best_id is None or best_s1 < self._config.join_threshold:
            best_id = self._class_id_for(signature)
            if best_id not in classes:
                classes[best_id] = {
                    "class_id": best_id,
                    "schema_version": _SCHEMA_VERSION,
                    "created_ts": time.time(),
                    "updated_ts": time.time(),
                    "n_incidents": 0,
                    "templates": [],
                    "centroid": signature.embedding,
                    "actions": {},
                    "skill_anchor": None,
                }
                created_new = True
            best_s1 = 1.0 if best_s1 < self._config.min_similarity_for_id else best_s1

        margin = best_s1 - max(second, 0.0)
        record = classes[best_id]
        self._update_class_centroid(record, signature.embedding)
        record_templates = set(str(item) for item in record.get("templates", []))
        record_templates.update(signature.template_ids)
        record["templates"] = sorted(record_templates)
        record["updated_ts"] = time.time()
        self._save_state()
        return ClassMatch(
            class_id=best_id,
            signature=signature,
            s1=float(best_s1),
            s2=float(max(second, 0.0)),
            margin=float(margin),
            created_new=created_new,
        )

    def choose_fast_path(
        self,
        *,
        match: ClassMatch,
        failed_action_keys: set[str],
    ) -> FastPathDecision:
        classes = self._state.get("classes", {})
        record = classes.get(match.class_id)
        if not isinstance(record, dict):
            return FastPathDecision(
                eligible=False,
                gate_reason="class_missing",
                source="full_loop",
                class_id=None,
                s1=match.s1,
                s2=match.s2,
                margin=match.margin,
                action_key=None,
                candidate=None,
            )

        if match.s1 < self._config.join_threshold:
            return self._reject(match, "s1_below_threshold")
        if match.margin < self._config.margin_delta:
            return self._reject(match, "margin_below_delta")

        actions = record.get("actions", {})
        if not isinstance(actions, dict) or not actions:
            return self._reject(match, "no_actions")

        best_key: str | None = None
        best_lcb = -1.0
        best_stats: dict[str, Any] | None = None
        now = time.time()
        for key, stats_obj in actions.items():
            if not isinstance(key, str) or not isinstance(stats_obj, dict):
                continue
            if key in failed_action_keys:
                continue
            cooldown_until = float(stats_obj.get("cooldown_until_ts", 0.0) or 0.0)
            if cooldown_until > now:
                continue
            attempts = int(stats_obj.get("attempts", 0) or 0)
            if attempts < self._config.n_min:
                continue
            lcb = _beta_lcb(
                float(stats_obj.get("alpha", 1.0) or 1.0),
                float(stats_obj.get("beta", 1.0) or 1.0),
            )
            if lcb > best_lcb:
                best_lcb = lcb
                best_key = key
                best_stats = stats_obj

        if best_key is None or best_stats is None:
            return self._reject(match, "no_confident_action")
        if best_lcb < self._config.tau_conf:
            return self._reject(match, "lcb_below_tau")

        template_payload = best_stats.get("param_template")
        if not isinstance(template_payload, dict):
            return self._reject(match, "missing_param_template")
        candidate = self._instantiate_candidate(template_payload, match.signature.slots)
        if candidate is None:
            return self._reject(match, "template_instantiate_failed")

        return FastPathDecision(
            eligible=True,
            gate_reason="eligible",
            source="class_memory",
            class_id=match.class_id,
            s1=match.s1,
            s2=match.s2,
            margin=match.margin,
            action_key=best_key,
            candidate=candidate,
        )

    def action_stats(self, *, class_id: str, action_key: str) -> dict[str, Any] | None:
        classes = self._state.get("classes", {})
        record = classes.get(class_id)
        if not isinstance(record, dict):
            return None
        actions = record.get("actions", {})
        if not isinstance(actions, dict):
            return None
        stats = actions.get(action_key)
        if not isinstance(stats, dict):
            return None

        alpha = float(stats.get("alpha", 1.0) or 1.0)
        beta = float(stats.get("beta", 1.0) or 1.0)
        return {
            "attempts": int(stats.get("attempts", 0) or 0),
            "alpha": alpha,
            "beta": beta,
            "lcb": _beta_lcb(alpha, beta),
            "mean_latency": float(stats.get("mean_latency", 0.0) or 0.0),
            "last_ok_ts": stats.get("last_ok_ts"),
            "last_failure_ts": stats.get("last_failure_ts"),
            "cooldown_until_ts": float(stats.get("cooldown_until_ts", 0.0) or 0.0),
        }

    def record_outcome(
        self,
        *,
        match: ClassMatch,
        spec: DynamicToolSpec,
        latency_ms: float,
        success: bool,
        source: str,
        ts: float | None = None,
    ) -> str:
        update = OutcomeUpdate(
            match=match,
            spec=spec,
            latency_ms=latency_ms,
            success=success,
            source=source,
            ts=time.time() if ts is None else ts,
        )
        classes = self._state.get("classes", {})
        record = classes.get(match.class_id)
        if not isinstance(record, dict):
            return canonical_action_key(spec)
        action_key = self._apply_outcome(record, update)
        self._save_state()
        return action_key

    def _apply_outcome(self, record: dict[str, Any], update: OutcomeUpdate) -> str:
        action_key = canonical_action_key(update.spec)
        actions = record.setdefault("actions", {})
        if not isinstance(actions, dict):
            actions = {}
            record["actions"] = actions
        stats = actions.get(action_key)
        if not isinstance(stats, dict):
            stats = {
                "alpha": 1.0,
                "beta": 1.0,
                "attempts": 0,
                "mean_latency": 0.0,
                "last_ok_ts": None,
                "last_failure_ts": None,
                "cooldown_until_ts": 0.0,
                "param_template": self._param_template_payload(
                    update.spec, update.match.signature.slots
                ),
                "source": update.source,
            }
            actions[action_key] = stats

        attempts = int(stats.get("attempts", 0) or 0) + 1
        mean_latency = float(stats.get("mean_latency", 0.0) or 0.0)
        mean_latency = ((mean_latency * (attempts - 1)) + max(0.0, update.latency_ms)) / attempts
        stats["attempts"] = attempts
        stats["mean_latency"] = mean_latency
        if update.success:
            stats["alpha"] = float(stats.get("alpha", 1.0) or 1.0) + 1.0
            stats["last_ok_ts"] = update.ts
            stats["cooldown_until_ts"] = 0.0
            record["skill_anchor"] = action_key
        else:
            stats["beta"] = float(stats.get("beta", 1.0) or 1.0) + 1.0
            stats["last_failure_ts"] = update.ts
            stats["cooldown_until_ts"] = update.ts + self._config.cooldown_seconds

        stats["source"] = update.source
        record["updated_ts"] = update.ts
        return action_key

    def render_skills_section(
        self,
        *,
        class_id: str,
        pending: OutcomeUpdate | None = None,
    ) -> str | None:
        """Render the class's skills.md section.

        With `pending`, the section reflects that outcome as if it were already
        recorded, without mutating the store, so step 15 can publish a skill that
        includes the success step 16 is about to persist.
        """
        classes = self._state.get("classes", {})
        record = classes.get(class_id)
        if not isinstance(record, dict):
            return None
        if pending is not None:
            record = copy.deepcopy(record)
            self._apply_outcome(record, pending)
        actions_obj = record.get("actions", {})
        if not isinstance(actions_obj, dict) or not actions_obj:
            return None

        scored: list[tuple[str, float, dict[str, Any]]] = []
        for key, stats_obj in actions_obj.items():
            if not isinstance(key, str) or not isinstance(stats_obj, dict):
                continue
            lcb = _beta_lcb(
                float(stats_obj.get("alpha", 1.0) or 1.0),
                float(stats_obj.get("beta", 1.0) or 1.0),
            )
            scored.append((key, lcb, stats_obj))
        if not scored:
            return None
        scored.sort(key=lambda row: (row[1], row[0]), reverse=True)
        champion_key, champion_lcb, champion_stats = scored[0]
        runner_up_keys = [item[0] for item in scored[1:4]]

        example_templates = ", ".join(record.get("templates", [])[:6])
        section = [
            f"# Class {class_id}",
            "",
            f"- champion_action_key: {champion_key}",
            f"- champion_lcb: {champion_lcb:.4f}",
            f"- attempts: {int(champion_stats.get('attempts', 0) or 0)}",
            f"- mean_latency_ms: {float(champion_stats.get('mean_latency', 0.0) or 0.0):.2f}",
            f"- last_verified_ts: {champion_stats.get('last_ok_ts')}",
            f"- runner_ups: {', '.join(runner_up_keys) if runner_up_keys else 'none'}",
            f"- templates: {example_templates or 'none'}",
            "",
            "## Champion Template",
            json.dumps(champion_stats.get("param_template", {}), indent=2, sort_keys=True),
            "",
        ]
        return "\n".join(section)

    def class_counts(self) -> int:
        classes = self._state.get("classes", {})
        if not isinstance(classes, dict):
            return 0
        return len(classes)

    @staticmethod
    def upsert_skills_section(skills_path: Path, *, class_id: str, section_body: str) -> None:
        begin = f"<!-- class:{class_id} begin -->"
        end = f"<!-- class:{class_id} end -->"
        block = f"{begin}\n{section_body.rstrip()}\n{end}\n"
        lock_path = skills_path.with_suffix(f"{skills_path.suffix}.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(lock_path):
            if skills_path.exists():
                original = skills_path.read_text(encoding="utf-8")
            else:
                original = (
                    "# Recovery Skills Library\n\n"
                    "Auto-learned skills from successful recovery sessions.\n"
                )

            start = original.find(begin)
            stop = original.find(end)
            if start != -1 and stop != -1 and stop >= start:
                stop += len(end)
                if stop < len(original) and original[stop : stop + 1] == "\n":
                    stop += 1
                updated = original[:start] + block + original[stop:]
            else:
                sep = "\n" if original and not original.endswith("\n") else ""
                updated = f"{original}{sep}\n{block}" if original else block
            _atomic_write_text(skills_path, updated)

    def _load_state(self) -> dict[str, Any]:
        self._config.store_path.parent.mkdir(parents=True, exist_ok=True)
        if not self._config.store_path.exists():
            return self._empty_state()

        try:
            raw = self._config.store_path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            backup = self._config.store_path.with_suffix(
                f"{self._config.store_path.suffix}.corrupt-{int(time.time())}"
            )
            with suppress(OSError):
                self._config.store_path.replace(backup)
            return self._empty_state()

        if not isinstance(payload, dict):
            return self._empty_state()
        if int(payload.get("schema_version", 0) or 0) > _SCHEMA_VERSION:
            return self._empty_state()
        classes = payload.get("classes")
        if not isinstance(classes, dict):
            payload["classes"] = {}
        payload.setdefault("schema_version", _SCHEMA_VERSION)
        payload.setdefault("classes", {})
        payload.setdefault("updated_ts", time.time())
        return payload

    def _save_state(self) -> None:
        self._state["schema_version"] = _SCHEMA_VERSION
        self._state["updated_ts"] = time.time()
        self._config.store_path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self._lock_path):
            _atomic_write_text(
                self._config.store_path,
                json.dumps(self._state, indent=2, sort_keys=True),
            )

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "updated_ts": time.time(),
            "classes": {},
        }

    def _class_id_for(self, signature: IncidentSignature) -> str:
        base = "|".join(signature.template_ids)
        return hashlib.sha256(base.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _default_template_id(failure_type: str) -> str:
        return hashlib.sha256(failure_type.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _update_class_centroid(record: dict[str, Any], embedding: list[float]) -> None:
        centroid = _as_float_vector(record.get("centroid"))
        n = int(record.get("n_incidents", 0) or 0)
        if not centroid:
            record["centroid"] = embedding
            record["n_incidents"] = 1
            return
        if len(centroid) != len(embedding):
            record["centroid"] = embedding
            record["n_incidents"] = n + 1
            return
        updated = [
            ((value * n) + emb) / max(1, n + 1)
            for value, emb in zip(centroid, embedding, strict=False)
        ]
        record["centroid"] = _l2_normalize(updated)
        record["n_incidents"] = n + 1

    @staticmethod
    def _merge_class_records(target: dict[str, Any], source: dict[str, Any]) -> None:
        target_templates = set(str(item) for item in target.get("templates", []))
        source_templates = set(str(item) for item in source.get("templates", []))
        target["templates"] = sorted(target_templates | source_templates)

        target_actions = target.get("actions")
        source_actions = source.get("actions")
        if not isinstance(target_actions, dict):
            target_actions = {}
            target["actions"] = target_actions
        if isinstance(source_actions, dict):
            for key, value in source_actions.items():
                if key not in target_actions and isinstance(value, dict):
                    target_actions[key] = value

        target_n = int(target.get("n_incidents", 0) or 0)
        source_n = int(source.get("n_incidents", 0) or 0)
        target_centroid = _as_float_vector(target.get("centroid"))
        source_centroid = _as_float_vector(source.get("centroid"))
        if target_centroid and source_centroid and len(target_centroid) == len(source_centroid):
            denom = max(1, target_n + source_n)
            target["centroid"] = _l2_normalize(
                [
                    ((t * target_n) + (s * source_n)) / denom
                    for t, s in zip(target_centroid, source_centroid, strict=False)
                ]
            )
        elif source_centroid:
            target["centroid"] = source_centroid
        target["n_incidents"] = target_n + source_n

    def _reject(self, match: ClassMatch, reason: str) -> FastPathDecision:
        return FastPathDecision(
            eligible=False,
            gate_reason=reason,
            source="full_loop",
            class_id=match.class_id,
            s1=match.s1,
            s2=match.s2,
            margin=match.margin,
            action_key=None,
            candidate=None,
        )

    @staticmethod
    def _param_template_payload(spec: DynamicToolSpec, slots: dict[str, str]) -> dict[str, Any]:
        return {
            "name": spec.name,
            "rationale": spec.rationale,
            "operation": spec.operation,
            "params": _slotify_params(spec.params, slots),
        }

    @staticmethod
    def _instantiate_candidate(
        template_payload: dict[str, Any],
        slots: dict[str, str],
    ) -> DynamicToolSpec | None:
        try:
            name = str(template_payload.get("name") or "class_memory_action")
            rationale = str(template_payload.get("rationale") or "class memory fast path")
            operation = str(template_payload.get("operation") or "")
            params = template_payload.get("params")
            if not isinstance(params, dict):
                params = {}
            concrete_params = _instantiate_slots(params, slots)
            return DynamicToolSpec(
                name=name,
                rationale=rationale,
                operation=operation,
                params=concrete_params,
            )
        except Exception:
            return None


def _collect_normalization_lines(
    diagnostics: dict[str, Any],
    diagnosis: Diagnosis,
) -> list[str]:
    rows: list[str] = []
    rows.append(diagnosis.failure_type)

    health = diagnostics.get("health_deep")
    if isinstance(health, dict):
        rows.append(f"health_status:{int(health.get('status_code', 0) or 0)}")
        detail = health.get("detail")
        if isinstance(detail, dict):
            detail_error = detail.get("error")
            if detail_error is not None:
                rows.append(f"health_error:{detail_error}")
            status = detail.get("status")
            if isinstance(status, dict):
                widgets_present = status.get("widgets_table_present")
                if isinstance(widgets_present, bool):
                    rows.append(f"widgets_present:{widgets_present}")
                integrity = status.get("integrity")
                if isinstance(integrity, list) and integrity:
                    rows.append(f"integrity:{str(integrity[0]).lower()}")
        health_error = health.get("error")
        if health_error is not None:
            rows.append(f"health_error:{health_error}")

    log_tail = diagnostics.get("log_tail")
    if isinstance(log_tail, list):
        rows.extend(str(item) for item in log_tail[-3:])

    db_status = diagnostics.get("db_status")
    if isinstance(db_status, dict):
        db_error = db_status.get("error")
        if db_error is not None:
            rows.append(f"db_error:{db_error}")
        widgets_present = db_status.get("widgets_table_present")
        if isinstance(widgets_present, bool):
            rows.append(f"db_widgets_present:{widgets_present}")

    db_integrity = diagnostics.get("db_integrity")
    if isinstance(db_integrity, dict):
        integrity_rows = db_integrity.get("rows")
        if isinstance(integrity_rows, list) and integrity_rows:
            rows.append(f"db_integrity:{str(integrity_rows[0]).lower()}")

    return [row for row in rows if row.strip() != ""]


def _drain_template(line: str, *, depth: int = 8) -> tuple[str, dict[str, str]]:
    tokens = [token for token in _TOKEN_SPLIT.split(line.strip()) if token]
    if not tokens:
        tokens = ["<empty>"]
    masked: list[str] = []
    slots: dict[str, str] = {}
    slot_index = 0
    for token in tokens[:depth]:
        if _is_variable_token(token):
            slot_name = f"slot_{slot_index}"
            slot_index += 1
            slots[slot_name] = token.strip("'`\"")
            masked.append(_PLACEHOLDER)
        else:
            masked.append(token.lower())
    template = " ".join(masked)
    template_id = hashlib.sha256(template.encode("utf-8")).hexdigest()[:16]
    return template_id, slots


def _is_variable_token(token: str) -> bool:
    cleaned = token.strip()
    if cleaned == "":
        return False
    stripped = cleaned.strip("'`\".,:;()[]{}")
    if stripped == "":
        return False
    return bool(
        _NUMBER_TOKEN.match(stripped)
        or _HEX_TOKEN.match(stripped)
        or _UUID_TOKEN.match(stripped)
        or _ALNUM_DIGIT_TOKEN.match(stripped)
        or _PATH_TOKEN.search(stripped)
        or _QUOTED_TOKEN.match(cleaned)
    )


def _hashed_embedding(text: str, *, dim: int) -> list[float]:
    vector = [0.0 for _ in range(max(8, dim))]
    for raw_token in _TOKEN_SPLIT.split(text.lower()):
        token = raw_token.strip()
        if token == "":
            continue
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        bucket = int(digest[:8], 16) % len(vector)
        sign = 1.0 if (int(digest[8:16], 16) % 2 == 0) else -1.0
        vector[bucket] += sign
    return _l2_normalize(vector)


def _l2_normalize(values: list[float]) -> list[float]:
    norm = math.sqrt(sum(item * item for item in values))
    if norm == 0.0:
        return values
    return [item / norm for item in values]


def _as_float_vector(value: Any) -> list[float]:
    if not isinstance(value, list):
        return []
    output: list[float] = []
    for item in value:
        if isinstance(item, (int, float)):
            output.append(float(item))
    return output


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return float(sum(a * b for a, b in zip(left, right, strict=False)))


def _beta_lcb(alpha: float, beta: float, *, z: float = 0.5) -> float:
    total = alpha + beta
    if total <= 0:
        return 0.0
    mean = alpha / total
    variance = (alpha * beta) / (total * total * (total + 1.0))
    bound = mean - (z * math.sqrt(max(variance, 0.0)))
    return max(0.0, min(1.0, bound))


def _normalize_arg_schema(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_arg_schema(value[key])
            for key in sorted(value)
            if isinstance(key, str)
        }
    if isinstance(value, list):
        return [_normalize_arg_schema(item) for item in value]
    if isinstance(value, str):
        compact = " ".join(value.strip().split())
        if _is_variable_token(compact):
            return "<string_var>"
        if len(compact) > 180:
            compact = compact[:180]
        return compact.lower()
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, int):
        return "<int>"
    if isinstance(value, float):
        return "<float>"
    if value is None:
        return "<none>"
    return f"<{type(value).__name__}>"


def _target_kind(operation: str, params: dict[str, Any]) -> str:
    alias_groups = [
        ("db_path", ["db_path", "database_path"]),
        ("destination_path", ["destination_path", "target_path"]),
        ("source_path", ["source_path", "source"]),
        ("service_name", ["service_name"]),
        ("backup_id", ["backup_id", "selection", "restore_point"]),
    ]
    for canonical_key, aliases in alias_groups:
        value = None
        for key in aliases:
            raw = params.get(key)
            if isinstance(raw, str) and raw.strip() != "":
                value = raw
                break
        if isinstance(value, str) and value.strip() != "":
            if "/" in value or "\\" in value:
                return f"{canonical_key}:path"
            return f"{canonical_key}:{value.strip().lower()}"
    return f"{operation}:default"


def _slotify_params(value: Any, slots: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _slotify_params(item, slots) for key, item in value.items()}
    if isinstance(value, list):
        return [_slotify_params(item, slots) for item in value]
    if isinstance(value, str):
        updated = value
        ordered_slots = sorted(slots.items(), key=lambda row: len(row[1]), reverse=True)
        for name, raw in ordered_slots:
            if raw and raw in updated:
                updated = updated.replace(raw, f"{{{{{name}}}}}")
        return updated
    return value


def _instantiate_slots(value: Any, slots: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _instantiate_slots(item, slots) for key, item in value.items()}
    if isinstance(value, list):
        return [_instantiate_slots(item, slots) for item in value]
    if isinstance(value, str):
        updated = value
        for key, raw in slots.items():
            updated = updated.replace(f"{{{{{key}}}}}", raw)
        return updated
    return value


@contextmanager
def _exclusive_lock(path: Path) -> Any:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp-{os.getpid()}-{int(time.time() * 1_000_000)}")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(path)

