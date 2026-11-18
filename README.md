# 课堂双语术语库

课堂双语术语库用于处理本地合成教研文本，保存来源版本、文本片段和处理记录，供校内复核使用。

## 环境

需要 Python 3.11 或更新版本。应用运行不依赖第三方包；运行测试和构建前，安装固定版本的开发工具：`python -m pip install -r requirements-dev.txt`。

## 运行

执行 `PYTHONPATH=src python -m nlp_classroom_translation_memory.cli ingest fixtures/records.json` 导入演练记录，再执行 `PYTHONPATH=src python -m nlp_classroom_translation_memory.cli list` 查看当前文本处理结果。运行数据保存在 `var/records.json`。

## 术语与翻译记忆模型

翻译记忆以**源文片段**为单位，而不是只按源句匹配：

| 概念 | 说明 |
| --- | --- |
| 材料 `material` | 课件、讲义或练习题，带学科（`subject`）。 |
| 片段 `segment` | 材料中的源文片段，带语境（`context`）和所属学科。 |
| 术语决定 `term_decision` | 教师确认的术语译法，按 `术语 + 学科 + 语境` 版本化；旧版本标记 `superseded`，不删除。 |
| 语境例外 `context_exception` | 教师为某个语境或某个片段接受的例外译法，独立保存，术语修订**永不覆盖**。 |
| 译文 `translation` | 教师确认的片段译文，快照当时生效的术语依据；按 `源文 + 学科 + 语境` 才可复用。 |
| 留痕 `derivation` | 拆段/合段的父子关系与原因。 |
| 待复核 `review_item` | 术语修订或拆/合段后需要教师处理的内容。 |
| 材料版本 `material_version` | 发布时的只读快照，历史版本不会被后续发布改写。 |

关键规则：

* **学科隔离**：同一句源文在不同学科下各自保存译文，查找时必须提供学科。
* **术语修订**：首次确认某术语建立基线；之后的修订会找出受影响的译文与材料，旧译文立即停止推荐，并生成待复核项（含期望译法）。修订回旧译法时，相关待复核项自动失效。
* **例外优先**：解析顺序为「片段例外 → 语境例外 → 学科例外 → 语境决定 → 学科默认决定」。命中例外的片段不受术语修订影响，也不会被要求复核。
* **拆段/合段**：父片段保留并标记 `split`/`merged`，`superseded_by` 指向去向；`derivation` 记录完整父子关系；旧译文保留为 `needs_review` 并生成带 `successor_segment_ids` 的复核项，不会静默丢失。
* **发布**：`publish` 生成不可变快照；快照不随草稿或术语修订变化，重新发布只追加新版本。

## 常用命令

```bash
state=/tmp/memory.json

# 材料与片段
PYTHONPATH=src python -m nlp_classroom_translation_memory.cli --state $state \
  material-create --subject 生物 --title 光合作用课件
PYTHONPATH=src python -m nlp_classroom_translation_memory.cli --state $state \
  segment-add --material <material_id> --text "光合作用需要光能。" --context 课堂

# 术语决定与语境例外
PYTHONPATH=src python -m nlp_classroom_translation_memory.cli --state $state \
  term-confirm --term 光合作用 --subject 生物 --translation photosynthesis --by 教师甲
PYTHONPATH=src python -m nlp_classroom_translation_memory.cli --state $state \
  term-exception --term 光 --subject 美术 --translation value --context 美术赏析 --by 教师乙

# 译文与查找（查找必须给学科）
PYTHONPATH=src python -m nlp_classroom_translation_memory.cli --state $state \
  translation-record --segment <segment_id> --text "Photosynthesis needs light."
PYTHONPATH=src python -m nlp_classroom_translation_memory.cli --state $state \
  lookup --text "光合作用需要光能。" --subject 生物 --context 课堂

# 拆段/合段与去向
PYTHONPATH=src python -m nlp_classroom_translation_memory.cli --state $state \
  segment-split --segment <segment_id> --child "第一句。" --child "第二句。"
PYTHONPATH=src python -m nlp_classroom_translation_memory.cli --state $state \
  segment-lineage --segment <segment_id>

# 复核、发布与一致性
PYTHONPATH=src python -m nlp_classroom_translation_memory.cli --state $state reviews
PYTHONPATH=src python -m nlp_classroom_translation_memory.cli --state $state publish --material <material_id>
PYTHONPATH=src python -m nlp_classroom_translation_memory.cli --state $state versions --material <material_id>
PYTHONPATH=src python -m nlp_classroom_translation_memory.cli --state $state check --subject 生物
```

## 测试

执行 `python -m pytest -q` 检查文本规范化、记录处理、状态读取、本地持久化，以及术语修订、语境例外、学科隔离、拆/合段留痕和发布历史。
