"""Regression tests for the knowledge-base follow-ups to PR #3.

All knowledge bases, Git repositories and subtitle files live in temporary directories.
Run: python -m unittest discover -s skills/translate-srt/tests -v
"""
import contextlib
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import kb_tools as kb
import srt_tools as srt


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="translate-srt-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {
            "TRANSLATE_SRT_HOME": str(self.root),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "KB Test",
            "GIT_AUTHOR_EMAIL": "kb-test@example.invalid",
            "GIT_COMMITTER_NAME": "KB Test",
            "GIT_COMMITTER_EMAIL": "kb-test@example.invalid",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.invoke(kb.init, str(self.root))
        self.database = self.root / "knowledge"

    @staticmethod
    def invoke(function, *args, **kwargs):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = function(*args, **kwargs)
        return result, output.getvalue()

    @staticmethod
    def alias(canonical="CanonicalName", variants=(), mode="ask", translation="标准译名"):
        result = {"canonical": canonical, "asr_variants": list(variants), "domain": "seiyuu",
                  "type": "person", "translation": translation}
        if mode is not None:
            result["mode"] = mode
        return result

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.database), *args], check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout

    def cli(self, *args):
        return subprocess.run([sys.executable, "-X", "utf8", "-B", str(SCRIPTS / "kb_tools.py"), *args],
                              capture_output=True, text=True, encoding="utf-8")

    def apply(self, proposal, **kwargs):
        path = self.root / "proposal.json"
        path.write_text(json.dumps(proposal, ensure_ascii=False), encoding="utf-8")
        return self.invoke(kb.apply_proposal, str(path), **kwargs)

    def files(self):
        return {p.relative_to(self.database).as_posix(): p.read_bytes()
                for p in self.database.rglob("*")
                if p.is_file() and ".git" not in p.relative_to(self.database).parts}

    def subtitle(self, body="CanonicalName", name="sample.srt"):
        path = self.root / name
        path.write_text("1\n00:00:00,000 --> 00:00:02,000\n" + body + "\n", encoding="utf-8")
        return path

    def read_log(self, path):
        with path.open(encoding="utf-8", newline="") as stream:
            return list(csv.DictReader(stream, delimiter="\t"))

    def replace(self, body):
        source = self.subtitle(body)
        output, log = self.root / "replaced.srt", self.root / "alias_log.tsv"
        rc, _ = self.invoke(kb.replace, str(source), str(output), log=str(log))
        self.assertEqual(rc, 0)
        return srt.parse(output.read_text(encoding="utf-8"))[0]["text"], self.read_log(log)

    def test_invalid_candidate_leaves_files_index_and_commit_unchanged(self):
        self.apply({"aliases": [self.alias("AlphaBeta")]}, commit=True)
        style = self.database / "seiyuu/style.md"
        style.write_text(style.read_text(encoding="utf-8") + "\n- Pending personal edit\n", encoding="utf-8")
        self.git("add", "seiyuu/style.md")
        before, head = self.files(), self.git("rev-parse", "HEAD")
        index = (self.database / ".git/index").read_bytes()
        summary = self.root / "result.md"
        rc, output = self.apply({
            "index": [{"domain": "new-domain"}],
            "aliases": [self.alias("GammaPerson", ["Alpha"], "auto")],
            "entities": [{"domain": "new-domain", "name": "NewEntity", "translation": "新实体"}],
        }, commit=True, summary_out=str(summary))
        self.assertEqual(rc, 2)
        self.assertIn("fails validation", output)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual((self.database / ".git/index").read_bytes(), index)
        self.assertIn("沉淀失败", summary.read_text(encoding="utf-8"))

    def test_malformed_proposal_has_no_partial_writes(self):
        before = self.files()
        rc, _ = self.apply({"aliases": [self.alias()], "entities": [{"domain": "seiyuu"}]})
        self.assertEqual(rc, 2)
        self.assertEqual(self.files(), before)

    def test_publish_failure_restores_files_and_removes_new_directory(self):
        before = self.files()
        original_replace = kb._replace_bytes

        def fail_new_glossary(path, data):
            if path.parent.name == "new-domain" and path.name == "glossary.md":
                raise OSError("simulated write failure")
            return original_replace(path, data)

        with patch.object(kb, "_replace_bytes", side_effect=fail_new_glossary):
            rc, output = self.apply({"index": [{"domain": "new-domain"}], "aliases": [self.alias()]})
        self.assertEqual(rc, 2)
        self.assertIn("simulated write failure", output)
        self.assertEqual(self.files(), before)
        self.assertFalse((self.database / "new-domain").exists())

    def test_domain_cannot_escape_candidate_directory(self):
        before = self.files()
        rc, output = self.apply({"index": [{"domain": "../escape"}]})
        self.assertEqual(rc, 2)
        self.assertIn("invalid domain", output)
        self.assertFalse((self.root / "escape").exists())
        self.assertEqual(self.files(), before)

    def test_valid_candidate_is_committed(self):
        before = self.git("rev-parse", "HEAD")
        rc, _ = self.apply({"aliases": [self.alias()]}, commit=True)
        self.assertEqual(rc, 0)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), before)
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(kb.check(self.database, quiet=True), 0)

    def test_commit_failure_is_reported_as_failure(self):
        original_git = kb._git

        def fail_commit(database, *args, **kwargs):
            if args[0] == "commit":
                return subprocess.CompletedProcess(args, 1, "", "test commit failure")
            return original_git(database, *args, **kwargs)

        with patch.object(kb, "_git", side_effect=fail_commit):
            rc, output = self.apply({"aliases": [self.alias()]}, commit=True)
        self.assertEqual(rc, 2)
        self.assertIn("commit failed", output)
        self.assertNotEqual(self.git("status", "--porcelain"), "")

    def test_new_ask_variant_downgrades_auto_row(self):
        self.apply({"aliases": [self.alias(variants=["KnownTypo"], mode="auto")]})
        self.apply({"aliases": [self.alias(variants=["NewTypo"], mode="ask")]})
        self.assertEqual(kb.load_aliases(self.database)[0]["mode"], "ask")
        text, log = self.replace("NewTypo KnownTypo")
        self.assertEqual(text, "NewTypo KnownTypo")
        self.assertEqual([row["status"] for row in log], ["ask", "ask"])

    def test_new_variant_without_mode_also_requires_confirmation(self):
        self.apply({"aliases": [self.alias(variants=["KnownTypo"], mode="auto")]})
        self.apply({"aliases": [self.alias(variants=["NewTypo"], mode=None)]})
        self.assertEqual(kb.load_aliases(self.database)[0]["mode"], "ask")
        self.assertEqual(self.replace("NewTypo")[0], "NewTypo")

    def test_explicit_confirmation_can_promote_row_again(self):
        self.apply({"aliases": [self.alias(variants=["KnownTypo"], mode="auto")]})
        self.apply({"aliases": [self.alias(variants=["NewTypo"])]})
        self.apply({"aliases": [self.alias(mode="auto")]})
        self.assertEqual(self.replace("NewTypo")[0], "CanonicalName")

    def test_long_ask_match_protects_its_short_auto_substring(self):
        self.apply({"aliases": [self.alias("FirstCanonical", ["ABCD"], "auto"),
                                self.alias("SecondCanonical", ["XABCDE"], "ask")]})
        text, log = self.replace("XABCDE ABCD")
        self.assertEqual(text, "XABCDE FirstCanonical")
        self.assertEqual([(r["variant"], r["status"]) for r in log],
                         [("XABCDE", "ask"), ("ABCD", "auto")])
        hits = kb.match_entries([{"text": "XABCDE"}], kb.build_patterns(self.database))
        self.assertEqual(set(hits), {"SecondCanonical"})

    def test_long_correct_entity_also_protects_auto_substring(self):
        self.apply({"aliases": [self.alias("FirstCanonical", ["ABCD"], "auto")],
                    "entities": [{"domain": "seiyuu", "name": "XABCDE", "translation": "正确专名"}]})
        text, log = self.replace("XABCDE")
        self.assertEqual(text, "XABCDE")
        self.assertEqual(log, [])

    def test_replacement_does_not_scan_its_own_output_for_ask_variants(self):
        self.apply({"aliases": [self.alias("LongCorrectName", ["BadName"], "auto"),
                                self.alias("OtherCanonical", ["CorrectName"], "ask")]})
        text, log = self.replace("BadName")
        self.assertEqual(text, "LongCorrectName")
        self.assertEqual([r["status"] for r in log], ["auto"])

    def test_diff_includes_new_domains_staged_and_unstaged_bodies(self):
        self.apply({"index": [{"domain": "cs"}], "entities": [{
            "domain": "cs", "name": "Valve", "translation": "Valve", "summary": "NEW_DOMAIN_REVIEW_TEXT",
        }]})
        rc, output = self.invoke(kb.diff)
        self.assertEqual(rc, 0)
        self.assertIn("NEW_DOMAIN_REVIEW_TEXT", output)
        self.git("add", "-A")
        entity = self.database / "cs/entities.md"
        entity.write_text(entity.read_text(encoding="utf-8") + "\nUNSTAGED_REVIEW_TEXT\n", encoding="utf-8")
        index = (self.database / ".git/index").read_bytes()
        rc, output = self.invoke(kb.diff)
        self.assertEqual(rc, 0)
        self.assertIn("NEW_DOMAIN_REVIEW_TEXT", output)
        self.assertIn("UNSTAGED_REVIEW_TEXT", output)
        self.assertEqual((self.database / ".git/index").read_bytes(), index)

    def test_diff_reads_unicode_untracked_paths_with_spaces(self):
        (self.database / "补充 notes.md").write_text("需要审查的正文\n", encoding="utf-8")
        rc, output = self.invoke(kb.diff)
        self.assertEqual(rc, 0)
        self.assertIn("补充 notes.md", output)
        self.assertIn("+需要审查的正文", output)
        self.assertIn("??", self.git("status", "--short"))

    def create_log_before_split(self):
        self.apply({"aliases": [self.alias(variants=["MisspelledName"], mode="auto")]})
        source = self.root / "source_fix.srt"
        source.write_text("1\n00:00:00,000 --> 00:00:20,000\nこれは一文目です。これは二文目です。これは三文目です。\n\n"
                          "2\n00:00:20,000 --> 00:00:22,000\nMisspelledNameです。\n", encoding="utf-8")
        log = self.root / "alias_log.tsv"
        self.invoke(kb.replace, str(source), log=str(log))
        entries = srt.process(source.read_text(encoding="utf-8"), "split", lang="ja",
                              max_duration=12, min_duration=0)
        source.write_text(srt.serialize(entries), encoding="utf-8")
        return source, log, entries

    def test_log_remaps_after_split_and_keeps_immutable_context(self):
        source, log, _ = self.create_log_before_split()
        rc, _ = self.invoke(kb.remap_log, str(source), str(log))
        self.assertEqual(rc, 0)
        row = self.read_log(log)[0]
        self.assertEqual(row["entry"], "2")
        self.assertEqual(row["current_entries"], "4")
        self.assertEqual(row["start_ms"], "20000")
        self.assertEqual(row["source_text"], "MisspelledNameです。")
        before = log.read_bytes()
        self.invoke(kb.remap_log, str(source), str(log))
        self.assertEqual(log.read_bytes(), before)

    def test_log_remaps_after_merge(self):
        source, log, entries = self.create_log_before_split()
        merged = [{"start": 0, "end": 22000, "text": "".join(entry["text"] for entry in entries)}]
        source.write_text(srt.serialize(merged), encoding="utf-8")
        rc, _ = self.invoke(kb.remap_log, str(source), str(log))
        self.assertEqual(rc, 0)
        self.assertEqual(self.read_log(log)[0]["current_entries"], "1")

    def test_log_keeps_all_candidates_when_one_source_entry_is_split(self):
        self.apply({"aliases": [self.alias(variants=["MisspelledName"], mode="auto")]})
        source = self.root / "source_fix.srt"
        source.write_text("1\n00:00:00,000 --> 00:00:20,000\n"
                          "これは一文目です。MisspelledNameです。これは三文目です。\n", encoding="utf-8")
        log = self.root / "alias_log.tsv"
        self.invoke(kb.replace, str(source), log=str(log))
        entries = srt.process(source.read_text(encoding="utf-8"), "split", lang="ja",
                              max_duration=12, min_duration=0)
        source.write_text(srt.serialize(entries), encoding="utf-8")
        rc, _ = self.invoke(kb.remap_log, str(source), str(log))
        self.assertEqual(rc, 0)
        self.assertEqual(self.read_log(log)[0]["current_entries"], "1;2;3")
        self.assertIn("MisspelledName", self.read_log(log)[0]["source_text"])

    def test_empty_log_can_be_remapped_for_a_video_without_alias_hits(self):
        source = self.subtitle("No known aliases")
        log = self.root / "alias_log.tsv"
        self.invoke(kb.replace, str(source), log=str(log))
        rc, _ = self.invoke(kb.remap_log, str(source), str(log))
        self.assertEqual(rc, 0)
        self.assertEqual(self.read_log(log), [])

    def test_deleted_entry_is_reported_without_guessing_a_new_number(self):
        source, log, entries = self.create_log_before_split()
        source.write_text(srt.serialize(entries[:-1]), encoding="utf-8")
        rc, output = self.invoke(kb.remap_log, str(source), str(log))
        self.assertEqual(rc, 1)
        self.assertEqual(self.read_log(log)[0]["current_entries"], "")
        self.assertIn("no current entry", output)

    def test_remap_cli_uses_timestamped_log(self):
        source, log, _ = self.create_log_before_split()
        result = self.cli("remap-log", str(source), "--log", str(log))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.read_log(log)[0]["current_entries"], "4")

    def test_legacy_log_is_rejected_without_overwriting_it(self):
        source = self.subtitle()
        log = self.root / "legacy.tsv"
        log.write_text("entry\tvariant\tcanonical\tstatus\n1\tbad\tgood\tauto\n", encoding="utf-8")
        before = log.read_bytes()
        rc, _ = self.invoke(kb.remap_log, str(source), str(log))
        self.assertEqual(rc, 2)
        self.assertEqual(log.read_bytes(), before)

    def add_domain_homographs(self):
        rc, _ = self.apply({"index": [{"domain": "alpha"}, {"domain": "beta"}],
                            "glossary": [{"domain": "alpha", "term": "LIVE", "translation": "直播"},
                                         {"domain": "beta", "term": "LIVE", "translation": "现场演出"}]})
        self.assertEqual(rc, 0)

    def test_glossary_matches_only_the_selected_domain(self):
        self.add_domain_homographs()
        source, output = self.subtitle("LIVE"), self.root / "glossary.md"
        rc, _ = self.invoke(kb.build_glossary, str(source), str(output), domains=["beta"])
        self.assertEqual(rc, 0)
        glossary = output.read_text(encoding="utf-8")
        self.assertIn("现场演出", glossary)
        self.assertNotIn("直播", glossary)

    def test_glossary_keeps_both_explicitly_selected_domain_definitions(self):
        self.add_domain_homographs()
        source, output = self.subtitle("LIVE"), self.root / "glossary.md"
        result = self.cli("glossary", str(source), "-o", str(output), "-d", "alpha", "-d", "beta")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        glossary = output.read_text(encoding="utf-8")
        self.assertIn("现场演出", glossary)
        self.assertIn("直播", glossary)

    def test_entity_translation_conflicts_with_existing_alias(self):
        self.apply({"aliases": [self.alias("SharedName", translation="旧译名")]})
        rc, output = self.apply({
            "entities": [{"domain": "seiyuu", "name": "SharedName", "translation": "新译名"}],
            "glossary": [{"domain": "seiyuu", "term": "OtherTerm", "translation": "可接受的术语"}],
        })
        self.assertEqual(rc, 1)
        self.assertIn("CONFLICT", output)
        self.assertEqual(kb.check(self.database, quiet=True), 0)
        glossary = self.root / "glossary.md"
        self.invoke(kb.build_glossary, str(self.subtitle("SharedName OtherTerm")), str(glossary))
        text = glossary.read_text(encoding="utf-8")
        self.assertIn("旧译名", text)
        self.assertNotIn("新译名", text)
        self.assertIn("可接受的术语", text)

    def test_alias_translation_conflicts_with_existing_entity(self):
        self.apply({"entities": [{"domain": "seiyuu", "name": "SharedName", "translation": "旧译名"}]})
        rc, output = self.apply({"aliases": [self.alias("SharedName", translation="新译名")]})
        self.assertEqual(rc, 1)
        self.assertIn("CONFLICT", output)
        self.assertEqual(kb.check(self.database, quiet=True), 0)
        self.assertNotIn("新译名", (self.database / "aliases.tsv").read_text(encoding="utf-8"))

    def test_conflicting_translations_in_one_proposal_are_not_both_written(self):
        rc, _ = self.apply({"aliases": [self.alias("SharedName", translation="旧译名")],
                            "entities": [{"domain": "seiyuu", "name": "SharedName", "translation": "新译名"}]})
        self.assertEqual(rc, 1)
        self.assertEqual(kb.check(self.database, quiet=True), 0)
        self.assertNotIn("新译名", (self.database / "seiyuu/entities.md").read_text(encoding="utf-8"))

    def test_check_detects_existing_cross_file_conflict_and_glossary_refuses_it(self):
        self.apply({"aliases": [self.alias("SharedName", translation="旧译名")]})
        entity = self.database / "seiyuu/entities.md"
        entity.write_text("# Entities\n\n### SharedName\n- 译名: 新译名\n- 稳定性: stable\n", encoding="utf-8")
        rc, output = self.invoke(kb.check, self.database)
        self.assertEqual(rc, 1)
        self.assertIn("translation", output)
        glossary = self.root / "glossary.md"
        glossary.write_text("preserve existing project glossary", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.invoke(kb.build_glossary, str(self.subtitle("SharedName")), str(glossary))
        self.assertEqual(glossary.read_text(encoding="utf-8"), "preserve existing project glossary")

    def test_glossary_title_variants_participate_in_translation_conflict_check(self):
        self.apply({"glossary": [{"domain": "seiyuu", "term": "Alpha / Beta", "translation": "已定译法"}]})
        rc, output = self.apply({"aliases": [self.alias("Beta", translation="另一译法")]})
        self.assertEqual(rc, 1)
        self.assertIn("CONFLICT", output)
        self.assertEqual(kb.check(self.database, quiet=True), 0)

    def test_empty_entity_translation_can_be_completed(self):
        entity = self.database / "seiyuu/entities.md"
        entity.write_text("# Entities\n\n### Mystery\n- 译名: \n- 稳定性: stable\n", encoding="utf-8")
        rc, _ = self.apply({"entities": [{"domain": "seiyuu", "name": "Mystery", "translation": "谜团"}]})
        self.assertEqual(rc, 0)
        self.assertEqual(kb.load_domain(self.database, "seiyuu")["entities"][0]["fields"]["译名"], "谜团")

    def test_alias_canonical_with_slash_is_one_identity(self):
        self.apply({"glossary": [{"domain": "seiyuu", "term": "Alpha", "translation": "字母"}]})
        rc, _ = self.apply({"aliases": [self.alias("Alpha/Beta", translation="组合名")]})
        self.assertEqual(rc, 0)
        self.assertEqual(kb.check(self.database, quiet=True), 0)
        self.assertEqual(kb.load_aliases(self.database)[0]["translation"], "组合名")


if __name__ == "__main__":
    unittest.main()
