"""课堂双语术语与翻译记忆服务。

数据关系概览::

    material (课件/讲义/练习题, 带学科)
      └── segment (源文片段, 带语境)
            ├── derivation (拆段/合段留痕: 父片段 -> 子片段)
            ├── translation (教师确认译文, 按片段 + 学科 + 语境存档)
            └── term_decision / context_exception (术语决定, 按学科 + 语境)

关键约束：

* 翻译记忆按 ``源文片段 + 学科 + 语境`` 查找，学科不同不得共用译文。
* 术语决定按 ``术语 + 学科 + 语境`` 版本化。首次确认建立基线；之后的修订
  才会触发复核。教师为某个语境确认的例外 (``context_exception``) 永远优先，
  后续术语修订不会覆盖它。
* 术语修订会找出受影响的译文与材料，生成待复核项；旧译文不会继续被推荐。
  未处理复核前默认禁止发布新材料版本。
* 已发布材料以不可变快照保存在 ``material_versions`` 中，修订不会改写历史版本；
  快照中片段若被拆/合，可通过 ``explain_version_references`` 查到当前去向。
* 拆段/合段不删除父片段：父片段标记为 split/merged 并记录去向，引用可追溯。
"""

from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime, timezone

from .text import normalize_text


MATERIAL_STATUSES = ("draft", "published")
TRANSLATION_CURRENT = "current"
TRANSLATION_NEEDS_REVIEW = "needs_review"
TRANSLATION_SUPERSEDED = "superseded"
SEGMENT_ACTIVE = "active"
SEGMENT_SPLIT = "split"
SEGMENT_MERGED = "merged"
DECISION_CONFIRMED = "confirmed"
DECISION_SUPERSEDED = "superseded"


def _now(at=None):
    if at:
        return str(at)
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _key(value):
    """术语/语境的比较键。"""
    return normalize_text(value or "").casefold()


def _require(value, message):
    if value is None or not str(value).strip():
        raise ValueError(message)
    return str(value).strip()


def _new_id(collection, prefix, existing):
    while True:
        candidate = f"{prefix}-{uuid.uuid4().hex[:12]}"
        if candidate not in existing:
            return candidate


def term_occurrences(term, text):
    """返回术语在规范化文本中的所有出现区间 ``(start, end)``。"""
    normalized_term = normalize_text(term)
    normalized_text = normalize_text(text)
    if not normalized_term or not normalized_text:
        return []
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 '’./+-]*", normalized_term):
        pattern = r"(?<![A-Za-z0-9])" + re.escape(normalized_term) + r"(?![A-Za-z0-9])"
        return [
            (match.start(), match.end())
            for match in re.finditer(pattern, normalized_text, flags=re.IGNORECASE)
        ]
    haystack = normalized_text.casefold()
    needle = normalized_term.casefold()
    spans = []
    start = haystack.find(needle)
    while start != -1:
        spans.append((start, start + len(needle)))
        start = haystack.find(needle, start + 1)
    return spans


def term_occurs(term, text):
    """判断术语是否出现在源文中。

    中文按规范化后的子串匹配；拉丁字母术语按词边界匹配，避免 ``art`` 命中
    ``part`` 之类的误判。
    """
    normalized_term = normalize_text(term)
    normalized_text = normalize_text(text)
    if not normalized_term or not normalized_text:
        return False
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 '’./+-]*", normalized_term):
        pattern = r"(?<![A-Za-z0-9])" + re.escape(normalized_term) + r"(?![A-Za-z0-9])"
        return re.search(pattern, normalized_text, flags=re.IGNORECASE) is not None
    return normalized_term.casefold() in normalized_text.casefold()


class TranslationMemory:
    """术语决定、翻译记忆与材料历史的一致性服务。"""

    def __init__(self, repository):
        # 允许直接传路径，便于脚本和测试使用。
        if isinstance(repository, (str, bytes)) or hasattr(repository, "__fspath__"):
            from .repository import DocumentRepository

            repository = DocumentRepository(repository)
        self.repository = repository

    # ------------------------------------------------------------------
    # 存储辅助
    # ------------------------------------------------------------------
    def _read(self):
        return self.repository.read_document()

    def _write(self, document):
        self.repository.write_document(document)

    @staticmethod
    def _collection(document, name):
        return document.setdefault(name, [])

    @staticmethod
    def _find(document, collection, field, value, label):
        for row in document.get(collection, []):
            if row.get(field) == value:
                return row
        raise ValueError(f"找不到{label}: {value}")

    @staticmethod
    def _material(document, material_id):
        return TranslationMemory._find(document, "materials", "material_id", material_id, "材料")

    @staticmethod
    def _segment(document, segment_id):
        return TranslationMemory._find(document, "segments", "segment_id", segment_id, "源文片段")

    @staticmethod
    def _current_translation(document, segment_id):
        """该片段当前应采用的译文。

        优先返回 ``current``；若只有待复核译文，也返回它并让调用方根据
        ``status`` 判断，避免把过期译文误当作“没有译文”而静默发布。
        """
        candidates = [
            row
            for row in document.get("translations", [])
            if row.get("segment_id") == segment_id
        ]
        for translation in candidates:
            if translation.get("status") == TRANSLATION_CURRENT:
                return translation
        candidates = [row for row in candidates if row.get("status") == TRANSLATION_NEEDS_REVIEW]
        candidates.sort(key=lambda row: row.get("confirmed_at", ""), reverse=True)
        return candidates[0] if candidates else None

    @staticmethod
    def _material_revision(material):
        material["revision"] = int(material.get("revision", 0)) + 1
        material["updated_at"] = _now()

    # ------------------------------------------------------------------
    # 材料与源文片段
    # ------------------------------------------------------------------
    def create_material(
        self,
        subject,
        title="",
        material_type="courseware",
        material_id=None,
        at=None,
    ):
        subject = _require(subject, "材料缺少学科")
        document = self._read()
        materials = self._collection(document, "materials")
        if material_id is None:
            material_id = _new_id(
                "materials", "material", {row.get("material_id") for row in materials}
            )
        else:
            material_id = _require(material_id, "材料缺少标识")
            if any(row.get("material_id") == material_id for row in materials):
                raise ValueError(f"材料标识已存在: {material_id}")
        timestamp = _now(at)
        material = {
            "material_id": material_id,
            "title": str(title or "").strip(),
            "subject": subject,
            "material_type": str(material_type or "courseware").strip() or "courseware",
            "status": "draft",
            "revision": 0,
            "published_revision": 0,
            "current_version": 0,
            "segment_ids": [],
            "created_at": timestamp,
            "updated_at": timestamp,
        }
        materials.append(material)
        self._write(document)
        return self._material_view(material)

    def _material_view(self, material):
        view = copy.deepcopy(material)
        view["has_unpublished_changes"] = (
            int(material.get("revision", 0)) > int(material.get("published_revision", 0))
        )
        return view

    def get_material(self, material_id):
        return self._material_view(self._material(self._read(), material_id))

    def list_materials(self, subject=None):
        materials = self._read().get("materials", [])
        if subject is not None:
            materials = [row for row in materials if row.get("subject") == subject]
        return [self._material_view(row) for row in materials]

    def add_segment(self, material_id, text, context="", segment_id=None, order=None, at=None):
        text = normalize_text(_require(text, "源文片段缺少正文"))
        context = normalize_text(str(context or ""))
        document = self._read()
        material = self._material(document, material_id)
        segments = self._collection(document, "segments")
        if segment_id is None:
            segment_id = _new_id(
                "segments", "segment", {row.get("segment_id") for row in segments}
            )
        else:
            segment_id = _require(segment_id, "源文片段缺少标识")
            if any(row.get("segment_id") == segment_id for row in segments):
                raise ValueError(f"源文片段标识已存在: {segment_id}")
        timestamp = _now(at)
        segment = {
            "segment_id": segment_id,
            "material_id": material_id,
            "subject": material["subject"],
            "text": text,
            "context": context,
            "order": int(order) if order is not None else len(material["segment_ids"]),
            "status": SEGMENT_ACTIVE,
            "superseded_by": [],
            "derived_from": [],
            "derivation_id": None,
            "created_at": timestamp,
        }
        segments.append(segment)
        material.setdefault("segment_ids", []).append(segment_id)
        self._material_revision(material)
        self._write(document)
        return copy.deepcopy(segment)

    def get_segment(self, segment_id):
        return copy.deepcopy(self._segment(self._read(), segment_id))

    def list_segments(self, material_id=None, subject=None, include_superseded=False):
        segments = self._read().get("segments", [])
        if material_id is not None:
            segments = [row for row in segments if row.get("material_id") == material_id]
        if subject is not None:
            segments = [row for row in segments if row.get("subject") == subject]
        if not include_superseded:
            segments = [row for row in segments if row.get("status") == SEGMENT_ACTIVE]
        return [copy.deepcopy(row) for row in segments]

    def current_segment_for(self, segment_id):
        """返回片段的当前去向：自身（仍有效）或拆/合段后的子片段。"""
        document = self._read()
        segment = self._segment(document, segment_id)
        if segment.get("status") == SEGMENT_ACTIVE:
            return [copy.deepcopy(segment)]
        by_id = {row.get("segment_id"): row for row in document.get("segments", [])}
        return [copy.deepcopy(by_id[child]) for child in segment.get("superseded_by", []) if child in by_id]

    # ------------------------------------------------------------------
    # 拆段 / 合段留痕
    # ------------------------------------------------------------------
    def _record_derivation(
        self,
        document,
        operation,
        material,
        parents,
        children,
        derivation_id,
        reason,
        timestamp,
    ):
        derivation = {
            "derivation_id": derivation_id,
            "operation": operation,
            "material_id": material["material_id"],
            "subject": material["subject"],
            "parent_segment_ids": [row["segment_id"] for row in parents],
            "child_segment_ids": [row["segment_id"] for row in children],
            "reason": str(reason or ""),
            "created_at": timestamp,
        }
        self._collection(document, "derivations").append(derivation)
        return derivation

    def _prepare_parent_supersede(self, document, parents, child_ids, derivation_id, operation, timestamp):
        for parent in parents:
            parent["status"] = SEGMENT_SPLIT if operation == "split" else SEGMENT_MERGED
            parent["superseded_by"] = list(child_ids)
            parent["derivation_id"] = derivation_id
            parent["superseded_at"] = timestamp
            parent["superseded_reason"] = operation
        for translation in document.get("translations", []):
            if translation.get("segment_id") in {row["segment_id"] for row in parents}:
                translation["status"] = TRANSLATION_NEEDS_REVIEW
                translation["stale_reason"] = f"source_{operation}"
                translation["stale_derivation_id"] = derivation_id

    def split_segment(
        self,
        segment_id,
        children,
        reason="",
        derivation_id=None,
        at=None,
    ):
        """把源文片段拆成多个子片段，保留父片段及其去向。"""
        if not isinstance(children, (list, tuple)) or not children:
            raise ValueError("拆段必须提供至少一个子片段")
        document = self._read()
        parent = self._segment(document, segment_id)
        if parent.get("status") != SEGMENT_ACTIVE:
            raise ValueError(f"只能拆分有效片段: {segment_id}")
        material = self._material(document, parent["material_id"])
        segments = self._collection(document, "segments")
        existing_ids = {row.get("segment_id") for row in segments}
        timestamp = _now(at)
        derivation_id = derivation_id or _new_id(
            "derivations", "derivation", {row.get("derivation_id") for row in document.get("derivations", [])}
        )
        if any(row.get("derivation_id") == derivation_id for row in document.get("derivations", [])):
            raise ValueError(f"留痕标识已存在: {derivation_id}")
        created = []
        for index, child in enumerate(children):
            if not isinstance(child, dict):
                raise ValueError("拆段子片段必须是字典")
            child_text = normalize_text(_require(child.get("text"), "拆段子片段缺少正文"))
            child_id = child.get("segment_id")
            if child_id is None:
                child_id = _new_id("segments", "segment", existing_ids)
            else:
                child_id = _require(child_id, "拆段子片段缺少标识")
            if child_id in existing_ids:
                raise ValueError(f"源文片段标识已存在: {child_id}")
            existing_ids.add(child_id)
            child_context = normalize_text(str(child.get("context", parent.get("context", ""))))
            # 子片段沿用父片段位置，并在父位置内按顺序细分，避免与后续片段
            # 的位置冲突（原先是父位置 × 100，会与紧邻片段重叠）。
            parent_order = int(parent.get("order", 0))
            child_segment = {
                "segment_id": child_id,
                "material_id": parent["material_id"],
                "subject": parent["subject"],
                "text": child_text,
                "context": child_context,
                "order": parent_order + (index + 1) / (len(children) + 1),
                "status": SEGMENT_ACTIVE,
                "superseded_by": [],
                "derived_from": [segment_id],
                "derivation_id": derivation_id,
                "created_at": timestamp,
            }
            segments.append(child_segment)
            material.setdefault("segment_ids", []).append(child_id)
            created.append(child_segment)
        derivation = self._record_derivation(
            document,
            "split",
            material,
            [parent],
            created,
            derivation_id,
            reason,
            timestamp,
        )
        self._prepare_parent_supersede(
            document, [parent], [row["segment_id"] for row in created], derivation_id, "split", timestamp
        )
        self._material_revision(material)
        created_reviews = self._prepare_structure_reviews(
            document, created, "split", derivation_id, timestamp
        )
        self._write(document)
        return {
            "parent": copy.deepcopy(parent),
            "children": copy.deepcopy(created),
            "derivation": copy.deepcopy(derivation),
            "review_items": copy.deepcopy(created_reviews),
        }

    def merge_segments(
        self,
        segment_ids,
        text=None,
        context=None,
        reason="",
        segment_id=None,
        derivation_id=None,
        at=None,
    ):
        """把多个源文片段合并成一个片段，保留所有父片段及其去向。"""
        if not isinstance(segment_ids, (list, tuple)) or len(segment_ids) < 2:
            raise ValueError("合段必须提供至少两个源文片段")
        document = self._read()
        parents = [self._segment(document, item) for item in segment_ids]
        if len({row["segment_id"] for row in parents}) != len(parents):
            raise ValueError("合段片段不能重复")
        material_ids = {row["material_id"] for row in parents}
        if len(material_ids) != 1:
            raise ValueError("只能合并同一材料内的片段")
        if any(row.get("status") != SEGMENT_ACTIVE for row in parents):
            raise ValueError("只能合并有效片段")
        material = self._material(document, parents[0]["material_id"])
        segments = self._collection(document, "segments")
        timestamp = _now(at)
        if segment_id is None:
            segment_id = _new_id("segments", "segment", {row.get("segment_id") for row in segments})
        else:
            segment_id = _require(segment_id, "合段片段缺少标识")
            if any(row.get("segment_id") == segment_id for row in segments):
                raise ValueError(f"源文片段标识已存在: {segment_id}")
        if text is None:
            combined = " ".join(row["text"] for row in parents)
        else:
            combined = str(text)
        combined = normalize_text(_require(combined, "合段片段缺少正文"))
        if context is None:
            context = parents[0].get("context", "")
        derivation_id = derivation_id or _new_id(
            "derivations", "derivation", {row.get("derivation_id") for row in document.get("derivations", [])}
        )
        if any(row.get("derivation_id") == derivation_id for row in document.get("derivations", [])):
            raise ValueError(f"留痕标识已存在: {derivation_id}")
        merged = {
            "segment_id": segment_id,
            "material_id": material["material_id"],
            "subject": material["subject"],
            "text": combined,
            "context": normalize_text(str(context or "")),
            "order": min(row.get("order", 0) for row in parents),
            "status": SEGMENT_ACTIVE,
            "superseded_by": [],
            "derived_from": [row["segment_id"] for row in parents],
            "derivation_id": derivation_id,
            "created_at": timestamp,
        }
        segments.append(merged)
        material.setdefault("segment_ids", []).append(segment_id)
        derivation = self._record_derivation(
            document,
            "merge",
            material,
            parents,
            [merged],
            derivation_id,
            reason,
            timestamp,
        )
        self._prepare_parent_supersede(
            document,
            parents,
            [segment_id],
            derivation_id,
            "merge",
            timestamp,
        )
        self._material_revision(material)
        # 先把译文失效与复核项在内存中准备好，再一次性写盘，避免半完成状态。
        created_reviews = self._prepare_structure_reviews(
            document, [merged], "merge", derivation_id, timestamp
        )
        self._write(document)
        return {
            "parents": copy.deepcopy(parents),
            "segment": copy.deepcopy(merged),
            "derivation": copy.deepcopy(derivation),
            "review_items": copy.deepcopy(created_reviews),
        }

    @staticmethod
    def _prepare_structure_reviews(document, children, operation, derivation_id, timestamp):
        """为拆/合段产生的旧译文建立待复核项（仅在内存中修改）。"""
        reviews = document.setdefault("review_items", [])
        child_ids = sorted(row["segment_id"] for row in children)
        created = []
        for translation in document.get("translations", []):
            if translation.get("status") == TRANSLATION_NEEDS_REVIEW and translation.get(
                "stale_derivation_id"
            ) == derivation_id:
                review = {
                    "review_id": _new_id(
                        "review_items", "review", {row.get("review_id") for row in reviews}
                    ),
                    "kind": f"segment_{operation}",
                    "reason": f"源文片段发生 {operation} 操作，原译文需复核",
                    "status": "pending",
                    "term": "",
                    "subject": translation.get("subject", ""),
                    "translation_id": translation.get("translation_id"),
                    "segment_id": translation.get("segment_id"),
                    "material_id": translation.get("material_id"),
                    "derivation_id": derivation_id,
                    "successor_segment_ids": child_ids,
                    "created_at": timestamp,
                    "resolved_at": None,
                    "resolution": "",
                    "reviewed_by": "",
                }
                reviews.append(review)
                created.append(review)
        return created

    def segment_lineage(self, segment_id):
        """返回一个片段的来源、去向和全部拆/合段留痕。"""
        document = self._read()
        segment = self._segment(document, segment_id)
        by_id = {row.get("segment_id"): row for row in document.get("segments", [])}
        derivations = {
            row.get("derivation_id"): row for row in document.get("derivations", []) if row.get("derivation_id")
        }
        ancestors = []
        descendants = []
        visited = set()
        pending = list(segment.get("derived_from", []))
        while pending:
            current = pending.pop(0)
            if current in visited or current not in by_id:
                continue
            visited.add(current)
            ancestors.append(copy.deepcopy(by_id[current]))
            pending.extend(by_id[current].get("derived_from", []))
        visited = set()
        pending = list(segment.get("superseded_by", []))
        while pending:
            current = pending.pop(0)
            if current in visited or current not in by_id:
                continue
            visited.add(current)
            descendants.append(copy.deepcopy(by_id[current]))
            pending.extend(by_id[current].get("superseded_by", []))
        related = []
        related_ids = [segment_id] + [row["segment_id"] for row in ancestors + descendants]
        for derivation in derivations.values():
            if set(derivation.get("parent_segment_ids", [])) & set(related_ids) or set(
                derivation.get("child_segment_ids", [])
            ) & set(related_ids):
                related.append(copy.deepcopy(derivation))
        related.sort(key=lambda row: (row.get("created_at", ""), row.get("derivation_id", "")))
        return {
            "segment": copy.deepcopy(segment),
            "status": segment.get("status"),
            "superseded_by": list(segment.get("superseded_by", [])),
            "derived_from": list(segment.get("derived_from", [])),
            "ancestors": ancestors,
            "descendants": descendants,
            "derivations": related,
        }

    # ------------------------------------------------------------------
    # 术语决定与语境例外
    # ------------------------------------------------------------------
    def record_term_decision(
        self,
        term,
        subject,
        translation,
        context=None,
        confirmed_by="",
        note="",
        definition="",
        decision_id=None,
        at=None,
    ):
        """记录（或修订）教师确认的术语译法。

        同一 ``术语 + 学科 + 语境`` 的旧决定会被标记为 superseded 而不是删除，
        受影响的译文进入待复核状态，但语境例外始终优先且不会被改写。
        """
        term = _require(term, "术语决定缺少术语")
        subject = _require(subject, "术语决定缺少学科")
        translation = _require(translation, "术语决定缺少确认译文")
        context = None if context is None else normalize_text(str(context))
        document = self._read()
        decisions = self._collection(document, "term_decisions")
        timestamp = _now(at)
        previous = None
        for row in decisions:
            if (
                row.get("status") == DECISION_CONFIRMED
                and _key(row.get("term")) == _key(term)
                and row.get("subject") == subject
                and _key(row.get("context")) == _key(context)
            ):
                previous = row
                break
        version = 1 if previous is None else int(previous.get("version", 1)) + 1
        if decision_id is None:
            decision_id = _new_id("term_decisions", "decision", {row.get("decision_id") for row in decisions})
        else:
            decision_id = _require(decision_id, "术语决定缺少标识")
            if any(row.get("decision_id") == decision_id for row in decisions):
                raise ValueError(f"术语决定标识已存在: {decision_id}")
        decision = {
            "decision_id": decision_id,
            "term": term,
            "term_key": _key(term),
            "subject": subject,
            "translation": translation,
            "context": context,
            "definition": str(definition or ""),
            "version": version,
            "status": DECISION_CONFIRMED,
            "supersedes": previous.get("decision_id") if previous else None,
            "confirmed_by": str(confirmed_by or ""),
            "confirmed_at": timestamp,
            "note": str(note or ""),
        }
        decisions.append(decision)
        if previous is not None:
            previous["status"] = DECISION_SUPERSEDED
            previous["superseded_by"] = decision_id
            previous["superseded_at"] = timestamp
        affected_translations, affected_segments = self._apply_term_change(
            document, decision, previous, timestamp
        )
        self._write(document)
        affected_materials = self._affected_materials(
            document, decision, affected_translations, affected_segments
        )
        affected_versions = self._affected_published_versions(document, decision)
        return {
            "decision": copy.deepcopy(decision),
            "previous": copy.deepcopy(previous) if previous else None,
            "affected_translations": copy.deepcopy(affected_translations),
            "affected_materials": affected_materials,
            "affected_published_versions": affected_versions,
            "review_items": [
                copy.deepcopy(row)
                for row in document.get("review_items", [])
                if row.get("new_decision_id") == decision_id
            ],
        }

    # 语义更明确的别名。
    update_term_decision = record_term_decision

    def _resolve_term(self, document, segment, term):
        """按例外 > 语境决定 > 学科默认决定的顺序解析一个术语。"""
        term_key = _key(term)
        subject = segment.get("subject")
        context_key = _key(segment.get("context"))
        exceptions = [
            row
            for row in document.get("context_exceptions", [])
            if row.get("active", True)
            and row.get("subject") == subject
            and _key(row.get("term")) == term_key
        ]
        segment_match = next(
            (row for row in exceptions if row.get("segment_id") == segment.get("segment_id")), None
        )
        if segment_match:
            return {
                "term": segment_match["term"],
                "translation": segment_match["translation"],
                "source": "exception",
                "decision_id": None,
                "exception_id": segment_match["exception_id"],
                "context": segment_match.get("context"),
            }
        context_match = next(
            (
                row
                for row in exceptions
                if row.get("segment_id") is None
                and row.get("context") is not None
                and _key(row.get("context")) == context_key
            ),
            None,
        )
        if context_match:
            return {
                "term": context_match["term"],
                "translation": context_match["translation"],
                "source": "exception",
                "decision_id": None,
                "exception_id": context_match["exception_id"],
                "context": context_match.get("context"),
            }
        subject_match = next(
            (
                row
                for row in exceptions
                if row.get("segment_id") is None and row.get("context") is None
            ),
            None,
        )
        if subject_match:
            return {
                "term": subject_match["term"],
                "translation": subject_match["translation"],
                "source": "exception",
                "decision_id": None,
                "exception_id": subject_match["exception_id"],
                "context": None,
            }
        confirmed = [
            row
            for row in document.get("term_decisions", [])
            if row.get("status") == DECISION_CONFIRMED
            and row.get("subject") == subject
            and _key(row.get("term")) == term_key
        ]
        context_decision = next(
            (
                row
                for row in confirmed
                if row.get("context") is not None and _key(row.get("context")) == context_key
            ),
            None,
        )
        selected = context_decision
        source = "context_decision"
        if selected is None:
            selected = next((row for row in confirmed if row.get("context") is None), None)
            source = "term_decision"
        if selected is None:
            return {
                "term": term,
                "translation": None,
                "source": None,
                "decision_id": None,
                "exception_id": None,
                "context": None,
            }
        return {
            "term": selected["term"],
            "translation": selected["translation"],
            "source": source,
            "decision_id": selected["decision_id"],
            "exception_id": None,
            "context": selected.get("context"),
        }

    def _known_terms(self, document, subject):
        terms = {}
        for row in document.get("term_decisions", []):
            if row.get("subject") == subject and row.get("status") == DECISION_CONFIRMED:
                terms.setdefault(_key(row["term"]), row["term"])
        for row in document.get("context_exceptions", []):
            if row.get("subject") == subject and row.get("active", True):
                terms.setdefault(_key(row["term"]), row["term"])
        return list(terms.values())

    def _terms_in_segment(self, document, segment):
        """片段中实际应生效的已知术语。

        中文术语可能互为子串（如“光”与“光合作用”）。只有当短术语的**每一次**
        出现都被同一学科内更长的术语覆盖时才抑制它；独立出现的短术语照常生效，
        例如“光合作用需要光”同时包含两个术语。
        """
        text = segment.get("text", "")
        matched = [
            term
            for term in self._known_terms(document, segment.get("subject"))
            if term_occurs(term, text)
        ]
        spans = {term: term_occurrences(term, text) for term in matched}
        result = []
        for term in matched:
            key = _key(term)
            own_spans = spans[term]
            longer_spans = [
                span
                for other in matched
                if other != term and len(_key(other)) > len(key) and key in _key(other)
                for span in spans[other]
            ]
            if own_spans and all(
                any(start >= outer_start and end <= outer_end for outer_start, outer_end in longer_spans)
                for start, end in own_spans
            ):
                continue
            result.append(term)
        return result

    def resolve_segment_terms(self, segment_id):
        """解析片段中所有已知术语当前生效的译法及其依据。"""
        document = self._read()
        segment = self._segment(document, segment_id)
        resolved = [
            self._resolve_term(document, segment, term)
            for term in self._terms_in_segment(document, segment)
        ]
        return {
            "segment_id": segment_id,
            "material_id": segment.get("material_id"),
            "subject": segment.get("subject"),
            "context": segment.get("context"),
            "terms": resolved,
        }

    def record_context_exception(
        self,
        term,
        translation,
        subject,
        context=None,
        segment_id=None,
        confirmed_by="",
        note="",
        exception_id=None,
        at=None,
    ):
        """教师为某个语境接受例外译法。

        例外独立存储，术语修订只影响 ``term_decisions``，永远不会覆盖或删除例外。
        """
        term = _require(term, "例外译法缺少术语")
        subject = _require(subject, "例外译法缺少学科")
        translation = _require(translation, "例外译法缺少确认译文")
        document = self._read()
        exceptions = self._collection(document, "context_exceptions")
        segment = None
        if segment_id is not None:
            segment = self._segment(document, segment_id)
            if segment.get("subject") != subject:
                raise ValueError("例外译法的学科与源文片段不一致")
            if context is None:
                context = segment.get("context", "")
        if context is not None:
            context = normalize_text(str(context))
        timestamp = _now(at)
        if exception_id is None:
            exception_id = _new_id(
                "context_exceptions", "exception", {row.get("exception_id") for row in exceptions}
            )
        else:
            exception_id = _require(exception_id, "例外译法缺少标识")
            if any(row.get("exception_id") == exception_id for row in exceptions):
                raise ValueError(f"例外译法标识已存在: {exception_id}")
        # 同一作用域的显式新例外替代旧例外；术语更新不会走这条路径。
        for row in exceptions:
            if (
                row.get("active", True)
                and row.get("subject") == subject
                and _key(row.get("term")) == _key(term)
                and row.get("segment_id") == segment_id
                and _key(row.get("context")) == _key(context)
            ):
                row["active"] = False
                row["superseded_by"] = exception_id
                row["superseded_at"] = timestamp
        exception = {
            "exception_id": exception_id,
            "term": term,
            "term_key": _key(term),
            "subject": subject,
            "translation": translation,
            "context": context,
            "segment_id": segment_id,
            "active": True,
            "confirmed_by": str(confirmed_by or ""),
            "confirmed_at": timestamp,
            "note": str(note or ""),
        }
        exceptions.append(exception)
        # 已有译文如果已经采用该例外译法，则解除因旧术语修订产生的待复核状态。
        refreshed = self._refresh_translations_for_exception(document, exception)
        self._write(document)
        return {
            "exception": copy.deepcopy(exception),
            "refreshed_translations": refreshed,
        }

    def _refresh_translations_for_exception(self, document, exception):
        refreshed = []
        for translation in document.get("translations", []):
            if translation.get("subject") != exception["subject"]:
                continue
            segment = next(
                (
                    row
                    for row in document.get("segments", [])
                    if row.get("segment_id") == translation.get("segment_id")
                ),
                None,
            )
            if segment is None or not term_occurs(exception["term"], segment.get("text", "")):
                continue
            resolution = self._resolve_term(document, segment, exception["term"])
            if resolution.get("source") != "exception" or resolution.get("exception_id") != exception["exception_id"]:
                continue
            entry = next(
                (
                    row
                    for row in translation.get("terms", [])
                    if _key(row.get("term")) == _key(exception["term"])
                ),
                None,
            )
            target = list(translation.get("terms", []))
            value = {
                "term": exception["term"],
                "translation": exception["translation"],
                "source": "exception",
                "decision_id": None,
                "exception_id": exception["exception_id"],
                "context": exception.get("context"),
            }
            if entry is not None:
                entry.update(value)
            else:
                target.append(value)
            translation["terms"] = target
            exception_ids = list(translation.get("exception_ids", []))
            if exception["exception_id"] not in exception_ids:
                exception_ids.append(exception["exception_id"])
            translation["exception_ids"] = exception_ids
            if translation.get("status") == TRANSLATION_NEEDS_REVIEW and str(
                translation.get("stale_reason", "")
            ).startswith("term_updated"):
                self._resolve_reviews_for(
                    document.setdefault("review_items", []),
                    translation,
                    exception["term"],
                    _now(),
                    "context_exception",
                )
                if not self._has_other_pending_reviews(
                    document, translation.get("translation_id"), exception["term"]
                ):
                    translation["status"] = TRANSLATION_CURRENT
                    translation["stale_reason"] = ""
            refreshed.append(copy.deepcopy(translation))
        return refreshed

    def list_term_decisions(self, term=None, subject=None, context=None, include_superseded=True):
        rows = self._read().get("term_decisions", [])
        if term is not None:
            rows = [row for row in rows if _key(row.get("term")) == _key(term)]
        if subject is not None:
            rows = [row for row in rows if row.get("subject") == subject]
        if context is not None:
            rows = [row for row in rows if _key(row.get("context")) == _key(context)]
        if not include_superseded:
            rows = [row for row in rows if row.get("status") == DECISION_CONFIRMED]
        return copy.deepcopy(rows)

    def list_context_exceptions(self, term=None, subject=None, include_inactive=False):
        rows = self._read().get("context_exceptions", [])
        if term is not None:
            rows = [row for row in rows if _key(row.get("term")) == _key(term)]
        if subject is not None:
            rows = [row for row in rows if row.get("subject") == subject]
        if not include_inactive:
            rows = [row for row in rows if row.get("active", True)]
        return copy.deepcopy(rows)

    def _apply_term_change(self, document, decision, previous, timestamp):
        """把术语修订传播到受影响译文，并生成待复核项。

        例外优先：解析到例外的片段不会被标记为过期。首次登记新术语时不制造
        复核噪音；只有真正改变既有译法（存在旧决定或旧译文快照）时才要求复核。
        """
        affected_translations = []
        candidate_segments = [
            row
            for row in document.get("segments", [])
            if row.get("status") == SEGMENT_ACTIVE
            and row.get("subject") == decision["subject"]
            and term_occurs(decision["term"], row.get("text", ""))
        ]
        # 受影响的片段由解析结果决定：语境例外/其它语境决定保护的片段不计入。
        affected_segments = []
        for segment in candidate_segments:
            resolution = self._resolve_term(document, segment, decision["term"])
            if resolution.get("decision_id") == decision["decision_id"]:
                affected_segments.append(segment)
        translations = document.get("translations", [])
        reviews = self._collection(document, "review_items")
        for translation in translations:
            segment = next(
                (
                    row
                    for row in document.get("segments", [])
                    if row.get("segment_id") == translation.get("segment_id")
                ),
                None,
            )
            if segment is None or segment.get("status") != SEGMENT_ACTIVE:
                continue
            if translation.get("subject") != decision["subject"]:
                continue
            if not term_occurs(decision["term"], segment.get("text", "")):
                continue
            if translation.get("status") not in (TRANSLATION_CURRENT, TRANSLATION_NEEDS_REVIEW):
                continue
            resolution = self._resolve_term(document, segment, decision["term"])
            if resolution.get("source") == "exception":
                # 教师确认的语境例外不受术语修订影响。
                continue
            if resolution.get("decision_id") != decision["decision_id"]:
                # 该片段受其它语境决定保护。
                continue
            entry = next(
                (
                    row
                    for row in translation.get("terms", [])
                    if _key(row.get("term")) == _key(decision["term"])
                ),
                None,
            )
            previous_value = entry.get("translation") if entry else None
            if entry is not None and previous_value == decision["translation"]:
                # 译文快照已经是新译法：只同步决定引用，避免无意义复核。
                self._attach_term_entry(translation, resolution, previous)
                # 术语改回旧译法时，之前产生的待复核项自动失效。
                if translation.get("status") == TRANSLATION_NEEDS_REVIEW and translation.get(
                    "stale_reason"
                ) == f"term_updated:{decision['term']}":
                    self._resolve_reviews_for(
                        reviews, translation, decision["term"], timestamp, "reverted_to_previous"
                    )
                    if not self._has_other_pending_reviews(
                        document, translation.get("translation_id"), decision["term"]
                    ):
                        translation["status"] = TRANSLATION_CURRENT
                        translation["stale_reason"] = ""
                continue
            if entry is None and previous is None:
                # 首次为某个作用域登记术语：以此作为基线，不追溯判定既有译文过期。
                self._attach_term_entry(translation, resolution, previous)
                continue
            # 译文快照与新的确认译法不一致（或尚无快照）：旧译文不再推荐，
            # 但保留原快照作为复核依据，等教师确认后再更新。
            if entry is None:
                translation.setdefault("terms", []).append(
                    {
                        "term": decision["term"],
                        "translation": previous.get("translation") if previous else None,
                        "source": "term_decision",
                        "decision_id": previous.get("decision_id") if previous else None,
                        "exception_id": None,
                        "context": previous.get("context") if previous else None,
                    }
                )
                previous_value = previous.get("translation") if previous else None
            if translation.get("status") == TRANSLATION_NEEDS_REVIEW and translation.get(
                "stale_reason"
            ) == f"term_updated:{decision['term']}":
                # 同一术语再次修订：更新已有待复核项的期望值，避免重复堆积。
                for review in reviews:
                    if (
                        review.get("status") == "pending"
                        and review.get("translation_id") == translation.get("translation_id")
                        and _key(review.get("term")) == _key(decision["term"])
                    ):
                        review["previous_decision_id"] = review.get("new_decision_id")
                        review["new_decision_id"] = decision["decision_id"]
                        review["previous_translation"] = previous_value
                        review["expected_translation"] = decision["translation"]
                translation["stale_decision_id"] = decision["decision_id"]
                affected_translations.append(copy.deepcopy(translation))
                continue
            translation["status"] = TRANSLATION_NEEDS_REVIEW
            translation["stale_reason"] = f"term_updated:{decision['term']}"
            translation["stale_decision_id"] = decision["decision_id"]
            affected_translations.append(copy.deepcopy(translation))
            reviews.append(
                {
                    "review_id": _new_id(
                        "review_items", "review", {row.get("review_id") for row in reviews}
                    ),
                    "kind": "term_revision",
                    "reason": f"术语“{decision['term']}”的确认译法已修订，旧译文需复核",
                    "status": "pending",
                    "term": decision["term"],
                    "subject": decision["subject"],
                    "translation_id": translation.get("translation_id"),
                    "segment_id": segment["segment_id"],
                    "material_id": segment.get("material_id"),
                    "previous_decision_id": previous.get("decision_id") if previous else None,
                    "new_decision_id": decision["decision_id"],
                    "previous_translation": previous_value,
                    "expected_translation": decision["translation"],
                    "created_at": timestamp,
                    "resolved_at": None,
                    "resolution": "",
                    "reviewed_by": "",
                }
            )
        # 含有该术语但尚无译文的片段也需要复核/补译。
        # 仅当这次修订真正改变了确认译法时才提示，避免重复提交相同决定产生噪音。
        term_changed = previous is not None and previous.get("translation") != decision["translation"]
        covered_segments = {row.get("segment_id") for row in translations}
        if term_changed:
            for segment in affected_segments:
                if segment["segment_id"] in covered_segments:
                    continue
                reviews.append(
                    {
                        "review_id": _new_id(
                            "review_items", "review", {row.get("review_id") for row in reviews}
                        ),
                        "kind": "term_revision_missing_translation",
                        "reason": f"术语“{decision['term']}”修订后，该片段需要补充译文",
                        "status": "pending",
                        "term": decision["term"],
                        "subject": decision["subject"],
                        "translation_id": None,
                        "segment_id": segment["segment_id"],
                        "material_id": segment.get("material_id"),
                        "previous_decision_id": previous.get("decision_id") if previous else None,
                        "new_decision_id": decision["decision_id"],
                        "previous_translation": previous.get("translation") if previous else None,
                        "expected_translation": decision["translation"],
                        "created_at": timestamp,
                        "resolved_at": None,
                        "resolution": "",
                        "reviewed_by": "",
                    }
                )
        return affected_translations, affected_segments

    def _attach_term_entry(self, translation, resolution, previous_decision):
        """把当前生效的术语依据写入译文的术语快照。"""
        entry = next(
            (
                row
                for row in translation.setdefault("terms", [])
                if _key(row.get("term")) == _key(resolution["term"])
            ),
            None,
        )
        if entry is None:
            translation["terms"].append(dict(resolution))
        else:
            # 保留教师确认的目标文本用法，只刷新依据引用。
            entry.update(
                {
                    "term": resolution["term"],
                    "translation": resolution["translation"],
                    "source": resolution.get("source"),
                    "decision_id": resolution.get("decision_id"),
                    "exception_id": resolution.get("exception_id"),
                    "context": resolution.get("context"),
                }
            )
        decision_id = resolution.get("decision_id")
        decision_ids = [
            row
            for row in translation.get("term_decision_ids", [])
            if row != (previous_decision or {}).get("decision_id")
        ]
        if decision_id and decision_id not in decision_ids:
            decision_ids.append(decision_id)
        translation["term_decision_ids"] = decision_ids
        if resolution.get("exception_id") is not None:
            exception_ids = list(translation.get("exception_ids", []))
            if resolution["exception_id"] not in exception_ids:
                exception_ids.append(resolution["exception_id"])
            translation["exception_ids"] = exception_ids

    @staticmethod
    def _has_other_pending_reviews(document, translation_id, term, exclude_review_id=None):
        return any(
            row.get("status") == "pending"
            and row.get("translation_id") == translation_id
            and row.get("review_id") != exclude_review_id
            and _key(row.get("term")) != _key(term)
            for row in document.get("review_items", [])
        )

    @staticmethod
    def _resolve_reviews_for(reviews, translation, term, timestamp, resolution):
        for review in reviews:
            if (
                review.get("status") == "pending"
                and review.get("translation_id") == translation.get("translation_id")
                and _key(review.get("term")) == _key(term)
            ):
                review["status"] = "resolved"
                review["resolution"] = resolution
                review["resolved_at"] = timestamp

    def _affected_materials(self, document, decision, affected_translations, affected_segments):
        material_ids = {row.get("material_id") for row in affected_segments}
        material_ids.update(row.get("material_id") for row in affected_translations)
        materials = {row["material_id"]: row for row in document.get("materials", [])}
        result = []
        for material_id in sorted(material_ids):
            material = materials.get(material_id)
            if material is None:
                continue
            segment_ids = sorted(
                {
                    row["segment_id"]
                    for row in affected_segments
                    if row.get("material_id") == material_id
                }
                | {
                    row["segment_id"]
                    for row in affected_translations
                    if row.get("material_id") == material_id
                }
            )
            translation_ids = sorted(
                row["translation_id"]
                for row in affected_translations
                if row.get("material_id") == material_id
            )
            versions = sorted(
                row.get("version")
                for row in document.get("material_versions", [])
                if row.get("material_id") == material_id
            )
            result.append(
                {
                    "material_id": material_id,
                    "title": material.get("title", ""),
                    "subject": material.get("subject", ""),
                    "material_type": material.get("material_type", ""),
                    "segment_ids": segment_ids,
                    "translation_ids": translation_ids,
                    "published_versions": versions,
                    "current_version": material.get("current_version", 0),
                }
            )
        return result

    def _affected_published_versions(self, document, decision):
        segments_by_id = {row.get("segment_id"): row for row in document.get("segments", [])}
        affected = []
        for version in document.get("material_versions", []):
            if version.get("subject") != decision["subject"]:
                continue
            segment_ids = []
            for snapshot_segment in version.get("segments", []):
                if not term_occurs(decision["term"], snapshot_segment.get("text", "")):
                    continue
                live = segments_by_id.get(snapshot_segment.get("segment_id"))
                # 语境例外保护的片段不受这次术语修订影响。
                if live is not None and self._resolve_term(document, live, decision["term"]).get(
                    "source"
                ) == "exception":
                    continue
                segment_ids.append(snapshot_segment.get("segment_id"))
            if segment_ids:
                affected.append(
                    {
                        "material_id": version.get("material_id"),
                        "version": version.get("version"),
                        "segment_ids": sorted(segment_ids),
                    }
                )
        return affected

    # ------------------------------------------------------------------
    # 译文与翻译记忆查找
    # ------------------------------------------------------------------
    def record_translation(
        self,
        segment_id,
        target_text,
        confirmed_by="",
        note="",
        translation_id=None,
        at=None,
    ):
        """记录教师确认的片段译文，并快照当时生效的术语决定。"""
        segment_id = _require(segment_id, "译文缺少源文片段")
        target_text = _require(target_text, "译文缺少正文")
        document = self._read()
        segment = self._segment(document, segment_id)
        if segment.get("status") != SEGMENT_ACTIVE:
            raise ValueError("只能为有效源文片段记录译文")
        material = self._material(document, segment["material_id"])
        translations = self._collection(document, "translations")
        timestamp = _now(at)
        if translation_id is None:
            translation_id = _new_id(
                "translations", "translation", {row.get("translation_id") for row in translations}
            )
        else:
            translation_id = _require(translation_id, "译文缺少标识")
            if any(row.get("translation_id") == translation_id for row in translations):
                raise ValueError(f"译文标识已存在: {translation_id}")
        # 同一片段只保留一条当前译文，旧译文保留为历史但不再推荐。
        for row in translations:
            if row.get("segment_id") == segment_id and row.get("status") == TRANSLATION_CURRENT:
                row["status"] = TRANSLATION_SUPERSEDED
                row["superseded_by"] = translation_id
                row["superseded_at"] = timestamp
        terms = [
            dict(self._resolve_term(document, segment, term))
            for term in self._terms_in_segment(document, segment)
        ]
        translation = {
            "translation_id": translation_id,
            "segment_id": segment_id,
            "material_id": material["material_id"],
            "subject": segment["subject"],
            "source_text": segment["text"],
            "target_text": target_text,
            "context": segment.get("context", ""),
            "terms": terms,
            "term_decision_ids": [
                row["decision_id"] for row in terms if row.get("decision_id")
            ],
            "exception_ids": [
                row["exception_id"] for row in terms if row.get("exception_id")
            ],
            "status": TRANSLATION_CURRENT,
            "stale_reason": "",
            "confirmed_by": str(confirmed_by or ""),
            "confirmed_at": timestamp,
            "note": str(note or ""),
        }
        translations.append(translation)
        self._material_revision(material)
        self._write(document)
        return copy.deepcopy(translation)

    def get_translation(self, translation_id):
        return copy.deepcopy(
            self._find(self._read(), "translations", "translation_id", translation_id, "译文")
        )

    def list_translations(
        self,
        material_id=None,
        subject=None,
        segment_id=None,
        status=None,
        include_superseded=True,
    ):
        rows = self._read().get("translations", [])
        if material_id is not None:
            rows = [row for row in rows if row.get("material_id") == material_id]
        if subject is not None:
            rows = [row for row in rows if row.get("subject") == subject]
        if segment_id is not None:
            rows = [row for row in rows if row.get("segment_id") == segment_id]
        if status is not None:
            rows = [row for row in rows if row.get("status") == status]
        if not include_superseded:
            rows = [row for row in rows if row.get("status") != TRANSLATION_SUPERSEDED]
        return copy.deepcopy(rows)

    def lookup(self, source_text, subject, context=None, include_stale=False):
        """按源文、学科与语境查找可复用的教师确认译文。

        学科必须显式提供：同一句源文在不同学科下不能共用单一译文。
        已过期、已失效（片段被拆/合）的译文默认不返回。
        """
        source_key = _key(source_text)
        subject = _require(subject, "查找翻译记忆时必须提供学科")
        if context is not None:
            context_key = _key(context)
        else:
            context_key = None
        document = self._read()
        segments = {row.get("segment_id"): row for row in document.get("segments", [])}
        results = []
        for translation in document.get("translations", []):
            if _key(translation.get("source_text")) != source_key:
                continue
            if translation.get("subject") != subject:
                continue
            if context_key is not None and _key(translation.get("context")) != context_key:
                continue
            if not include_stale and translation.get("status") != TRANSLATION_CURRENT:
                continue
            segment = segments.get(translation.get("segment_id"))
            if segment is None or segment.get("status") != SEGMENT_ACTIVE:
                continue
            results.append(copy.deepcopy(translation))
        results.sort(key=lambda row: (row.get("confirmed_at", ""), row.get("translation_id", "")))
        return results

    # ------------------------------------------------------------------
    # 复核
    # ------------------------------------------------------------------
    def pending_reviews(self, material_id=None, subject=None, term=None, status="pending"):
        rows = self._read().get("review_items", [])
        if status is not None:
            rows = [row for row in rows if row.get("status") == status]
        if material_id is not None:
            rows = [row for row in rows if row.get("material_id") == material_id]
        if subject is not None:
            rows = [row for row in rows if row.get("subject") == subject]
        if term is not None:
            rows = [row for row in rows if _key(row.get("term")) == _key(term)]
        return copy.deepcopy(rows)

    def resolve_review(
        self,
        review_id,
        reviewed_by="",
        resolution="updated",
        target_text=None,
        note="",
        at=None,
    ):
        """复核一项待办。

        术语复核提供 ``target_text`` 时更新该译文并重新快照术语决定。
        拆/合段复核指向的旧片段已失效，不能通过它复活旧译文；此时
        ``target_text`` 会记录到复核项作为参考，请在新片段上另行翻译。
        """
        document = self._read()
        review = self._find(document, "review_items", "review_id", review_id, "复核项")
        if review.get("status") != "pending":
            raise ValueError(f"复核项已处理: {review_id}")
        timestamp = _now(at)
        structural = review.get("kind") in ("segment_split", "segment_merge")
        if structural and target_text is not None:
            raise ValueError("拆/合段复核不能直接改写旧译文，请在新片段上记录译文")
        if target_text is not None:
            target_text = _require(target_text, "复核译文不能为空")
            translation = None
            if review.get("translation_id") is not None:
                translation = next(
                    (
                        row
                        for row in document.get("translations", [])
                        if row.get("translation_id") == review.get("translation_id")
                    ),
                    None,
                )
            if translation is None:
                if review.get("segment_id") is None:
                    raise ValueError("复核项没有可更新的片段")
                segment = self._segment(document, review["segment_id"])
                if segment.get("status") != SEGMENT_ACTIVE:
                    raise ValueError("源文片段已失效，无法直接补译")
                # 用普通记录流程会重新读取存储，因此在这里就地创建。
                translation = {
                    "translation_id": _new_id(
                        "translations", "translation", {row.get("translation_id") for row in document.get("translations", [])}
                    ),
                    "segment_id": segment["segment_id"],
                    "material_id": segment["material_id"],
                    "subject": segment["subject"],
                    "source_text": segment["text"],
                    "target_text": target_text,
                    "context": segment.get("context", ""),
                    "terms": [],
                    "term_decision_ids": [],
                    "exception_ids": [],
                    "status": TRANSLATION_CURRENT,
                    "stale_reason": "",
                    "confirmed_by": str(reviewed_by or ""),
                    "confirmed_at": timestamp,
                    "note": str(note or ""),
                }
                translation["terms"] = [
                    dict(self._resolve_term(document, segment, term))
                    for term in self._terms_in_segment(document, segment)
                ]
                translation["term_decision_ids"] = [
                    row["decision_id"] for row in translation["terms"] if row.get("decision_id")
                ]
                translation["exception_ids"] = [
                    row["exception_id"] for row in translation["terms"] if row.get("exception_id")
                ]
                document.setdefault("translations", []).append(translation)
                review["translation_id"] = translation["translation_id"]
            else:
                translation["target_text"] = target_text
                translation["status"] = TRANSLATION_CURRENT
                translation["stale_reason"] = ""
                translation["confirmed_by"] = str(reviewed_by or "")
                translation["confirmed_at"] = timestamp
                segment = self._segment(document, translation["segment_id"])
                translation["terms"] = [
                    dict(self._resolve_term(document, segment, term))
                    for term in self._terms_in_segment(document, segment)
                ]
                translation["term_decision_ids"] = [
                    row["decision_id"] for row in translation["terms"] if row.get("decision_id")
                ]
                translation["exception_ids"] = [
                    row["exception_id"] for row in translation["terms"] if row.get("exception_id")
                ]
            material = self._material(document, translation["material_id"])
            self._material_revision(material)
            # 同一译文可能还有其它待复核术语；全部处理完才恢复推荐。
            if not self._has_other_pending_reviews(
                document,
                translation.get("translation_id"),
                review.get("term", ""),
                exclude_review_id=review_id,
            ):
                translation["status"] = TRANSLATION_CURRENT
                translation["stale_reason"] = ""
        else:
            # 教师确认旧译法仍可接受（例如已有语境例外）。
            # 拆/合段留下的旧译文只能作为历史，不能因此恢复推荐。
            if (
                not structural
                and resolution in ("kept", "dismissed")
                and review.get("translation_id") is not None
            ):
                translation = next(
                    (
                        row
                        for row in document.get("translations", [])
                        if row.get("translation_id") == review.get("translation_id")
                    ),
                    None,
                )
                if translation is not None:
                    translation["note"] = str(note or translation.get("note", ""))
                    if not self._has_other_pending_reviews(
                        document,
                        translation.get("translation_id"),
                        review.get("term", ""),
                        exclude_review_id=review_id,
                    ):
                        translation["status"] = TRANSLATION_CURRENT
                        translation["stale_reason"] = ""
        review["status"] = "resolved"
        review["resolution"] = str(resolution or "updated")
        review["reviewed_by"] = str(reviewed_by or "")
        review["resolved_at"] = timestamp
        review["resolution_note"] = str(note or "")
        self._write(document)
        return copy.deepcopy(review)

    # ------------------------------------------------------------------
    # 发布与历史版本
    # ------------------------------------------------------------------
    def publish_material(
        self,
        material_id,
        published_by="",
        note="",
        version=None,
        at=None,
        allow_pending_reviews=False,
    ):
        """发布材料快照；历史版本只增不改。

        存在待复核术语的片段默认阻止发布，避免把过期译文带入新课件；
        教师也可显式允许发布，此时快照会标记未复核片段。
        """
        document = self._read()
        material = self._material(document, material_id)
        segments = [
            row
            for row in document.get("segments", [])
            if row.get("material_id") == material_id and row.get("status") == SEGMENT_ACTIVE
        ]
        if not segments:
            raise ValueError("材料没有可发布的源文片段")
        segment_ids = {row["segment_id"] for row in segments}
        blocking = [
            row
            for row in document.get("review_items", [])
            if row.get("status") == "pending"
            and row.get("material_id") == material_id
            and (
                row.get("segment_id") in segment_ids
                or row.get("successor_segment_ids")
                and set(row.get("successor_segment_ids", [])) & segment_ids
            )
        ]
        if blocking and not allow_pending_reviews:
            pending_segments = sorted({row.get("segment_id") for row in blocking if row.get("segment_id")})
            raise ValueError(
                "材料存在待复核内容，不能发布: " + ", ".join(pending_segments or [material_id])
            )
        versions = [row for row in document.get("material_versions", []) if row.get("material_id") == material_id]
        if version is None:
            version = max((int(row.get("version", 0)) for row in versions), default=0) + 1
        else:
            version = int(version)
            if any(int(row.get("version", 0)) == version for row in versions):
                raise ValueError(f"材料版本已存在: {version}")
        timestamp = _now(at)
        snapshot_segments = []
        stale_segments = []
        for segment in sorted(segments, key=lambda row: (row.get("order", 0), row.get("segment_id", ""))):
            translation = self._current_translation(document, segment["segment_id"])
            if translation is not None and translation.get("status") != TRANSLATION_CURRENT:
                stale_segments.append(segment["segment_id"])
            snapshot_segments.append(
                {
                    "segment_id": segment["segment_id"],
                    "text": segment["text"],
                    "context": segment.get("context", ""),
                    "order": segment.get("order", 0),
                    "translation_id": translation.get("translation_id") if translation else None,
                    "target_text": translation.get("target_text") if translation else None,
                    "translation_status": translation.get("status") if translation else None,
                    "terms": copy.deepcopy(translation.get("terms", [])) if translation else [],
                }
            )
        snapshot = {
            "version_id": _new_id(
                "material_versions", "version", {row.get("version_id") for row in document.get("material_versions", [])}
            ),
            "material_id": material_id,
            "version": version,
            "subject": material.get("subject", ""),
            "title": material.get("title", ""),
            "material_type": material.get("material_type", ""),
            "published_at": timestamp,
            "published_by": str(published_by or ""),
            "note": str(note or ""),
            "segments": snapshot_segments,
            "stale_segment_ids": sorted(stale_segments),
            "revision": material.get("revision", 0),
        }
        document.setdefault("material_versions", []).append(snapshot)
        material["current_version"] = version
        material["status"] = "published"
        material["published_revision"] = material.get("revision", 0)
        material["updated_at"] = timestamp
        self._write(document)
        return copy.deepcopy(snapshot)

    def list_material_versions(self, material_id=None):
        rows = self._read().get("material_versions", [])
        if material_id is not None:
            rows = [row for row in rows if row.get("material_id") == material_id]
        rows = sorted(rows, key=lambda row: (row.get("material_id", ""), int(row.get("version", 0))))
        return copy.deepcopy(rows)

    # 历史版本更直观的别名。
    list_versions = list_material_versions

    def get_material_version(self, material_id, version):
        for row in self._read().get("material_versions", []):
            if row.get("material_id") == material_id and int(row.get("version", 0)) == int(version):
                return copy.deepcopy(row)
        raise ValueError(f"找不到材料版本: {material_id} v{version}")

    def explain_version_references(self, material_id, version):
        """说明历史版本中每个片段当前的有效去向。

        已发布快照本身不可变；若某个片段后来被拆段或合段，这里给出它现在
        对应的有效片段，避免引用关系静默丢失。
        """
        snapshot = self.get_material_version(material_id, version)
        document = self._read()
        segments = {row.get("segment_id"): row for row in document.get("segments", [])}
        references = []
        for item in snapshot.get("segments", []):
            segment_id = item.get("segment_id")
            segment = segments.get(segment_id)
            if segment is None:
                references.append(
                    {
                        "segment_id": segment_id,
                        "text": item.get("text", ""),
                        "status": "missing",
                        "superseded_by": [],
                        "current_segment_ids": [],
                        "derivation_id": None,
                    }
                )
                continue
            current_ids = self._current_segment_ids(document, segment)
            references.append(
                {
                    "segment_id": segment_id,
                    "text": item.get("text", ""),
                    "status": segment.get("status", SEGMENT_ACTIVE),
                    "superseded_by": list(segment.get("superseded_by", [])),
                    "current_segment_ids": current_ids,
                    "derivation_id": segment.get("derivation_id"),
                    "derivation": copy.deepcopy(
                        next(
                            (
                                row
                                for row in document.get("derivations", [])
                                if row.get("derivation_id") == segment.get("derivation_id")
                            ),
                            None,
                        )
                    ),
                }
            )
        return {
            "material_id": material_id,
            "version": int(snapshot.get("version", 0)),
            "published_at": snapshot.get("published_at"),
            "references": references,
        }

    @staticmethod
    def _current_segment_ids(document, segment):
        segments = {row.get("segment_id"): row for row in document.get("segments", [])}
        if segment.get("status") == SEGMENT_ACTIVE:
            return [segment["segment_id"]]
        result = []
        pending = list(segment.get("superseded_by", []))
        visited = set()
        while pending:
            current = pending.pop(0)
            if current in visited or current not in segments:
                continue
            visited.add(current)
            row = segments[current]
            if row.get("status") == SEGMENT_ACTIVE:
                result.append(current)
            else:
                pending.extend(row.get("superseded_by", []))
        return sorted(result)

    # ------------------------------------------------------------------
    # 一致性
    # ------------------------------------------------------------------
    def check_consistency(self, subject=None, term=None):
        """检查同一学科内教师确认译法是否一致。

        以当前有效译文的术语快照为准：同一 ``学科 + 术语`` 出现多种译法且该差异
        没有语境例外/语境决定的依据时报告冲突。语境例外和语境决定允许的差异
        不计为冲突。目标文本是否实际使用确认译法另由 ``unused_terms`` 报告。
        """
        document = self._read()
        segments = {row.get("segment_id"): row for row in document.get("segments", [])}
        groups = {}
        conflicts = []
        unused = []
        for translation in document.get("translations", []):
            if translation.get("status") != TRANSLATION_CURRENT:
                continue
            if subject is not None and translation.get("subject") != subject:
                continue
            segment = segments.get(translation.get("segment_id"))
            if segment is None or segment.get("status") != SEGMENT_ACTIVE:
                continue
            for entry in translation.get("terms", []):
                if term is not None and _key(entry.get("term")) != _key(term):
                    continue
                if entry.get("translation") is None:
                    continue
                key = (translation.get("subject"), _key(entry.get("term")), entry.get("term"))
                group = groups.setdefault(
                    key,
                    {
                        "subject": translation.get("subject"),
                        "term": entry.get("term"),
                        "variants": {},
                        "allowed": set(),
                    },
                )
                group["variants"].setdefault(entry.get("translation"), []).append(
                    {
                        "translation_id": translation.get("translation_id"),
                        "segment_id": translation.get("segment_id"),
                        "material_id": translation.get("material_id"),
                        "context": translation.get("context", ""),
                        "source": entry.get("source"),
                    }
                )
                if entry.get("source") in ("exception", "context_decision"):
                    group["allowed"].add(entry["translation"])
                if not term_occurs(entry["translation"], translation.get("target_text", "")):
                    unused.append(
                        {
                            "subject": translation.get("subject"),
                            "term": entry.get("term"),
                            "expected_translation": entry["translation"],
                            "translation_id": translation.get("translation_id"),
                            "segment_id": translation.get("segment_id"),
                            "material_id": translation.get("material_id"),
                            "context": translation.get("context", ""),
                            "target_text": translation.get("target_text", ""),
                        }
                    )
                    conflicts.append(
                        {
                            "subject": translation.get("subject"),
                            "term": entry.get("term"),
                            "expected_translation": entry["translation"],
                            "found_translation": entry["translation"],
                            "reason": "target_text_mismatch",
                            "translation_id": translation.get("translation_id"),
                            "segment_id": translation.get("segment_id"),
                            "material_id": translation.get("material_id"),
                            "context": translation.get("context", ""),
                        }
                    )
        for group in groups.values():
            defaults = [
                row
                for row in document.get("term_decisions", [])
                if row.get("status") == DECISION_CONFIRMED
                and row.get("subject") == group["subject"]
                and _key(row.get("term")) == _key(group["term"])
            ]
            expected = next(
                (row.get("translation") for row in defaults if row.get("context") is None), None
            )
            allowed = set(group["allowed"])
            if expected is not None:
                allowed.add(expected)
            if not allowed:
                # 没有确认决定也没有例外时无法判定一致性，只要只有一种用法即视为一致。
                allowed = set(group["variants"])
            conflicting = sorted(value for value in group["variants"] if value not in allowed)
            group["conflicting_values"] = conflicting
            for value in conflicting:
                for usage in group["variants"][value]:
                    conflicts.append(
                        {
                            "subject": group["subject"],
                            "term": group["term"],
                            "expected_translation": expected,
                            "found_translation": value,
                            **usage,
                        }
                    )
        groups_output = []
        for group in groups.values():
            group["allowed"] = sorted(value for value in group["allowed"] if value)
            groups_output.append(group)
        groups_output.sort(key=lambda row: (row["subject"], _key(row["term"])))
        conflicts.sort(key=lambda row: (row["subject"], _key(row["term"]), row["translation_id"]))
        unused.sort(key=lambda row: (row["subject"], _key(row["term"]), row["translation_id"]))
        return {
            "consistent": not conflicts and not unused,
            "subject": subject,
            "term": term,
            "conflicts": conflicts,
            "unused_terms": unused,
            "groups": groups_output,
        }

    consistency_report = check_consistency

    # ------------------------------------------------------------------
    # 只读查询
    # ------------------------------------------------------------------
    def list_review_items(self, **kwargs):
        return self.pending_reviews(**kwargs)

    def list_derivations(self, material_id=None):
        rows = self._read().get("derivations", [])
        if material_id is not None:
            rows = [row for row in rows if row.get("material_id") == material_id]
        return copy.deepcopy(rows)
