import argparse
import json
from pathlib import Path

from .memory import TranslationMemory
from .repository import DocumentRepository, RecordRepository
from .workflow import TextWorkflow


def _build_parser():
    parser = argparse.ArgumentParser(description="本地教研文本处理工具")
    parser.add_argument("--state", default="var/records.json", help="数据文件位置")
    subparsers = parser.add_subparsers(dest="action", required=True)

    # 兼容原有的文本记录导入/查看流程。
    ingest = subparsers.add_parser("ingest", help="导入演练记录")
    ingest.add_argument("path", nargs="?", default="fixtures/records.json")
    listing = subparsers.add_parser("list", help="查看文本处理结果")
    listing.add_argument("--source", default=None)

    material = subparsers.add_parser("material-create", help="新建材料（课件/讲义/练习题）")
    material.add_argument("--subject", required=True)
    material.add_argument("--title", default="")
    material.add_argument("--type", default="courseware")
    material.add_argument("--id", default=None)

    segment = subparsers.add_parser("segment-add", help="为材料添加源文片段")
    segment.add_argument("--material", required=True)
    segment.add_argument("--text", required=True)
    segment.add_argument("--context", default="")

    term = subparsers.add_parser("term-confirm", help="记录教师确认的术语译法")
    term.add_argument("--term", required=True)
    term.add_argument("--subject", required=True)
    term.add_argument("--translation", required=True)
    term.add_argument("--context", default=None)
    term.add_argument("--by", default="")

    exception = subparsers.add_parser("term-exception", help="为某个语境确认例外译法")
    exception.add_argument("--term", required=True)
    exception.add_argument("--subject", required=True)
    exception.add_argument("--translation", required=True)
    exception.add_argument("--context", default=None)
    exception.add_argument("--segment", default=None)
    exception.add_argument("--by", default="")

    translation = subparsers.add_parser("translation-record", help="记录片段译文")
    translation.add_argument("--segment", required=True)
    translation.add_argument("--text", required=True)
    translation.add_argument("--by", default="")

    lookup = subparsers.add_parser("lookup", help="按源文、学科和语境查找译文")
    lookup.add_argument("--text", required=True)
    lookup.add_argument("--subject", required=True)
    lookup.add_argument("--context", default=None)

    split = subparsers.add_parser("segment-split", help="拆分源文片段")
    split.add_argument("--segment", required=True)
    split.add_argument("--child", action="append", required=True, help="子片段正文，可重复")

    merge = subparsers.add_parser("segment-merge", help="合并源文片段")
    merge.add_argument("--segment", action="append", required=True)
    merge.add_argument("--text", default=None)
    merge.add_argument("--segment-id", default=None)

    lineage = subparsers.add_parser("segment-lineage", help="查看片段的拆/合段去向")
    lineage.add_argument("--segment", required=True)

    publish = subparsers.add_parser("publish", help="发布材料并保留历史版本")
    publish.add_argument("--material", required=True)
    publish.add_argument("--by", default="")
    publish.add_argument("--note", default="")
    publish.add_argument(
        "--allow-pending-reviews", action="store_true", help="存在待复核内容时仍然发布"
    )

    versions = subparsers.add_parser("versions", help="查看材料历史版本")
    versions.add_argument("--material", default=None)
    versions.add_argument("--version", type=int, default=None, help="查看某版本的片段去向")

    reviews = subparsers.add_parser("reviews", help="查看待复核内容")
    reviews.add_argument("--material", default=None)
    reviews.add_argument("--subject", default=None)
    reviews.add_argument("--term", default=None)
    reviews.add_argument("--status", default="pending")

    resolve = subparsers.add_parser("review-resolve", help="处理一项待复核内容")
    resolve.add_argument("--review", required=True)
    resolve.add_argument("--by", default="")
    resolve.add_argument("--resolution", default="updated")
    resolve.add_argument("--text", default=None, help="复核后的新译文")
    resolve.add_argument("--note", default="")

    consistency = subparsers.add_parser("check", help="检查同学科术语译法一致性")
    consistency.add_argument("--subject", default=None)
    consistency.add_argument("--term", default=None)
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    if args.action in ("ingest", "list"):
        workflow = TextWorkflow(RecordRepository(args.state))
        if args.action == "ingest":
            result = workflow.ingest(json.loads(Path(args.path).read_text(encoding="utf-8")))
        else:
            result = workflow.list_records(args.source)
    else:
        memory = TranslationMemory(DocumentRepository(args.state))
        if args.action == "material-create":
            result = memory.create_material(
                args.subject, title=args.title, material_type=args.type, material_id=args.id
            )
        elif args.action == "segment-add":
            result = memory.add_segment(args.material, args.text, context=args.context)
        elif args.action == "term-confirm":
            result = memory.record_term_decision(
                args.term, args.subject, args.translation, context=args.context, confirmed_by=args.by
            )
        elif args.action == "term-exception":
            result = memory.record_context_exception(
                args.term,
                args.translation,
                args.subject,
                context=args.context,
                segment_id=args.segment,
                confirmed_by=args.by,
            )
        elif args.action == "translation-record":
            result = memory.record_translation(args.segment, args.text, confirmed_by=args.by)
        elif args.action == "lookup":
            result = memory.lookup(args.text, args.subject, context=args.context)
        elif args.action == "segment-split":
            result = memory.split_segment(
                args.segment, [{"text": text} for text in args.child]
            )
        elif args.action == "segment-merge":
            result = memory.merge_segments(
                args.segment, text=args.text, segment_id=args.segment_id
            )
        elif args.action == "segment-lineage":
            result = memory.segment_lineage(args.segment)
        elif args.action == "publish":
            result = memory.publish_material(
                args.material,
                published_by=args.by,
                note=args.note,
                allow_pending_reviews=args.allow_pending_reviews,
            )
        elif args.action == "versions":
            if args.material is not None and args.version is not None:
                result = memory.explain_version_references(args.material, args.version)
            else:
                result = memory.list_material_versions(args.material)
        elif args.action == "reviews":
            result = memory.pending_reviews(
                material_id=args.material, subject=args.subject, term=args.term, status=args.status
            )
        elif args.action == "review-resolve":
            result = memory.resolve_review(
                args.review,
                reviewed_by=args.by,
                resolution=args.resolution,
                target_text=args.text,
                note=args.note,
            )
        elif args.action == "check":
            result = memory.check_consistency(subject=args.subject, term=args.term)
        else:  # pragma: no cover - argparse 已限制取值范围
            raise ValueError(f"未知操作: {args.action}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
