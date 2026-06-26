"""Unit tests for src/session_store.py — atomic JSON persistence of thread records."""

import json
from pathlib import Path

import session_store


def _p(tmp_path) -> Path:
    return tmp_path / "data" / "sessions.json"


class TestLoad:
    def test_missing_file_returns_empty(self, tmp_path):
        assert session_store.load(_p(tmp_path)) == {}

    def test_unreadable_garbage_returns_empty(self, tmp_path):
        path = _p(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json")
        assert session_store.load(path) == {}

    def test_round_trips_a_record(self, tmp_path):
        path = _p(tmp_path)
        data = {
            "1700000000.0001": {
                "session_id": "uuid-1",
                "cwd": "/abs/proj",
                "plugin_dir": None,
                "in_flight": False,
                "pid": None,
            }
        }
        session_store.save(data, path)
        assert session_store.load(path) == data


class TestSaveAtomic:
    def test_creates_parent_dir(self, tmp_path):
        path = _p(tmp_path)
        assert not path.parent.exists()
        session_store.save({}, path)
        assert path.exists()

    def test_no_leftover_temp_files(self, tmp_path):
        path = _p(tmp_path)
        session_store.save({"t": {"session_id": "x", "cwd": None,
                                  "plugin_dir": None, "in_flight": False, "pid": None}}, path)
        leftovers = [p.name for p in path.parent.iterdir() if p.name != "sessions.json"]
        assert leftovers == []

    def test_overwrite_replaces_content(self, tmp_path):
        path = _p(tmp_path)
        session_store.save({"a": {"session_id": "1", "cwd": None,
                                  "plugin_dir": None, "in_flight": False, "pid": None}}, path)
        session_store.save({"b": {"session_id": "2", "cwd": None,
                                  "plugin_dir": None, "in_flight": False, "pid": None}}, path)
        loaded = json.loads(path.read_text())
        assert set(loaded) == {"b"}


class TestUpsert:
    def test_creates_record_with_defaults(self, tmp_path):
        path = _p(tmp_path)
        data = session_store.upsert("T1", session_id="s1", cwd="/c", path=path)
        assert data["T1"] == {
            "session_id": "s1", "cwd": "/c",
            "plugin_dir": None, "in_flight": False, "pid": None, "channel": None,
        }

    def test_merges_only_provided_fields(self, tmp_path):
        path = _p(tmp_path)
        session_store.upsert("T1", session_id="s1", cwd="/c",
                             plugin_dir="/p", path=path)
        data = session_store.upsert("T1", in_flight=True, pid=4242, path=path)
        rec = data["T1"]
        assert rec["session_id"] == "s1"      # untouched
        assert rec["cwd"] == "/c"             # untouched
        assert rec["plugin_dir"] == "/p"      # untouched
        assert rec["in_flight"] is True       # changed
        assert rec["pid"] == 4242             # changed

    def test_can_write_pid_none_explicitly(self, tmp_path):
        path = _p(tmp_path)
        session_store.upsert("T1", session_id="s", in_flight=True, pid=99, path=path)
        data = session_store.upsert("T1", in_flight=False, pid=None, path=path)
        assert data["T1"]["in_flight"] is False
        assert data["T1"]["pid"] is None

    def test_upsert_persists_to_disk(self, tmp_path):
        path = _p(tmp_path)
        session_store.upsert("T1", session_id="s1", path=path)
        assert session_store.load(path)["T1"]["session_id"] == "s1"

    def test_can_store_channel(self, tmp_path):
        path = _p(tmp_path)
        data = session_store.upsert("T1", channel="C9", path=path)
        assert data["T1"]["channel"] == "C9"


def test_sessions_path_is_under_repo_data_dir():
    assert session_store.SESSIONS_PATH.name == "sessions.json"
    assert session_store.SESSIONS_PATH.parent.name == "data"
