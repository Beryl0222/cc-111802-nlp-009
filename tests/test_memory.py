import tempfile
import unittest
from pathlib import Path

from nlp_classroom_translation_memory.memory import TranslationMemory
from nlp_classroom_translation_memory.repository import DocumentRepository


class MemoryTestCase(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "memory.json"
        self.memory = TranslationMemory(DocumentRepository(self.path))

    def make_segment(self, text, context="", subject="语文", material_type="courseware", translate=None, title=""):
        material = self.memory.create_material(
            subject, title=title or f"{subject}材料", material_type=material_type
        )
        segment = self.memory.add_segment(material["material_id"], text, context=context)
        if translate is not None:
            self.memory.record_translation(segment["segment_id"], translate, confirmed_by="教师甲")
        return material, segment


class TerminologyRevisionTests(MemoryTestCase):
    def test_revision_finds_affected_materials_and_stops_recommending_old_text(self):
        first_material, first = self.make_segment("光合作用需要光能。", context="课堂", translate="Light is needed.")
        second_material, second = self.make_segment(
            "光合作用的产物是糖。", context="练习", material_type="exercise", translate="Sugar is produced."
        )
        unrelated_material, _ = self.make_segment("细胞呼吸释放能量。", context="课堂", translate="Energy is released.")

        # 首次确认建立基线，不追溯制造复核噪音。
        baseline = self.memory.record_term_decision("光合作用", "语文", "photosynthesis", confirmed_by="教师甲")
        self.assertEqual(baseline["decision"]["version"], 1)
        self.assertEqual(baseline["affected_translations"], [])
        self.assertEqual(self.memory.pending_reviews(term="光合作用"), [])

        result = self.memory.record_term_decision(
            "光合作用", "语文", "photosynthesis process", confirmed_by="教师乙"
        )
        self.assertEqual(result["decision"]["version"], 2)
        self.assertEqual(result["previous"]["status"], "superseded")
        affected_ids = {row["material_id"] for row in result["affected_materials"]}
        self.assertEqual(affected_ids, {first_material["material_id"], second_material["material_id"]})
        self.assertNotIn(unrelated_material["material_id"], affected_ids)
        self.assertEqual(
            [row["segment_id"] for row in result["affected_translations"]],
            [first["segment_id"], second["segment_id"]],
        )

        # 旧译文不再被推荐。
        self.assertEqual(self.memory.lookup("光合作用需要光能。", "语文"), [])
        # 待复核项可定位到材料与片段，并给出期望译法。
        reviews = self.memory.pending_reviews(term="光合作用")
        self.assertEqual(len(reviews), 2)
        self.assertEqual({row["material_id"] for row in reviews}, affected_ids)
        self.assertEqual({row["expected_translation"] for row in reviews}, {"photosynthesis process"})

        # 复核后恢复推荐。
        review = next(
            row
            for row in reviews
            if row["translation_id"] == result["affected_translations"][0]["translation_id"]
        )
        self.memory.resolve_review(
            review["review_id"], reviewed_by="教师乙", target_text="Photosynthesis process needs light."
        )
        current = self.memory.lookup("光合作用需要光能。", "语文")
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["target_text"], "Photosynthesis process needs light.")
        self.assertEqual(current[0]["status"], "current")

    def test_term_revision_that_matches_existing_translation_does_not_create_review(self):
        _, segment = self.make_segment(
            "电解质溶于水。", context="课堂", subject="化学", translate="Electrolyte dissolves."
        )
        self.memory.record_term_decision("电解质", "化学", "electrolyte", confirmed_by="教师甲")
        result = self.memory.record_term_decision("电解质", "化学", "electrolyte", confirmed_by="教师甲")
        self.assertEqual(result["decision"]["version"], 2)
        self.assertEqual(result["affected_translations"], [])
        self.assertEqual(self.memory.pending_reviews(term="电解质"), [])
        self.assertEqual(len(self.memory.lookup("电解质溶于水。", "化学")), 1)

    def test_reverting_a_term_to_previous_wording_resolves_pending_review(self):
        _, _ = self.make_segment("光合作用需要光能。", context="课堂", translate="Light is needed.")
        self.memory.record_term_decision("光合作用", "语文", "photosynthesis")
        self.memory.record_term_decision("光合作用", "语文", "photosynthesis process")
        self.assertTrue(self.memory.pending_reviews(term="光合作用"))
        self.memory.record_term_decision("光合作用", "语文", "photosynthesis")
        self.assertEqual(self.memory.pending_reviews(term="光合作用"), [])
        self.assertEqual(len(self.memory.lookup("光合作用需要光能。", "语文")), 1)


class ContextExceptionTests(MemoryTestCase):
    def test_context_exception_survives_later_term_revision(self):
        _, segment = self.make_segment("光在艺术中表示明暗。", context="美术赏析", subject="美术", translate="Light means value.")
        self.memory.record_term_decision("光", "美术", "light", confirmed_by="教师甲")
        exception = self.memory.record_context_exception(
            "光", "value", "美术", context="美术赏析", confirmed_by="教师乙"
        )
        self.assertEqual(exception["exception"]["translation"], "value")

        self.memory.record_term_decision("光", "美术", "illumination", confirmed_by="教师甲")
        # 例外不被覆盖，且该片段不进入待复核。
        self.assertEqual(self.memory.pending_reviews(term="光"), [])
        current = self.memory.lookup("光在艺术中表示明暗。", "美术")
        self.assertEqual(len(current), 1)
        entry = next(row for row in current[0]["terms"] if row["term"] == "光")
        self.assertEqual(entry["translation"], "value")
        self.assertEqual(entry["source"], "exception")
        self.assertEqual(entry["exception_id"], exception["exception"]["exception_id"])
        exceptions = self.memory.list_context_exceptions(term="光", subject="美术")
        self.assertEqual([row["translation"] for row in exceptions], ["value"])

    def test_exception_can_scope_to_one_segment_only(self):
        material = self.memory.create_material("化学")
        special = self.memory.add_segment(material["material_id"], "该物质在反应中作催化剂。", context="实验")
        ordinary = self.memory.add_segment(material["material_id"], "催化剂降低活化能。", context="实验")
        self.memory.record_translation(special["segment_id"], "Special case.")
        self.memory.record_translation(ordinary["segment_id"], "Catalyst lowers activation energy.")
        self.memory.record_term_decision("催化剂", "化学", "catalyst")
        self.memory.record_context_exception(
            "催化剂", "catalytic agent", "化学", segment_id=special["segment_id"], confirmed_by="教师"
        )
        self.memory.record_term_decision("催化剂", "化学", "catalyzer")
        # 只有普通片段需要复核。
        reviews = self.memory.pending_reviews(term="催化剂")
        self.assertEqual([row["segment_id"] for row in reviews], [ordinary["segment_id"]])
        special_entry = next(
            row
            for row in self.memory.lookup("该物质在反应中作催化剂。", "化学")[0]["terms"]
            if row["term"] == "催化剂"
        )
        self.assertEqual(special_entry["translation"], "catalytic agent")

    def test_adding_exception_clears_a_review_created_by_the_term_revision(self):
        _, segment = self.make_segment("光在艺术中表示明暗。", context="美术赏析", subject="美术", translate="Light means value.")
        self.memory.record_term_decision("光", "美术", "light")
        self.memory.record_term_decision("光", "美术", "illumination")
        self.assertEqual(len(self.memory.pending_reviews(term="光")), 1)
        self.memory.record_context_exception(
            "光", "value", "美术", context="美术赏析", confirmed_by="教师乙"
        )
        translation = self.memory.get_translation(self.memory.lookup("光在艺术中表示明暗。", "美术")[0]["translation_id"])
        self.assertEqual(translation["status"], "current")
        review = self.memory.pending_reviews(term="光", status=None)[0]
        self.assertEqual(review["status"], "resolved")


class SubjectSeparationTests(MemoryTestCase):
    def test_same_source_text_in_different_subjects_has_separate_translations(self):
        _, chinese_segment = self.make_segment(
            "光在诗中表示希望。", context="课堂", subject="语文", translate="Light means hope."
        )
        _, physics_segment = self.make_segment(
            "光在诗中表示希望。", context="课堂", subject="物理", translate="Light carries energy."
        )
        self.memory.record_term_decision("光", "语文", "light (literary)")
        self.memory.record_term_decision("光", "物理", "light (optical)")

        chinese_matches = self.memory.lookup("光在诗中表示希望。", "语文")
        physics_matches = self.memory.lookup("光在诗中表示希望。", "物理")
        self.assertEqual(len(chinese_matches), 1)
        self.assertEqual(len(physics_matches), 1)
        self.assertEqual(chinese_matches[0]["target_text"], "Light means hope.")
        self.assertEqual(physics_matches[0]["target_text"], "Light carries energy.")
        self.assertNotEqual(chinese_matches[0]["translation_id"], physics_matches[0]["translation_id"])
        self.assertEqual(chinese_matches[0]["subject"], "语文")
        self.assertEqual(physics_matches[0]["subject"], "物理")
        self.assertNotEqual(chinese_segment["segment_id"], physics_segment["segment_id"])
        self.assertEqual(chinese_matches[0]["segment_id"], chinese_segment["segment_id"])
        self.assertEqual(physics_matches[0]["segment_id"], physics_segment["segment_id"])

        # 修订语文术语不影响物理材料。
        result = self.memory.record_term_decision("光", "语文", "light in poetry")
        affected_subjects = {row["subject"] for row in result["affected_materials"]}
        self.assertEqual(affected_subjects, {"语文"})
        self.assertEqual(len(self.memory.lookup("光在诗中表示希望。", "物理")), 1)

    def test_lookup_requires_a_subject(self):
        self.make_segment("同一句源文。", subject="语文", translate="Same source.")
        with self.assertRaises(ValueError):
            self.memory.lookup("同一句源文。", "")

    def test_context_partitions_translation_memory(self):
        self.make_segment("该词在课堂使用。", context="课堂", translate="Classroom usage.")
        self.make_segment("该词在练习使用。", context="练习", translate="Exercise usage.")
        classroom = self.memory.lookup("该词在课堂使用。", "语文", context="课堂")
        exercise = self.memory.lookup("该词在练习使用。", "语文", context="练习")
        self.assertEqual([row["target_text"] for row in classroom], ["Classroom usage."])
        self.assertEqual([row["target_text"] for row in exercise], ["Exercise usage."])


class PublishedVersionTests(MemoryTestCase):
    def test_published_snapshot_survives_term_revision_and_republish(self):
        material, segment = self.make_segment(
            "光合作用需要光能。", context="课堂", translate="Light is needed.", title="生物课件"
        )
        # 先建立术语基线，再发布，随后修订术语。
        self.memory.record_term_decision("光合作用", "语文", "photosynthesis")
        first = self.memory.publish_material(material["material_id"], published_by="教师甲")
        self.assertEqual(first["version"], 1)

        revision = self.memory.record_term_decision("光合作用", "语文", "photosynthesis process")
        self.assertEqual(
            revision["affected_published_versions"],
            [{"material_id": material["material_id"], "version": 1, "segment_ids": [segment["segment_id"]]}],
        )
        # 快照内容不变。
        stored = self.memory.get_material_version(material["material_id"], 1)
        self.assertEqual(stored["segments"][0]["target_text"], "Light is needed.")

        review = self.memory.pending_reviews(term="光合作用")[0]
        self.memory.resolve_review(review["review_id"], target_text="Photosynthesis needs light.")
        second = self.memory.publish_material(material["material_id"], published_by="教师乙")
        self.assertEqual(second["version"], 2)
        versions = self.memory.list_material_versions(material["material_id"])
        self.assertEqual([row["version"] for row in versions], [1, 2])
        self.assertEqual(versions[0]["segments"][0]["target_text"], "Light is needed.")
        self.assertEqual(versions[1]["segments"][0]["target_text"], "Photosynthesis needs light.")
        # 历史版本只读：后续再发布不会改写它们。
        self.memory.publish_material(material["material_id"])
        self.assertEqual(
            self.memory.get_material_version(material["material_id"], 1)["segments"][0]["target_text"],
            "Light is needed.",
        )

    def test_publishing_is_blocked_while_translations_need_review(self):
        material, segment = self.make_segment("光合作用需要光能。", translate="Light is needed.")
        self.memory.record_term_decision("光合作用", "语文", "photosynthesis")
        self.memory.record_term_decision("光合作用", "语文", "photosynthesis process")
        with self.assertRaises(ValueError):
            self.memory.publish_material(material["material_id"])
        self.assertEqual(self.memory.list_material_versions(material["material_id"]), [])
        # 显式允许时可发布，但快照标记未复核片段。
        snapshot = self.memory.publish_material(
            material["material_id"], allow_pending_reviews=True
        )
        self.assertEqual(snapshot["stale_segment_ids"], [segment["segment_id"]])
        self.assertEqual(snapshot["segments"][0]["translation_status"], "needs_review")

    def test_draft_changes_do_not_alter_published_version(self):
        material, segment = self.make_segment("第一版片段。", translate="First.")
        published = self.memory.publish_material(material["material_id"])
        second = self.memory.add_segment(material["material_id"], "第二版片段。")
        self.memory.record_translation(second["segment_id"], "Second.")
        snapshot = self.memory.get_material_version(material["material_id"], 1)
        self.assertEqual([row["text"] for row in snapshot["segments"]], ["第一版片段。"])
        view = self.memory.get_material(material["material_id"])
        self.assertTrue(view["has_unpublished_changes"])
        self.assertEqual(view["published_revision"], published["revision"])
        self.assertGreater(view["revision"], view["published_revision"])


    def test_published_version_points_to_successors_after_split(self):
        material, segment = self.make_segment("第一句。第二句。", translate="First. Second.")
        self.memory.publish_material(material["material_id"])
        split = self.memory.split_segment(
            segment["segment_id"], [{"text": "第一句。"}, {"text": "第二句。"}]
        )
        explanation = self.memory.explain_version_references(material["material_id"], 1)
        reference = explanation["references"][0]
        self.assertEqual(reference["segment_id"], segment["segment_id"])
        self.assertEqual(reference["status"], "split")
        self.assertEqual(
            reference["current_segment_ids"],
            sorted(row["segment_id"] for row in split["children"]),
        )
        self.assertEqual(reference["derivation"]["operation"], "split")
        # 已发布的正文保持不变。
        self.assertEqual(
            self.memory.get_material_version(material["material_id"], 1)["segments"][0]["text"],
            "第一句。第二句。",
        )


class SegmentationTests(MemoryTestCase):
    def test_split_retains_parent_and_child_lineage(self):
        material, segment = self.make_segment("第一句。第二句。", context="课堂", translate="First. Second.")
        result = self.memory.split_segment(segment["segment_id"], [{"text": "第一句。"}, {"text": "第二句。"}])
        parent = result["parent"]
        self.assertEqual(parent["status"], "split")
        self.assertEqual(len(parent["superseded_by"]), 2)
        self.assertEqual(len(result["children"]), 2)
        for child in result["children"]:
            self.assertEqual(child["derived_from"], [segment["segment_id"]])
        lineage = self.memory.segment_lineage(segment["segment_id"])
        self.assertEqual(lineage["status"], "split")
        self.assertEqual([row["segment_id"] for row in lineage["descendants"]], parent["superseded_by"])
        self.assertEqual(lineage["derivations"][0]["operation"], "split")

        # 旧译文仍可追溯，但不再推荐；复核项说明去向。
        self.assertEqual(self.memory.lookup("第一句。第二句。", "语文"), [])
        old = self.memory.list_translations(segment_id=segment["segment_id"])[0]
        self.assertEqual(old["status"], "needs_review")
        review = self.memory.pending_reviews(material_id=material["material_id"])[0]
        self.assertEqual(review["kind"], "segment_split")
        self.assertEqual(sorted(review["successor_segment_ids"]), sorted(parent["superseded_by"]))

        # 子片段可以独立翻译。
        self.memory.record_translation(result["children"][0]["segment_id"], "First.")
        self.assertEqual(len(self.memory.lookup("第一句。", "语文")), 1)

    def test_merge_retains_all_parents_and_their_destinations(self):
        material = self.memory.create_material("语文")
        first = self.memory.add_segment(material["material_id"], "第一段。", context="课堂")
        second = self.memory.add_segment(material["material_id"], "第二段。", context="课堂")
        self.memory.record_translation(first["segment_id"], "First.")
        self.memory.record_translation(second["segment_id"], "Second.")
        result = self.memory.merge_segments(
            [first["segment_id"], second["segment_id"]], text="第一段。第二段。", context="课堂"
        )
        merged = result["segment"]
        self.assertEqual(
            sorted(merged["derived_from"]), sorted([first["segment_id"], second["segment_id"]])
        )
        for parent in result["parents"]:
            self.assertEqual(parent["status"], "merged")
            self.assertEqual(parent["superseded_by"], [merged["segment_id"]])
            lineage = self.memory.segment_lineage(parent["segment_id"])
            self.assertEqual([row["segment_id"] for row in lineage["descendants"]], [merged["segment_id"]])
        self.assertEqual(self.memory.lookup("第一段。", "语文"), [])
        self.assertEqual(self.memory.lookup("第二段。", "语文"), [])
        reviews = self.memory.pending_reviews(material_id=material["material_id"])
        self.assertEqual({row["kind"] for row in reviews}, {"segment_merge"})
        self.assertEqual(len(reviews), 2)

    def test_structural_review_cannot_revive_stale_parent_translation(self):
        material, segment = self.make_segment("第一句。第二句。", translate="First. Second.")
        self.memory.split_segment(segment["segment_id"], [{"text": "第一句。"}, {"text": "第二句。"}])
        review = self.memory.pending_reviews(material_id=material["material_id"])[0]
        with self.assertRaises(ValueError):
            self.memory.resolve_review(review["review_id"], target_text="First. Second.")
        self.memory.resolve_review(review["review_id"], resolution="kept")
        old = self.memory.list_translations(segment_id=segment["segment_id"])[0]
        self.assertEqual(old["status"], "needs_review")
        self.assertEqual(self.memory.lookup("第一句。第二句。", "语文"), [])

    def test_cannot_split_or_merge_inactive_segments(self):
        _, segment = self.make_segment("甲。乙。")
        result = self.memory.split_segment(segment["segment_id"], [{"text": "甲。"}])
        with self.assertRaises(ValueError):
            self.memory.split_segment(segment["segment_id"], [{"text": "丙。"}])
        child = result["children"][0]
        with self.assertRaises(ValueError):
            self.memory.merge_segments([child["segment_id"], segment["segment_id"]])


class TermMatchingTests(MemoryTestCase):
    def test_overlapping_chinese_terms_are_matched_by_occurrence(self):
        material = self.memory.create_material("语文")
        both = self.memory.add_segment(material["material_id"], "光合作用需要光。")
        only_long = self.memory.add_segment(material["material_id"], "光合作用很强。")
        only_short = self.memory.add_segment(material["material_id"], "这里有光。")
        self.memory.record_term_decision("光", "语文", "light")
        self.memory.record_term_decision("光合作用", "语文", "photosynthesis")

        def terms(segment):
            return [row["term"] for row in self.memory.resolve_segment_terms(segment["segment_id"])["terms"]]

        self.assertEqual(terms(both), ["光", "光合作用"])
        self.assertEqual(terms(only_long), ["光合作用"])
        self.assertEqual(terms(only_short), ["光"])

    def test_latin_term_does_not_match_inside_a_longer_word(self):
        material = self.memory.create_material("英语")
        inside = self.memory.add_segment(material["material_id"], "The part is missing.")
        standalone = self.memory.add_segment(material["material_id"], "We study art today.")
        self.memory.record_term_decision("art", "英语", "艺术")
        self.assertEqual(self.memory.resolve_segment_terms(inside["segment_id"])["terms"], [])
        self.assertEqual(
            [row["term"] for row in self.memory.resolve_segment_terms(standalone["segment_id"])["terms"]],
            ["art"],
        )


class ConsistencyTests(MemoryTestCase):
    def test_consistency_reports_disallowed_variant_but_allows_context_decisions(self):
        material = self.memory.create_material("生物")
        first = self.memory.add_segment(material["material_id"], "细胞膜控制物质进出。", context="课堂")
        second = self.memory.add_segment(material["material_id"], "细胞膜具有选择透过性。", context="实验")
        self.memory.record_term_decision("细胞膜", "生物", "cell membrane")
        self.memory.record_translation(first["segment_id"], "The cell membrane controls entry.")
        self.memory.record_translation(second["segment_id"], "The cell wall is selective.")
        report = self.memory.check_consistency(subject="生物", term="细胞膜")
        self.assertFalse(report["consistent"])
        self.assertEqual(len(report["conflicts"]), 1)
        self.assertEqual(report["conflicts"][0]["expected_translation"], "cell membrane")
        self.assertEqual(report["conflicts"][0]["reason"], "target_text_mismatch")

        # 先前的译文与决定一致，但不是最新决定时不会被误报为冲突。
        self.memory.record_translation(second["segment_id"], "The cell membrane is selective.")
        report = self.memory.check_consistency(subject="生物", term="细胞膜")
        self.assertTrue(report["consistent"])

    def test_context_exception_variant_is_allowed_not_a_conflict(self):
        material = self.memory.create_material("美术")
        first = self.memory.add_segment(material["material_id"], "光在艺术中表示明暗。", context="美术赏析")
        second = self.memory.add_segment(material["material_id"], "光沿直线传播。", context="物理实验")
        self.memory.record_term_decision("光", "美术", "light")
        self.memory.record_context_exception("光", "value", "美术", context="美术赏析")
        self.memory.record_translation(first["segment_id"], "Light means value.")
        self.memory.record_translation(second["segment_id"], "Light travels in straight lines.")
        report = self.memory.check_consistency(subject="美术", term="光")
        self.assertTrue(report["consistent"])
        self.assertEqual(report["conflicts"], [])


class MultiTermTests(MemoryTestCase):
    def test_one_resolved_review_does_not_release_a_translation_with_other_pending_terms(self):
        material = self.memory.create_material("生物")
        segment = self.memory.add_segment(material["material_id"], "细胞膜与叶绿体共同作用。", context="课堂")
        self.memory.record_translation(segment["segment_id"], "Membrane and chloroplast.")
        self.memory.record_term_decision("细胞膜", "生物", "cell membrane")
        self.memory.record_term_decision("叶绿体", "生物", "chloroplast")
        self.memory.record_term_decision("细胞膜", "生物", "plasma membrane")
        self.memory.record_term_decision("叶绿体", "生物", "chloroplast organelle")
        self.assertEqual(len(self.memory.pending_reviews()), 2)

        self.memory.record_context_exception("细胞膜", "membrane", "生物", context="课堂")
        # 细胞膜一项解除，但叶绿体仍待复核：整条译文不能被推荐。
        self.assertEqual(
            [(row["term"], row["status"]) for row in self.memory.pending_reviews(status=None)],
            [("细胞膜", "resolved"), ("叶绿体", "pending")],
        )
        translation = self.memory.list_translations(segment_id=segment["segment_id"])[0]
        self.assertEqual(translation["status"], "needs_review")
        self.assertEqual(self.memory.lookup("细胞膜与叶绿体共同作用。", "生物"), [])

        # 复核剩余术语后恢复推荐。
        remaining = self.memory.pending_reviews(status="pending")[0]
        self.memory.resolve_review(
            remaining["review_id"], target_text="Cell membrane and chloroplast organelle work together."
        )
        self.assertEqual(len(self.memory.lookup("细胞膜与叶绿体共同作用。", "生物")), 1)


class AtomicityTests(MemoryTestCase):
    def test_term_revision_creates_review_for_segment_without_translation(self):
        material = self.memory.create_material("化学")
        segment = self.memory.add_segment(material["material_id"], "催化剂降低活化能。", context="课堂")
        self.memory.record_term_decision("催化剂", "化学", "catalyst")
        self.memory.record_term_decision("催化剂", "化学", "catalyzer")
        reviews = self.memory.pending_reviews(term="催化剂")
        self.assertEqual([row["kind"] for row in reviews], ["term_revision_missing_translation"])
        self.assertEqual(reviews[0]["segment_id"], segment["segment_id"])
        self.assertEqual(reviews[0]["previous_translation"], "catalyst")
        self.memory.resolve_review(
            reviews[0]["review_id"], target_text="Catalyzer lowers activation energy."
        )
        self.assertEqual(len(self.memory.lookup("催化剂降低活化能。", "化学")), 1)

    def test_failed_term_decision_does_not_write_partial_state(self):
        material, segment = self.make_segment("光合作用需要光能。", translate="Light is needed.")
        before = (self.path.read_text(encoding="utf-8") if self.path.exists() else None)
        with self.assertRaises(ValueError):
            self.memory.record_term_decision("光合作用", "", "photosynthesis")
        self.assertEqual(self.path.read_text(encoding="utf-8") if self.path.exists() else None, before)
        self.assertEqual(len(self.memory.list_translations()), 1)
        self.assertEqual(len(self.memory.lookup("光合作用需要光能。", "语文")), 1)

    def test_repository_rejects_corrupt_document_without_overwriting(self):
        self.path.write_text("[]", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.memory.list_materials()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "[]")


if __name__ == "__main__":
    unittest.main()
