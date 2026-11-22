"""Tests for background jobs (the proxy-timeout workaround)."""
import time

import server


def _wait(job_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        out = server.ark_job_status(job_id, wait_seconds=1)
        if out.get("status") != "running":
            return out
    raise AssertionError("job did not finish in time")


def test_start_job_rejects_unknown_tool():
    out = server.ark_start_job("ark_nonexistent", "{}")
    assert out["ok"] is False
    assert "not a background-capable tool" in out["error"]


def test_start_job_rejects_bad_json():
    out = server.ark_start_job("ark_edit_image", "{not json")
    assert out["ok"] is False
    assert "not valid JSON" in out["error"]


def test_start_job_rejects_non_object_arguments():
    out = server.ark_start_job("ark_edit_image", "[1, 2]")
    assert out["ok"] is False
    assert "must be a JSON object" in out["error"]


def test_job_runs_and_returns_result(monkeypatch):
    monkeypatch.setattr(server, "ark_edit_image",
                        lambda **kw: {"ok": True, "echo": kw, "files": ["x.jpg"]})
    out = server.ark_start_job("ark_edit_image", '{"intent": "paint it blue", "image": "a.jpg"}')
    assert out["ok"] is True
    assert out["status"] == "running"

    final = _wait(out["job_id"])
    assert final["status"] == "ok"
    assert final["result"]["echo"]["intent"] == "paint it blue"


def test_job_records_exceptions(monkeypatch):

    def boom(**kw):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(server, "ark_edit_image", boom)
    out = server.ark_start_job("ark_edit_image", '{"intent": "x", "image": "a.jpg"}')
    final = _wait(out["job_id"])
    assert final["status"] == "error"
    assert "model exploded" in final["error"]


def test_unknown_job_id_is_reported():
    out = server.ark_job_status("does-not-exist")
    assert out["ok"] is False
    assert "unknown job_id" in out["error"]


def test_status_reports_running_immediately(monkeypatch):
    monkeypatch.setattr(server, "ark_edit_image",
                        lambda **kw: (time.sleep(3), {"ok": True})[1])
    out = server.ark_start_job("ark_edit_image", '{"intent": "x", "image": "a.jpg"}')
    first = server.ark_job_status(out["job_id"], wait_seconds=0)
    assert first["status"] == "running"
    assert first["hint"]
    assert _wait(out["job_id"])["status"] == "ok"


def test_wait_seconds_is_capped(monkeypatch):
    """A long blocking poll would itself hit the proxy timeout."""
    monkeypatch.setattr(server, "ark_edit_image",
                        lambda **kw: (time.sleep(2), {"ok": True})[1])
    out = server.ark_start_job("ark_edit_image", '{"intent": "x", "image": "a.jpg"}')
    started = time.time()
    server.ark_job_status(out["job_id"], wait_seconds=999)
    assert time.time() - started < 65
    _wait(out["job_id"])


def test_done_jobs_are_swept(monkeypatch):
    server._JOBS.clear()
    server._JOBS["old"] = {"id": "old", "tool": "t", "status": "ok", "done": True,
                           "result": {}, "error": None, "started": 0,
                           "updated": time.time() - server._JOB_TTL - 10}
    server._sweep_jobs()
    assert "old" not in server._JOBS


def test_running_jobs_are_not_swept():
    server._JOBS.clear()
    server._JOBS["live"] = {"id": "live", "tool": "t", "status": "running", "done": False,
                            "result": None, "error": None, "started": 0,
                            "updated": time.time() - server._JOB_TTL - 10}
    server._sweep_jobs()
    assert "live" in server._JOBS
    server._JOBS.clear()


def test_instructions_describe_the_job_workflow():
    text = server.mcp.instructions
    assert "ark_start_job" in text
    assert "ark_job_status" in text
    assert "524" in text
