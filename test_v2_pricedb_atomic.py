import multiprocessing
import threading
import time

import pytest

from v2 import pricedb


def _row(index, artnr):
    return {
        "dedup_idx": index,
        "review_status": "approved",
        "selected_candidate_idx": 0,
        "candidates": [{
            "candidate_idx": 0,
            "our_artnr": artnr,
            "eligible": True,
        }],
    }


def test_pricedb_replace_failure_preserves_previous_database(tmp_path, monkeypatch):
    monkeypatch.setattr(pricedb, "V2_PRICEDB_PATH", tmp_path / "pricedb.jsonl")
    pricedb.commit_job("old", [_row(0, "10000")])

    def fail_replace(_source, _target):
        raise OSError("diskfeil")

    monkeypatch.setattr(pricedb.os, "replace", fail_replace)
    with pytest.raises(OSError, match="diskfeil"):
        pricedb.commit_job("new", [_row(1, "10001")])

    assert [record["job_id"] for record in pricedb.load_all()] == ["old"]


def test_concurrent_pricedb_commits_keep_both_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(pricedb, "V2_PRICEDB_PATH", tmp_path / "pricedb.jsonl")
    barrier = threading.Barrier(2)

    def commit(job_id, artnr):
        barrier.wait()
        pricedb.commit_job(job_id, [_row(0, artnr)])

    threads = [
        threading.Thread(target=commit, args=("a", "10000")),
        threading.Thread(target=commit, args=("b", "10001")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert {record["job_id"] for record in pricedb.load_all()} == {"a", "b"}


def test_multiprocess_pricedb_commits_keep_both_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(pricedb, "V2_PRICEDB_PATH", tmp_path / "pricedb.jsonl")
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    real_read = pricedb._read_all

    def synchronized_read():
        records = real_read()
        time.sleep(0.2)
        return records

    monkeypatch.setattr(pricedb, "_read_all", synchronized_read)

    def commit(job_id, artnr):
        barrier.wait(timeout=5)
        pricedb.commit_job(job_id, [_row(0, artnr)])

    processes = [
        context.Process(target=commit, args=("a", "10000")),
        context.Process(target=commit, args=("b", "10001")),
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    monkeypatch.setattr(pricedb, "_read_all", real_read)
    assert {record["job_id"] for record in pricedb.load_all()} == {"a", "b"}
