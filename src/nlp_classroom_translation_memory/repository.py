import json
from pathlib import Path


DOCUMENT_COLLECTIONS = (
    "segments",
    "derivations",
    "term_decisions",
    "context_exceptions",
    "translations",
    "materials",
    "material_versions",
    "review_items",
)


class DocumentRepository:
    """JSON document store for the terminology and translation-memory workflow.

    The collection shape is deliberately explicit so that an empty store can be
    created without a migration step. Unknown top-level keys are preserved when
    reading and writing, which leaves room for future schema additions.
    """

    def __init__(self, path):
        self.path = Path(path)

    @staticmethod
    def empty_document():
        return {name: [] for name in DOCUMENT_COLLECTIONS}

    def read(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self.empty_document()
        if not isinstance(value, dict):
            raise ValueError("翻译记忆库格式无效")
        document = dict(value)
        for name in DOCUMENT_COLLECTIONS:
            collection = document.setdefault(name, [])
            if not isinstance(collection, list):
                raise ValueError(f"翻译记忆库集合无效: {name}")
        return document

    # Alternate names make the repository usable as a small storage adapter
    # without coupling the domain service to this module's terminology.
    read_document = read
    read_state = read

    def write(self, document):
        if not isinstance(document, dict):
            raise TypeError("翻译记忆库状态必须是字典")
        value = dict(document)
        for name in DOCUMENT_COLLECTIONS:
            if not isinstance(value.get(name, []), list):
                raise ValueError(f"翻译记忆库集合无效: {name}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    write_document = write
    write_state = write


class RecordRepository:
    """演练记录存储。

    与 :class:`DocumentRepository` 共用同一个 JSON 文件：读写时只触碰
    ``records`` 键，其它集合（术语决定、译文、历史版本等）原样保留。
    """

    def __init__(self, path):
        self.path = Path(path)

    def _read_document(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict):
            raise ValueError("记录库格式无效")
        return value

    def read(self):
        value = self._read_document()
        records = value.get("records", [])
        if not isinstance(records, list):
            raise ValueError("记录库格式无效")
        return records

    def replace(self, records):
        value = self._read_document()
        value["records"] = records
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)
