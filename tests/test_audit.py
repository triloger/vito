from __future__ import annotations

import json

from vito.exec.audit import GENESIS, AuditLog


def test_append_chains_records(tmp_path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    h0 = log.append({"event": "armed", "mode": "paper"})
    h1 = log.append({"event": "order", "side": "long", "notional": 300.0})
    h2 = log.append({"event": "refused", "reason": "daily_halt"})

    lines = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert [line["seq"] for line in lines] == [0, 1, 2]
    assert lines[0]["prev"] == GENESIS
    assert lines[1]["prev"] == h0
    assert lines[2]["prev"] == h1
    assert lines[2]["hash"] == h2


def test_verify_accepts_intact_chain(tmp_path) -> None:
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(5):
        log.append({"i": i})
    assert log.verify()


def test_verify_detects_tampering(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(3):
        log.append({"i": i})

    lines = path.read_text().splitlines()
    record = json.loads(lines[1])
    record["event"]["i"] = 99
    lines[1] = json.dumps(record, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")

    assert not AuditLog(path).verify()


def test_reopening_continues_the_chain(tmp_path) -> None:
    path = tmp_path / "audit.jsonl"
    first = AuditLog(path)
    first.append({"a": 1})

    second = AuditLog(path)
    second.append({"a": 2})

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert lines[1]["prev"] == lines[0]["hash"]
    assert second.verify()
