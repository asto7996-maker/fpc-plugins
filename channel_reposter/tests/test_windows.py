"""Несколько окон перелива: независимые пары каналов."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import MAX_JOBS, Database  # noqa: E402


class MultiWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "jobs.db")
        self.db.ensure_defaults(
            caption="cap",
            posts_per_cycle=3,
            source_channel="@src_a",
            target_channel="@dst_a",
            interval_seconds=60,
        )

    def test_migrates_single_window(self) -> None:
        jobs = self.db.list_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].source_channel, "@src_a")
        self.assertEqual(jobs[0].target_channel, "@dst_a")
        self.assertEqual(self.db.get_settings().job_id, jobs[0].job_id)

    def test_create_and_switch_windows(self) -> None:
        first = self.db.get_settings()
        second = self.db.create_job()
        self.assertEqual(len(self.db.list_jobs()), 2)
        self.assertEqual(second.source_channel, "")
        self.assertFalse(second.is_running)
        self.assertEqual(second.caption_template, "cap")

        self.db.set_active_job(second.job_id)
        self.db.set_source_channel("@src_b")
        self.db.set_target_channel("@dst_b")
        self.db.set_progress_id(10)

        self.db.set_active_job(first.job_id)
        s = self.db.get_settings()
        self.assertEqual(s.source_channel, "@src_a")
        self.assertEqual(s.progress_id, 0)

        self.db.set_active_job(second.job_id)
        s = self.db.get_settings()
        self.assertEqual(s.source_channel, "@src_b")
        self.assertEqual(s.target_channel, "@dst_b")
        self.assertEqual(s.progress_id, 10)

    def test_history_is_per_window(self) -> None:
        a = self.db.get_settings()
        self.db.add_history(100, target_message_id=1, status="ok")
        b = self.db.create_job()
        self.db.set_active_job(b.job_id)
        self.db.add_history(100, target_message_id=2, status="ok")

        self.assertTrue(self.db.was_processed(100))
        self.assertEqual(self.db.history_count(), 1)

        self.db.set_active_job(a.job_id)
        self.assertTrue(self.db.was_processed(100))
        self.assertEqual(self.db.history_count(), 1)

        self.db.clear_history()
        self.assertEqual(self.db.history_count(), 0)
        self.db.set_active_job(b.job_id)
        self.assertEqual(self.db.history_count(), 1)

    def test_running_flags_independent(self) -> None:
        a = self.db.get_settings()
        b = self.db.create_job()
        self.db.set_running(True)
        self.db.set_active_job(b.job_id)
        self.db.set_running(False)

        jobs = {j.job_id: j for j in self.db.list_jobs()}
        self.assertTrue(jobs[a.job_id].is_running)
        self.assertFalse(jobs[b.job_id].is_running)
        self.assertTrue(self.db.any_job_running())

        due = self.db.due_jobs(9e12)
        self.assertEqual([j.job_id for j in due], [a.job_id])

    def test_cannot_delete_last(self) -> None:
        with self.assertRaises(ValueError):
            self.db.delete_job(self.db.get_settings().job_id)

    def test_delete_switches_away(self) -> None:
        a = self.db.get_settings().job_id
        b = self.db.create_job().job_id
        self.db.set_active_job(b)
        self.db.delete_job(b)
        self.assertEqual(self.db.get_settings().job_id, a)
        self.assertEqual(len(self.db.list_jobs()), 1)

    def test_max_jobs(self) -> None:
        while self.db._job_count() < MAX_JOBS:
            self.db.create_job()
        with self.assertRaises(ValueError):
            self.db.create_job()

    def test_job_scope_isolates_progress(self) -> None:
        a = self.db.get_settings().job_id
        b = self.db.create_job().job_id
        with self.db.job_scope(b):
            self.db.set_progress_id(77)
            self.assertEqual(self.db.get_progress_id(), 77)
        self.assertEqual(self.db.get_settings().job_id, a)
        self.assertEqual(self.db.get_progress_id(), 0)
        self.assertEqual(self.db.get_job(b).progress_id, 77)  # type: ignore[union-attr]

    def test_clone_copies_source_not_target(self) -> None:
        first = self.db.get_settings()
        clone = self.db.clone_job(first.job_id)
        self.assertEqual(clone.source_channel, "@src_a")
        self.assertEqual(clone.target_channel, "")
        self.assertFalse(clone.is_running)
        self.assertEqual(clone.caption_template, "cap")
        self.assertEqual(len(self.db.list_jobs()), 2)

    def test_start_ready_and_pause_all(self) -> None:
        a = self.db.get_settings()
        b = self.db.create_job()
        self.db.set_active_job(b.job_id)
        self.db.set_source_channel("@src_b")
        self.db.set_target_channel("@dst_b")
        self.db.set_progress_id(0)

        started, skipped = self.db.start_ready_jobs()
        self.assertEqual(set(started), {a.job_id, b.job_id})
        self.assertEqual(skipped, [])
        jobs = {j.job_id: j for j in self.db.list_jobs()}
        self.assertTrue(jobs[a.job_id].is_running)
        self.assertTrue(jobs[b.job_id].is_running)

        empty = self.db.create_job()
        started, skipped = self.db.start_ready_jobs()
        self.assertIn(empty.job_id, skipped)

        paused = self.db.pause_all_jobs()
        self.assertGreaterEqual(len(paused), 2)
        self.assertFalse(any(j.is_running for j in self.db.list_jobs()))


if __name__ == "__main__":
    unittest.main()
