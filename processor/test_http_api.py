"""HTTP handlers: settings reach the geometry, and requests stay bounded."""

import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import cv2
import numpy as np

import nozzle_sizing
import stamp_tool


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Every upload, extraction and job lands in one directory removed afterwards.
        scratch = tempfile.TemporaryDirectory()
        cls.addClassCleanup(scratch.cleanup)
        cls.enterClassContext(mock.patch.object(tempfile, "tempdir", scratch.name))
        # The handler and job thread report progress with print(); keep tests quiet.
        cls.enterClassContext(mock.patch.object(stamp_tool, "print", lambda *a, **k: None, create=True))
        for store in (stamp_tool.images, stamp_tool.extractions, stamp_tool.stamp_jobs):
            cls.enterClassContext(mock.patch.dict(store))
        cls.server = stamp_tool.ThreadedHTTPServer(("127.0.0.1", 0), stamp_tool.StampHandler)
        cls.addClassCleanup(cls.server.server_close)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.server.shutdown)

        image = np.full((200, 200), 255, np.uint8)
        cv2.circle(image, (100, 100), 70, 0, -1)
        png = cv2.imencode(".png", image)[1].tobytes()
        boundary = "TESTBOUNDARY"
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="disc.png"\r\n'
                "Content-Type: image/png\r\n\r\n").encode() + png + f"\r\n--{boundary}--\r\n".encode()
        status, upload, _ = cls.request("POST", "/upload", body,
                                     {"Content-Type": f"multipart/form-data; boundary={boundary}"})
        if status != 200:
            raise RuntimeError(f"upload failed: {upload}")
        status, extraction = cls.post("/extract", {"image_id": upload["id"], "x": 0, "y": 0, "w": 200, "h": 200})
        if status != 200:
            raise RuntimeError(f"extraction failed: {extraction}")
        cls.extraction = extraction["id"]

    @classmethod
    def request(cls, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=120)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            data = response.read()
        finally:
            connection.close()
        try:
            return response.status, json.loads(data), response.headers
        except ValueError:
            return response.status, data, response.headers

    @classmethod
    def post(cls, path, payload):
        status, data, _ = cls.request("POST", path, json.dumps(payload).encode(),
                                      {"Content-Type": "application/json"})
        return status, data

    def body(self, **overrides):
        return {"extraction_id": self.extraction, "width_mm": 30, "total_height_mm": 20,
                "body_shape": "rectangular", "height_mm": 20, "mode": "raised",
                "nozzle_mm": 0.4, "tolerance_percent": 5, **overrides}

    def assertSlotFree(self):
        self.assertTrue(stamp_tool.sizing_slot.acquire(blocking=False), "sizing slot leaked")
        stamp_tool.sizing_slot.release()

    def test_fit_reaches_the_preview_and_the_sizer(self):
        status, preview = self.post("/print-preview", self.body(fit="fill"))
        self.assertEqual(status, 200, preview)
        self.assertEqual(preview["dimensions"]["fit"], "fill")
        status, sizing = self.post("/nozzle-size", self.body(fit="bleed"))
        self.assertEqual(status, 200, sizing)
        self.assertEqual(sizing["current"]["dimensions"]["fit"], "bleed")

    def test_export_passes_the_fit_and_a_contained_name_to_the_renderer(self):
        commands = []
        real_run = subprocess.run

        def fake_png2stamp(cmd, *args, **kwargs):
            if cmd[:2] != ["bash", stamp_tool.PNG2STAMP]:
                return real_run(cmd, *args, **kwargs)
            commands.append(cmd)
            with open(cmd[3] + ".3mf", "wb") as output:
                output.write(b"3mf")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        escape = os.path.join(tempfile.gettempdir(), "escaped", "evil")
        with mock.patch.object(stamp_tool.subprocess, "run", fake_png2stamp):
            status, job = self.post("/stamp", self.body(fit="fill", name=escape))
            self.assertEqual(status, 200, job)
            for _ in range(200):
                _, state, _ = self.request("GET", f"/stamp/status/{job['job_id']}")
                if state["status"] != "running":
                    break
                time.sleep(0.05)
        self.assertEqual(state["status"], "done", state)
        (cmd,) = commands
        self.assertEqual(cmd[-1], "fill")
        # The output stays a plain name inside the job's own temp directory.
        self.assertEqual(os.path.dirname(cmd[3]), os.path.dirname(cmd[2]))
        self.assertNotIn(os.path.sep, os.path.basename(cmd[3]))
        self.assertFalse(os.path.exists(os.path.dirname(escape)))

    def test_invalid_fit_and_oversized_numbers_are_rejected(self):
        for path in ("/print-preview", "/nozzle-size", "/stamp"):
            status, _ = self.post(path, self.body(fit="edge"))
            self.assertEqual(status, 400, path)
            status, _, _ = self.request("POST", path, b'{"extraction_id": ' + b"1" * 5000 + b"}",
                                        {"Content-Type": "application/json"})
            self.assertEqual(status, 400, path)

    def test_failed_searches_release_the_slot(self):
        status, _ = self.post("/nozzle-size", self.body(nozzle_mm=0.5))
        self.assertEqual(status, 400)
        self.assertSlotFree()
        with mock.patch.object(stamp_tool, "size_for_nozzle", side_effect=RuntimeError("boom")):
            status, _ = self.post("/nozzle-size", self.body())
        self.assertEqual(status, 500)
        self.assertSlotFree()

    def test_only_one_search_runs_at_a_time(self):
        active, peak, guard, finish = [0], [0], threading.Lock(), threading.Event()

        def slow_search(*args, **kwargs):
            with guard:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            finish.wait(30)  # Runs until both other requests have been refused.
            with guard:
                active[0] -= 1
            return {"current": {}}

        statuses = []
        with mock.patch.object(stamp_tool, "size_for_nozzle", slow_search), \
                mock.patch.object(stamp_tool, "SIZING_WAIT_SECONDS", 0.05):
            threads = [threading.Thread(target=lambda: statuses.append(self.post("/nozzle-size", self.body())[0]))
                       for _ in range(3)]
            for thread in threads:
                thread.start()
            for _ in range(3000):
                if statuses.count(503) == 2:
                    break
                time.sleep(0.01)
            finish.set()
            for thread in threads:
                thread.join()
        self.assertEqual(peak[0], 1)
        self.assertEqual(sorted(statuses), [200, 503, 503])
        self.assertSlotFree()

    def test_a_new_search_waits_briefly_for_the_slot(self):
        # E.g. the user's own just-abandoned search, about to notice and stop.
        self.assertTrue(stamp_tool.sizing_slot.acquire(blocking=False))
        release = threading.Timer(0.3, stamp_tool.sizing_slot.release)
        release.start()
        try:
            status, data = self.post("/nozzle-size", self.body())
        finally:
            release.join()
        self.assertEqual(status, 200, data)
        self.assertSlotFree()

    def test_previews_run_between_the_evaluations_of_a_search(self):
        held_during_evaluation, probe_got_lock, probed_mid_search = [], threading.Event(), []
        real_detail, real_resolve = nozzle_sizing.nozzle_detail, nozzle_sizing.resolve_dimensions

        def probe():
            # Stands in for a preview arriving mid-search.
            if stamp_tool.preview_lock.acquire(timeout=30):
                probe_got_lock.set()
                stamp_tool.preview_lock.release()

        def watched_detail(*args, **kwargs):
            held_during_evaluation.append(stamp_tool.preview_lock.locked())
            # Later evaluations see whether the probe already got in between them.
            probed_mid_search.append(probe_got_lock.is_set())
            if len(held_during_evaluation) == 1:
                threading.Thread(target=probe, daemon=True).start()
            return real_detail(*args, **kwargs)

        def spaced_resolve(*args, **kwargs):
            time.sleep(0.02)  # A gap between evaluations, outside the lock.
            return real_resolve(*args, **kwargs)

        with mock.patch.object(nozzle_sizing, "nozzle_detail", watched_detail), \
                mock.patch.object(nozzle_sizing, "resolve_dimensions", spaced_resolve):
            # A passing 60 mm disc walks its whole ladder down: many evaluations.
            status, data = self.post("/nozzle-size", self.body(width_mm=60, height_mm=60))
        self.assertEqual(status, 200, data)
        self.assertGreater(len(held_during_evaluation), 3)
        self.assertTrue(all(held_during_evaluation), "an evaluation ran without preview_lock")
        self.assertTrue(any(probed_mid_search), "the search held preview_lock throughout")

    def test_a_disconnected_client_stops_its_search(self):
        stopped = threading.Event()

        def endless_search(*args, checkpoint, **kwargs):
            for _ in range(500):
                try:
                    checkpoint()
                except stamp_tool.SizingStopped:
                    stopped.set()
                    raise
                time.sleep(0.01)
            return {"current": {}}

        with mock.patch.object(stamp_tool, "size_for_nozzle", endless_search):
            body = json.dumps(self.body()).encode()
            client = socket.create_connection(("127.0.0.1", self.port))
            client.sendall(b"POST /nozzle-size HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\n"
                           + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            time.sleep(0.2)
            client.close()
            self.assertTrue(stopped.wait(3), "search kept running after its client left")
        time.sleep(0.1)
        self.assertSlotFree()

    def test_a_held_through_search_releases_the_preview_lock(self):
        with mock.patch.object(stamp_tool, "SIZING_CONTENDED_SECONDS", -1):
            status, data = self.post("/nozzle-size", self.body(width_mm=60, height_mm=60))
        self.assertEqual(status, 200, data)
        self.assertFalse(stamp_tool.preview_lock.locked(), "preview_lock left held")
        self.assertSlotFree()

    def test_a_queued_search_stops_when_its_client_leaves(self):
        # Previews hold the lock; the abandoned search must not wait them out.
        stamp_tool.preview_lock.acquire()
        try:
            body = json.dumps(self.body()).encode()
            client = socket.create_connection(("127.0.0.1", self.port))
            client.sendall(b"POST /nozzle-size HTTP/1.1\r\nHost: test\r\nContent-Type: application/json\r\n"
                           + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
            time.sleep(0.3)
            client.close()
            for _ in range(300):
                if stamp_tool.sizing_slot.acquire(blocking=False):
                    stamp_tool.sizing_slot.release()
                    break
                time.sleep(0.01)
            else:
                self.fail("queued search kept the slot after its client left")
        finally:
            stamp_tool.preview_lock.release()

    def test_a_search_past_its_deadline_answers_busy(self):
        def endless_search(*args, checkpoint, **kwargs):
            while True:
                checkpoint()
                time.sleep(0.01)

        with mock.patch.object(stamp_tool, "size_for_nozzle", endless_search), \
                mock.patch.object(stamp_tool, "SIZING_DEADLINE_SECONDS", 0.2):
            status, data, headers = self.request("POST", "/nozzle-size", json.dumps(self.body()).encode(),
                                                 {"Content-Type": "application/json"})
        self.assertEqual(status, 503, data)
        self.assertIn("too busy", data["error"])
        self.assertEqual(headers["Retry-After"], str(stamp_tool.SIZING_RETRY_SECONDS))
        self.assertSlotFree()


class ScriptedLock:
    """A lock whose acquire() results are scripted, to stand in for contention."""

    def __init__(self, grants):
        self.grants = list(grants)
        self.held = False

    def acquire(self, checkpoint=None, poll_seconds=0.25):
        # Each scripted refusal is one poll interval spent waiting in line.
        while self.grants and not self.grants.pop(0):
            if checkpoint:
                checkpoint()
        self.held = True
        return True

    def release(self):
        assert self.held, "released a lock that was not held"
        self.held = False


class FairLockTests(unittest.TestCase):
    def test_waiters_get_the_lock_in_arrival_order(self):
        lock, order = stamp_tool.FairLock(), []
        lock.acquire()

        def waiter(name):
            with lock:
                order.append(name)

        threads = []
        for name in "abc":
            threads.append(threading.Thread(target=waiter, args=(name,)))
            threads[-1].start()
            while len(lock._waiters) < len(threads):
                time.sleep(0.001)
        lock.release()
        for thread in threads:
            thread.join()
        self.assertEqual(order, list("abc"))
        self.assertFalse(lock.locked())

    def test_a_waiter_that_checks_in_keeps_its_place(self):
        # A threading.Lock poller never gets through seven looping holders.
        lock, stop = stamp_tool.FairLock(), threading.Event()

        def holder():
            while not stop.is_set():
                with lock:
                    time.sleep(0.03)

        holders = [threading.Thread(target=holder) for _ in range(7)]
        for thread in holders:
            thread.start()
        try:
            checks = []
            start = time.monotonic()
            lock.acquire(checkpoint=lambda: checks.append(1), poll_seconds=0.01)
            waited = time.monotonic() - start
            lock.release()
        finally:
            stop.set()
            for thread in holders:
                thread.join()
        self.assertLess(waited, 5)
        self.assertTrue(checks, "never checked in while waiting")

    def test_giving_up_leaves_the_lock_consistent(self):
        lock = stamp_tool.FairLock()
        lock.acquire()
        self.assertFalse(lock.acquire(blocking=False))
        self.assertFalse(lock.acquire(timeout=0.05))

        def stop():
            raise stamp_tool.SizingStopped("client gone")

        with self.assertRaises(stamp_tool.SizingStopped):
            lock.acquire(checkpoint=stop, poll_seconds=0.01)
        self.assertFalse(lock._waiters)
        lock.release()
        self.assertFalse(lock.locked())
        with self.assertRaises(RuntimeError):
            lock.release()

    def test_a_turn_handed_over_while_giving_up_is_passed_on(self):
        lock = stamp_tool.FairLock()
        lock.acquire()
        mine, theirs = threading.Event(), threading.Event()
        lock._waiters.extend([mine, theirs])
        lock.release()  # Hands the lock to "mine" ...
        lock._withdraw(mine)  # ... which gives up at that very moment.
        self.assertTrue(theirs.is_set())
        self.assertTrue(lock.locked())


class SearchLockTests(unittest.TestCase):
    def test_uncontended_searches_release_between_evaluations(self):
        lock = stamp_tool.FairLock()
        search = stamp_tool.SearchLock(lock)
        for _ in range(3):
            with search:
                self.assertTrue(lock.locked())
            self.assertFalse(lock.locked())
        search.close()
        self.assertFalse(lock.locked())

    def test_waits_add_up_until_the_search_keeps_the_lock(self):
        # Two waits, each under the threshold, together over it.
        wait = stamp_tool.SIZING_CONTENDED_SECONDS * 0.6
        lock = ScriptedLock([True, True])
        search = stamp_tool.SearchLock(lock, clock=iter([0, wait, 100, 100 + wait]).__next__)
        with search:
            pass
        self.assertFalse(lock.held)
        with search:
            pass
        self.assertTrue(lock.held, "released despite contention")
        with search:  # Later evaluations reuse the held lock without acquiring.
            pass
        search.close()
        self.assertFalse(lock.held)

    def test_a_queued_search_checks_in_while_it_waits(self):
        calls = []
        lock = ScriptedLock([False, False, True])
        with stamp_tool.SearchLock(lock, checkpoint=lambda: calls.append(1), poll_seconds=0):
            pass
        self.assertEqual(len(calls), 2)

        def stop():
            raise stamp_tool.SizingStopped("client gone")

        lock = ScriptedLock([False, True])
        with self.assertRaises(stamp_tool.SizingStopped):
            with stamp_tool.SearchLock(lock, checkpoint=stop, poll_seconds=0):
                pass
        self.assertFalse(lock.held)

    def test_limits_hold_the_lock_long_before_any_deadline(self):
        # Hold-through must start well before the search would give up, and
        # the server must answer before the page abandons the request.
        page = (Path(__file__).with_name("index.html")).read_text()
        page_timeout = int(re.search(r"SIZING_TIMEOUT_MS = (\d+)", page).group(1)) / 1000
        self.assertLessEqual(stamp_tool.SIZING_CONTENDED_SECONDS * 4, stamp_tool.SIZING_DEADLINE_SECONDS)
        self.assertLess(stamp_tool.SIZING_DEADLINE_SECONDS, page_timeout)


if __name__ == "__main__":
    unittest.main()
