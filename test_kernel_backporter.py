#!/usr/bin/env python3
"""Bounded plan execution integration tests using isolated local Git repos."""
import contextlib
import importlib.machinery
import io
import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

MODULE = Path(__file__).with_name("kernel-backporter")
loader = importlib.machinery.SourceFileLoader("kernel_backporter", str(MODULE))
spec = importlib.util.spec_from_loader(loader.name, loader)
kb = importlib.util.module_from_spec(spec)
import sys
sys.modules[spec.name] = kb
loader.exec_module(kb)


def git(repo, *args, check=True):
    p = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True)
    if check and p.returncode:
        raise AssertionError(f"git {args}: {p.stderr}")
    return p.stdout.strip()


class PlanModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.up = self.root / "upstream"
        self.work = self.root / "work"
        self.up.mkdir()
        git(self.up, "init", "-b", "main")
        git(self.up, "config", "user.name", "Test User")
        git(self.up, "config", "user.email", "test@example.com")
        (self.up / "base.txt").write_text("base\n")
        git(self.up, "add", ".")
        git(self.up, "commit", "-m", "base")
        self.base = git(self.up, "rev-parse", "HEAD")
        self.a = self.commit("a.txt", "commit A")
        self.b = self.commit("b.txt", "commit B")
        self.c = self.commit("c.txt", "commit C")
        self.target = self.c
        git(self.root, "clone", "-q", str(self.up), str(self.work))
        git(self.work, "checkout", "--detach", self.base)
        git(self.work, "config", "user.name", "Backport User")
        git(self.work, "config", "user.email", "backport@example.com")
        self.planfile = self.root / "plan.json"
        self.build_plan()

    def commit(self, name, subject):
        (self.up / name).write_text(subject + "\n")
        git(self.up, "add", name)
        git(self.up, "commit", "-m", subject)
        return git(self.up, "rev-parse", "HEAD")

    def build_plan(self, decisions=None, prereqs=None):
        decisions = decisions or {}
        prereqs = prereqs or {}
        payload = {
            "downstream": self.base, "replay_start": self.base,
            "target": self.target, "order": [self.a, self.b, self.c],
            "commits": {
                sha: {
                    "selected": True, "match": {"status": "missing"},
                    "advisor_decision": decisions.get(sha, "TAKE"),
                    "advisor_prerequisites": [{"sha": dep} for dep in prereqs.get(sha, [])],
                    "subject": subject,
                }
                for sha, subject in [(self.a, "A"), (self.b, "B"), (self.c, "C")]
            }
        }
        self.planfile.write_text(json.dumps(payload))
        return payload

    def args(self, *extra):
        return ["-j", "RHEL-223619", "--plan", str(self.planfile),
                "--repo", str(self.work), "--upstream-url", str(self.up),
                "--downstream-url", str(self.up),
                "--reference-root", str(self.root / "refs"),
                "--report-dir", str(self.root / "reports"),
                "--skip-refresh", *extra]

    def run_plan(self, *extra):
        return kb.main(self.args(*extra))

    def journals(self):
        return list((self.root / "reports").rglob("journal.json"))

    def status_output(self, *extra):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = kb.main(self.args("--status", *extra))
        return result, output.getvalue()

    def test_status_before_first_run_is_read_only_and_fast(self):
        result, output = self.status_output()
        self.assertEqual(result, 0)
        self.assertIn("NOT STARTED", output)
        self.assertIn("3 TAKE pending", output)
        self.assertFalse(self.journals())
        # Status must not even create or refresh reference repositories.
        self.assertFalse((self.root / "refs").exists())
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), self.base)

    def test_status_after_apply_and_resume(self):
        self.assertEqual(self.run_plan("--max-attempts", "2"), 0)
        journal = self.journals()[0]
        original = journal.read_bytes()
        result, output = self.status_output()
        self.assertEqual(result, 0)
        self.assertIn("Status: IN SYNC", output)
        self.assertIn("2 applied", output)
        self.assertIn("Recent recorded applications:", output)
        self.assertIn(f"{self.a[:12]} ->", output)
        self.assertIn("1 TAKE pending", output)
        self.assertIn(f"Next TAKE: {self.c[:12]}", output)
        self.assertEqual(journal.read_bytes(), original)
        self.assertEqual(self.run_plan("--resume"), 0)
        result, output = self.status_output()
        self.assertEqual(result, 0)
        self.assertIn("3 applied", output)
        self.assertIn("0 TAKE pending", output)

    def test_status_hard_reset_requires_new_journal(self):
        self.assertEqual(self.run_plan("--max-attempts", "2"), 0)
        journal = self.journals()[0]
        old = journal.read_bytes()
        git(self.work, "reset", "--hard", self.base)
        result, output = self.status_output()
        self.assertEqual(result, 2)
        self.assertIn("Status: DIVERGED", output)
        self.assertIn("2 applied", output)
        self.assertIn("NOT verified", output)
        self.assertIn("hard reset", output)
        self.assertEqual(journal.read_bytes(), old)
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), self.base)
        with self.assertRaisesRegex(kb.KBError, "journal already exists"):
            self.run_plan()
        with self.assertRaisesRegex(kb.KBError, "working HEAD differs"):
            self.run_plan("--resume")
        journal.rename(journal.with_suffix(".json.archived"))
        result, output = self.status_output()
        self.assertEqual(result, 0)
        self.assertIn("NOT STARTED", output)
        self.assertEqual(self.run_plan("--max-attempts", "2"), 0)
        result, output = self.status_output()
        self.assertEqual(result, 0)
        self.assertIn("2 applied", output)

    def test_status_blocked_plan_and_policy_skips(self):
        self.build_plan(decisions={self.b: "REVIEW"},
                        prereqs={self.c: [self.b]})
        self.assertEqual(self.run_plan("--max-attempts", "3"), 2)
        result, output = self.status_output()
        self.assertEqual(result, 0)
        self.assertIn("Status: BLOCKED", output)
        self.assertIn("1 applied", output)
        self.assertIn("1 policy skipped", output)
        self.assertIn("Blocked:", output)
        self.assertIn("1 TAKE pending", output)

    def test_status_rejects_bad_journal_identity_or_entries(self):
        self.assertEqual(self.run_plan(), 0)
        journal = self.journals()[0]
        original = json.loads(journal.read_text())
        for field, value in (("repo", "/unrelated/tree"),
                             ("plan_sha256", "0" * 64),
                             ("cursor", 2),
                             ("last_head", self.base)):
            with self.subTest(field=field):
                mutated = {**original, field: value}
                journal.write_text(json.dumps(mutated))
                with self.assertRaises(kb.KBError):
                    self.status_output()
        journal.write_text(json.dumps(original))
        self.assertEqual(self.status_output()[0], 0)

    def test_status_checks_dirty_worktree_and_incompatible_options(self):
        (self.work / "untracked").write_text("untracked\n")
        result, output = self.status_output()
        self.assertEqual(result, 2)
        self.assertIn("not clean", output)
        (self.work / "untracked").unlink()
        for option in (("--resume",), ("--dry-run",),
                       ("--max-attempts", "2"),
                       ("--progress-output", str(self.root / "out.c"))):
            with self.subTest(option=option):
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        kb.parse_args(self.args("--status", *option))
                self.assertEqual(caught.exception.code, 2)
        self.assertEqual(kb.parse_args(self.args("--status")).status, True)
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                kb.parse_args(["--plan", str(self.planfile)])

    def test_actual_bounded_apply_and_resume(self):
        self.assertEqual(self.run_plan("--max-attempts", "2"), 0)
        self.assertEqual(git(self.work, "rev-list", "--count", f"{self.base}..HEAD"), "2")
        state = json.loads(self.journals()[0].read_text())
        self.assertEqual(state["cursor"], 2)
        self.assertEqual([x["status"] for x in state["entries"]], ["APPLIED", "APPLIED"])
        self.assertEqual(self.run_plan("--resume", "--max-attempts", "2"), 0)
        self.assertEqual(git(self.work, "rev-list", "--count", f"{self.base}..HEAD"), "3")
        self.assertEqual(json.loads(self.journals()[0].read_text())["cursor"], 3)
        self.assertIn(f"(cherry picked from commit {self.a})", git(self.work, "log", "-1", "--format=%B", "HEAD~2"))

    def test_dry_run_is_cumulative_and_does_not_journal(self):
        self.assertEqual(self.run_plan("--max-attempts", "3", "--dry-run"), 0)
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), self.base)
        self.assertFalse(self.journals())

    def test_refuses_stale_baseline(self):
        (self.work / "local.txt").write_text("local\n")
        git(self.work, "add", ".")
        git(self.work, "commit", "-m", "local")
        with self.assertRaisesRegex(kb.KBError, "does not match plan baseline"):
            self.run_plan("--dry-run")

    def test_policy_skips_non_take(self):
        self.build_plan(decisions={self.b: "SKIP", self.c: "REVIEW"})
        self.assertEqual(self.run_plan("--max-attempts", "5"), 0)
        self.assertEqual(git(self.work, "rev-list", "--count", f"{self.base}..HEAD"), "1")
        entries = json.loads(self.journals()[0].read_text())["entries"]
        self.assertEqual([e["status"] for e in entries], ["APPLIED", "SKIPPED_POLICY", "SKIPPED_POLICY"])

    def test_rejects_prerequisite_outside_plan(self):
        self.build_plan(prereqs={self.a: [self.c]})
        self.assertEqual(self.run_plan("--max-attempts", "2"), 2)
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), self.base)
        state = json.loads(self.journals()[0].read_text())
        self.assertEqual(state["blocked"]["sha"], self.a)
        self.assertEqual(state["cursor"], 0)

    def test_failed_preflight_stops_before_real_change(self):
        # The upstream patch changes 'base', but the downstream differs.
        # Reset target to the pristine baseline plus a conflicting commit.
        (self.up / "base.txt").write_text("upstream\n")
        git(self.up, "add", ".")
        git(self.up, "commit", "-m", "conflicting patch")
        conflicting = git(self.up, "rev-parse", "HEAD")
        (self.work / "base.txt").write_text("downstream\n")
        git(self.work, "add", ".")
        git(self.work, "commit", "-m", "downstream change")
        newbase = git(self.work, "rev-parse", "HEAD")
        self.base = newbase
        self.a = conflicting
        self.b = conflicting
        self.c = conflicting
        # We cannot duplicate SHAs; single item plan for conflict.
        self.planfile.write_text(json.dumps({
            "downstream": newbase, "replay_start": newbase, "target": conflicting,
            "order": [conflicting], "commits": {conflicting: {
                "selected": True, "match": {"status": "missing"},
                "advisor_decision": "TAKE", "advisor_prerequisites": []
            }}
        }))
        (self.root / "plan.c").write_text(
            "BACKPORT PLAN (oldest first)\n" + "=" * 80 + "\n" +
            f'{conflicting[:12]} [DEPENDENCY] ("conflicting patch") // Take\n' +
            "      advisor: TAKE\n"
        )
        self.assertEqual(self.run_plan("--max-attempts", "2",
                                       "--progress-output", str(self.root / "plan_2.c")), 2)
        self.assertIn("// Review (conflict)", (self.root / "plan_2.c").read_text())
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), newbase)
        self.assertEqual((self.work / "base.txt").read_text(), "downstream\n")
        state = json.loads(self.journals()[0].read_text())
        dossier = Path(state["blocked"]["dossier"])
        self.assertTrue((dossier / "upstream.patch").is_file())
        self.assertIn("base.txt", json.loads((dossier / "summary.json").read_text())["files"])

    def test_manual_resolution_requires_provenance(self):
        self.build_plan(prereqs={self.a: [self.c]})
        self.assertEqual(self.run_plan(), 2)
        (self.work / "a.txt").write_text("manually applied\n")
        git(self.work, "add", ".")
        git(self.work, "commit", "-m", f"manual A\n\n(cherry picked from commit {self.a})")
        # A is manually resolved, but C remains a requirement for B in this test? no.
        self.assertEqual(self.run_plan("--resume", "--max-attempts", "1"), 0)
        state = json.loads(self.journals()[0].read_text())
        self.assertEqual(state["entries"][0]["status"], "APPLIED_MANUALLY")
        self.assertEqual(state["cursor"], 2)

    def test_dry_run_resuming_does_not_change_journal(self):
        self.assertEqual(self.run_plan(), 0)
        journal = self.journals()[0]
        before = journal.read_bytes()
        self.assertEqual(self.run_plan("--resume", "--dry-run", "--max-attempts", "2"), 0)
        self.assertEqual(journal.read_bytes(), before)
        self.assertEqual(git(self.work, "rev-list", "--count", f"{self.base}..HEAD"), "1")

    def test_manual_resolution_rejects_missing_provenance(self):
        self.build_plan(prereqs={self.a: [self.c]})
        self.assertEqual(self.run_plan(), 2)
        (self.work / "a.txt").write_text("some unrelated change\n")
        git(self.work, "add", "a.txt")
        git(self.work, "commit", "-m", "no provenance")
        with self.assertRaisesRegex(kb.KBError, "lacks exact upstream cherry-pick provenance"):
            self.run_plan("--resume")

    def test_cumulative_dry_run_of_dependent_patches(self):
        git(self.up, "checkout", "-B", "dependent", self.base)
        (self.up / "dependent.txt").write_text("before\n")
        git(self.up, "add", "dependent.txt")
        git(self.up, "commit", "-m", "introduce dependent file")
        first = git(self.up, "rev-parse", "HEAD")
        (self.up / "dependent.txt").write_text("after\n")
        git(self.up, "commit", "-am", "modify dependent file")
        second = git(self.up, "rev-parse", "HEAD")
        git(self.up, "checkout", "main")
        self.planfile.write_text(json.dumps({
            "downstream": self.base, "replay_start": self.base, "target": second,
            "order": [first, second],
            "commits": {sha: {"selected": True, "match": {"status": "missing"},
                "advisor_decision": "TAKE", "advisor_prerequisites": []}
                for sha in (first, second)}
        }))
        self.assertEqual(self.run_plan("--max-attempts", "2", "--dry-run"), 0)
        self.assertFalse((self.work / "dependent.txt").exists())
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), self.base)

    def test_explicit_prerequisite_marked_skip_blocks_take(self):
        self.build_plan(decisions={self.b: "SKIP"}, prereqs={self.c: [self.b]})
        self.assertEqual(self.run_plan("--max-attempts", "5"), 2)
        state = json.loads(self.journals()[0].read_text())
        self.assertEqual(state["blocked"]["sha"], self.c)
        self.assertEqual([e["status"] for e in state["entries"]], ["APPLIED", "SKIPPED_POLICY"])

    def test_refuses_journal_reuse_without_resume(self):
        self.assertEqual(self.run_plan(), 0)
        with self.assertRaisesRegex(kb.KBError, "journal already exists"):
            self.run_plan()

    def test_refuses_plan_mutation_on_resume(self):
        self.assertEqual(self.run_plan(), 0)
        self.build_plan(decisions={self.b: "SKIP"})
        with self.assertRaisesRegex(kb.KBError, "cannot resume: journal unavailable"):
            # Different digest -> a new journal, but HEAD is now past baseline.
            self.run_plan("--resume")

    def text_template(self):
        lines = ["BACKPORT PLAN (oldest first)", "=" * 80]
        for sha, label in ((self.a, "A"), (self.b, "B"), (self.c, "C")):
            decision = json.loads(self.planfile.read_text())["commits"][sha]["advisor_decision"]
            lines.append(f'{sha[:12]} [DEPENDENCY] ("{label}") // {decision.capitalize()}')
            lines.append(f"      advisor: {decision}")
            lines.append("      advisor-rationale: unmodified evidence")
        lines += ["", "SAME PATCH SERIES", "=" * 80, "unchanged appendix"]
        (self.root / "plan.c").write_text("\n".join(lines) + "\n")
        return self.root / "plan.c"

    def snapshots(self):
        return sorted(self.root.glob("plan_*.c"))

    def test_explicit_progress_outputs_preserve_source_and_resume(self):
        original = self.text_template().read_bytes()
        self.assertEqual(self.run_plan("--max-attempts", "2", "--progress-output",
                                       str(self.root / "plan_2.c")), 0)
        self.assertEqual([p.name for p in self.snapshots()], ["plan_2.c"])
        first = self.snapshots()[0].read_text()
        entries = json.loads(self.journals()[0].read_text())["entries"]
        self.assertIn(f"{self.a[:12]} [DEPENDENCY]", first)
        self.assertIn(f"// Done {entries[0]['downstream_sha'][:12]}", first)
        self.assertIn(f"// Done {entries[1]['downstream_sha'][:12]}", first)
        self.assertIn(f'{self.c[:12]} [DEPENDENCY] ("C") // Take', first)
        self.assertIn("advisor-rationale: unmodified evidence", first)
        self.assertEqual(self.run_plan("--resume", "--max-attempts", "1",
                                       "--progress-output", str(self.root / "plan_3.c")), 0)
        self.assertEqual([p.name for p in self.snapshots()], ["plan_2.c", "plan_3.c"])
        self.assertEqual(self.snapshots()[0].read_text(), first)
        self.assertEqual(self.text_template().read_bytes(), original)
        self.assertEqual(self.snapshots()[1].read_text().count("// Done "), 3)
        self.assertIn("unchanged appendix", self.snapshots()[1].read_text())

    def test_dry_run_produces_no_snapshot(self):
        self.text_template()
        self.assertEqual(self.run_plan("--dry-run", "--max-attempts", "2"), 0)
        self.assertFalse(self.snapshots())
        self.assertFalse(self.journals())

    def test_blocked_prerequisite_snapshot_marks_review(self):
        self.build_plan(prereqs={self.c: [self.b]})
        # Prior advisor decision SKIP must remain unchanged in the original.
        self.build_plan(decisions={self.b: "SKIP"}, prereqs={self.c: [self.b]})
        self.text_template()
        self.assertEqual(self.run_plan("--max-attempts", "5",
                                       "--progress-output", str(self.root / "plan_2.c")), 2)
        snapshot = self.snapshots()[0].read_text()
        self.assertIn(f'{self.b[:12]} [DEPENDENCY] ("B") // Skip', snapshot)
        self.assertIn(f'{self.c[:12]} [DEPENDENCY] ("C") // Review (prerequisite)', snapshot)
        self.assertIn("      advisor: TAKE", snapshot)

    def test_snapshot_conflict_reason_and_manual_resolution(self):
        self.build_plan(prereqs={self.a: [self.c]})
        self.text_template()
        self.assertEqual(self.run_plan("--progress-output", str(self.root / "plan_2.c")), 2)
        first = self.snapshots()[0].read_text()
        self.assertIn(f'{self.a[:12]} [DEPENDENCY] ("A") // Review (prerequisite)', first)
        (self.work / "a.txt").write_text("manual change\n")
        git(self.work, "add", "a.txt")
        git(self.work, "commit", "-m", f"manual A\n\n(cherry picked from commit {self.a})")
        self.assertEqual(self.run_plan("--resume", "--max-attempts", "1",
                                       "--progress-output", str(self.root / "plan_3.c")), 0)
        second = self.snapshots()[1].read_text()
        state = json.loads(self.journals()[0].read_text())
        self.assertIn(f"// Done {state['entries'][0]['downstream_sha'][:12]}", second)

    def test_existing_output_refuses_before_backports(self):
        self.text_template()
        previous = self.root / "plan_2.c"
        previous.write_text("historical snapshot\n")
        with self.assertRaisesRegex(kb.KBError, "already exists"):
            self.run_plan("--progress-output", str(previous))
        self.assertEqual(previous.read_text(), "historical snapshot\n")
        self.assertFalse(self.journals())
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), self.base)
        self.assertFalse((self.root / "plan_3.c").exists())

    def test_mismatched_text_plan_rejected_before_any_backports(self):
        source = self.text_template()
        source.write_text(source.read_text().replace(self.a[:12], "0" * 12, 1))
        with self.assertRaisesRegex(kb.KBError, "does not match JSON order"):
            self.run_plan("--progress-output", str(self.root / "plan_2.c"))
        self.assertFalse(self.journals())
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), self.base)

    def test_mismatched_policy_rejected(self):
        source = self.text_template()
        source.write_text(source.read_text().replace("// Take", "// Skip", 1))
        with self.assertRaisesRegex(kb.KBError, "differs from JSON"):
            self.run_plan("--progress-output", str(self.root / "plan_2.c"))
        self.assertFalse(self.journals())

    def test_text_plan_flag_requires_existing_file(self):
        with self.assertRaisesRegex(kb.KBError, "text report not found"):
            self.run_plan("--text-plan", str(self.root / "missing.c"),
                          "--progress-output", str(self.root / "plan_2.c"))
        self.assertFalse(self.journals())

    def test_renderer_mark_already_present_without_inventing_sha(self):
        source = self.text_template()
        plan = kb.BackportPlan.load(str(self.planfile))
        renderer = kb.PlanTextProgress(source, plan)
        text = renderer.render(plan, {"entries": [
            {"sha": self.a, "status": "ALREADY_PRESENT"}], "blocked": None})
        self.assertIn(f'{self.a[:12]} [DEPENDENCY] ("A") // Done (already present)', text)

    def test_no_progress_output_is_default(self):
        self.text_template()
        self.assertEqual(self.run_plan("--max-attempts", "2"), 0)
        self.assertFalse(self.snapshots())
        self.assertEqual(json.loads(self.journals()[0].read_text())["cursor"], 2)

    def test_user_chosen_names_are_not_incremented(self):
        self.text_template()
        first = self.root / "checkpoint_alpha.txt"
        second = self.root / "checkpoint_beta.c"
        self.assertEqual(self.run_plan("--progress-output", str(first)), 0)
        old = first.read_bytes()
        self.assertEqual(self.run_plan("--resume", "--progress-output", str(second)), 0)
        self.assertEqual(first.read_bytes(), old)
        self.assertIn("// Done ", second.read_text())
        self.assertFalse(self.snapshots())

    def test_rejects_output_equal_to_original_or_json(self):
        original = self.text_template()
        for dest in (original, self.planfile):
            with self.subTest(dest=dest):
                with self.assertRaisesRegex(kb.KBError, "already exists"):
                    self.run_plan("--progress-output", str(dest))
                self.assertFalse(self.journals())

    def test_rejects_missing_output_directory(self):
        self.text_template()
        with self.assertRaisesRegex(kb.KBError, "directory does not exist"):
            self.run_plan("--progress-output", str(self.root / "absent" / "copy.c"))
        self.assertFalse(self.journals())

    def test_json_only_plan_does_not_require_text(self):
        self.assertEqual(self.run_plan(), 0)
        self.assertFalse(self.snapshots())

    def test_rejects_dry_run_with_progress_output(self):
        self.text_template()
        with self.assertRaises(SystemExit) as raised:
            self.run_plan("--dry-run", "--progress-output", str(self.root / "test.c"))
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(self.journals())

    def test_text_plan_requires_output(self):
        self.text_template()
        with self.assertRaises(SystemExit) as raised:
            self.run_plan("--text-plan", str(self.root / "plan.c"))
        self.assertEqual(raised.exception.code, 2)
        self.assertFalse(self.journals())

    def test_dangling_symlink_is_not_overwritten(self):
        self.text_template()
        dest = self.root / "dangling.c"
        dest.symlink_to(self.root / "not-here")
        with self.assertRaisesRegex(kb.KBError, "already exists"):
            self.run_plan("--progress-output", str(dest))
        self.assertTrue(dest.is_symlink())
        self.assertFalse(self.journals())

    def test_validates_json(self):
        p = self.build_plan()
        p["order"] = [self.a, self.a]
        self.planfile.write_text(json.dumps(p))
        with self.assertRaisesRegex(kb.KBError, "duplicate"):
            self.run_plan("--dry-run")


class OptionalTargetTests(unittest.TestCase):
    """Single-commit implicit target and frozen plan-target regressions."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.up = self.root / "upstream"
        self.work = self.root / "work"
        self.up.mkdir()
        git(self.up, "init", "-b", "main")
        git(self.up, "config", "user.name", "Test User")
        git(self.up, "config", "user.email", "test@example.com")
        (self.up / "base.txt").write_text("initial\n")
        git(self.up, "add", ".")
        git(self.up, "commit", "-m", "base")
        self.base = git(self.up, "rev-parse", "HEAD")
        (self.up / "one.txt").write_text("one\n")
        git(self.up, "add", ".")
        git(self.up, "commit", "-m", "first")
        self.first = git(self.up, "rev-parse", "HEAD")
        git(self.root, "clone", "-q", str(self.up), str(self.work))
        git(self.work, "checkout", "--detach", self.base)
        git(self.work, "config", "user.name", "Backport User")
        git(self.work, "config", "user.email", "backport@example.com")
        self.ds = self.root / "downstream"
        git(self.root, "clone", "-q", str(self.up), str(self.ds))
        git(self.ds, "checkout", "-B", "main", self.base)

    def arguments(self, *extra):
        return ["-j", "RHEL-293896", "-c", self.first,
                "--repo", str(self.work), "--upstream-url", str(self.up),
                "--downstream-url", str(self.ds),
                "--reference-root", str(self.root / "refs"),
                "--report-dir", str(self.root / "reports"),
                *extra]

    def reference(self, *extra):
        args = kb.parse_args(self.arguments(*extra))
        refs = kb.ReferenceManager(args, kb.Git(self.work))
        _, _, target, _ = refs.prepare()
        return args, refs, target

    def test_single_commit_accepts_omitted_target(self):
        args = kb.parse_args(self.arguments())
        self.assertIsNone(args.target)
        args, _, target = self.reference()
        self.assertEqual(args.target, "refs/remotes/origin/HEAD")
        self.assertEqual(target, self.first)

    def test_implicit_target_uses_refreshed_tracking_tip_not_stale_local_head(self):
        _, refs, first_target = self.reference()
        self.assertEqual(first_target, self.first)
        local_head = git(refs.upstream_path, "rev-parse", "HEAD")
        (self.up / "two.txt").write_text("two\n")
        git(self.up, "add", ".")
        git(self.up, "commit", "-m", "second")
        second = git(self.up, "rev-parse", "HEAD")
        args, refs, target = self.reference()
        self.assertEqual(target, second)
        self.assertEqual(git(refs.upstream_path, "rev-parse", "HEAD"), local_head)
        self.assertEqual(args.target, "refs/remotes/origin/HEAD")

    def test_explicit_target_is_not_replaced(self):
        args, _, target = self.reference("--target", self.base)
        self.assertEqual(args.target, self.base)
        self.assertEqual(target, self.base)

    def test_local_head_fallback_if_remote_head_missing(self):
        _, refs, _ = self.reference()
        git(refs.upstream_path, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        args, refs, target = self.reference("--skip-refresh")
        self.assertEqual(args.target, "HEAD")
        self.assertEqual(target, git(refs.upstream_path, "rev-parse", "HEAD"))

    def test_direct_dry_run_without_target_does_not_edit_working_tree(self):
        original = git(self.work, "rev-parse", "HEAD")
        self.assertEqual(kb.main(self.arguments("--skip-refresh", "--dry-run", "--no-series")), 0)
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), original)
        report = json.loads((self.root / "reports" / self.first / "report.json").read_text())
        self.assertEqual(report["target"], self.first)
        self.assertEqual(report["target_name"], "refs/remotes/origin/HEAD")

    def test_explicit_older_target_still_checks_ancestry(self):
        args = kb.parse_args(self.arguments("--target", self.base, "--skip-refresh"))
        with self.assertRaisesRegex(kb.KBError, "not an ancestor"):
            kb.KernelBackporter(args).prepare()

    def test_plan_still_loads_frozen_target(self):
        payload = {
            "downstream": self.base, "replay_start": self.base,
            "target": self.first, "order": [self.first],
            "commits": {self.first: {
                "selected": True, "match": {"status": "missing"},
                "advisor_decision": "TAKE", "advisor_prerequisites": [],
            }},
        }
        plan = self.root / "plan.json"
        plan.write_text(json.dumps(payload))
        args = kb.parse_args(["-j", "RHEL-293896", "--plan", str(plan)])
        self.assertIsNone(args.target)
        self.assertEqual(kb.BackportPlan.load(args.plan).target, self.first)


if __name__ == "__main__":
    unittest.main()
