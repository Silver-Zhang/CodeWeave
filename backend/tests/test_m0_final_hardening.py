"""M0-R3: raw response-token validation and bounded status; no simulated Clang."""
import base64
import hashlib
import json
import shlex
import sqlite3
import subprocess

import pytest

from codeweave.index import MAX_STATUS_BYTES, SCHEMA
from test_m0_r2 import ANALYZER, cli, commands, index, project, q, sha, write


def size(value):
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()) + 1


def paginate(db, kind, limit=20):
    items, offset = [], 0
    while True:
        result = q(db, "status", "--details", kind, "--limit", limit, "--offset", offset)
        assert size(result) <= MAX_STATUS_BYTES
        assert result["returned_count"] == len(result["items"]) <= limit
        assert result["remaining_count"] == max(0, result["total_count"] - offset - len(result["items"]))
        items.extend(result["items"])
        if not result["has_more"]:
            assert result["next_offset"] is None
            break
        assert result["next_offset"] > offset
        offset = result["next_offset"]
    return items


def field_bytes(db, field, details=None, offset=0):
    pieces, position, digest = [], 0, None
    args = ["--field", field]
    if details:
        args += ["--details", details, "--limit", 1, "--offset", offset]
    while True:
        result = q(db, "status", *args, "--field-offset", position)
        assert size(result) <= MAX_STATUS_BYTES
        raw = base64.b64decode(result["content"], validate=True)
        assert result["returned_bytes"] == len(raw) <= 4096
        assert result["field_offset"] == position
        if digest is not None:
            assert result["field_sha256"] == digest
        digest = result["field_sha256"]
        pieces.append(raw)
        if not result["has_more"]:
            assert result["next_field_offset"] is None
            break
        position = result["next_field_offset"]
    value = b"".join(pieces)
    assert len(value) == result["field_bytes"] and hashlib.sha256(value).hexdigest() == digest
    return value


@pytest.mark.parametrize("format", ["arguments", "command"])
@pytest.mark.parametrize("prefix", [[], ["-D"], ["-U"], ["-I"], ["-include"], ["-imacros"], ["-std"], ["-x"], ["--target"], ["-o"]])
def test_C_reject_response_before_analyzer(project, tmp_path, format, prefix):
    write(project, "main.cpp", "int main() { return 0; }\n")
    sentinel = write(tmp_path, "sentinel", "KEEP")
    rsp = write(project, "options with spaces.rsp", f'VALUE -DCHANGED=1 --serialize-diagnostics {sentinel}\n')
    guard = write(tmp_path, "guard.py", '#!/usr/bin/python3\nimport sys\nprint("CLANG_PHASE_ENTERED",file=sys.stderr)\nsys.exit(77)\n')
    guard.chmod(0o755)
    args = ["c++", *prefix, "@" + str(rsp), "-c", "main.cpp"]
    entry = {"directory": str(project), "file": "main.cpp", format: args if format == "arguments" else shlex.join(args)}
    write(project, "compile_commands.json", json.dumps([entry]))
    error = cli("index", "--project-root", project, "--compilation-database", project, "--database", tmp_path / "index.db", "--analyzer", guard, code=2)
    assert error["error"]["code"] == "unsafe_compilation_argument"
    assert "CLANG_PHASE_ENTERED" not in error["error"]["message"]
    assert sentinel.read_text() == "KEEP" and not (tmp_path / "index.db").exists()


@pytest.mark.parametrize("format", ["arguments", "command"])
def test_C_response_spellings_and_normal_parameters(project, tmp_path, format):
    write(project, "inc/config.hpp", "#define HEADER 5\n")
    write(project, "main.cpp", '#include "config.hpp"\nint target() { return HEADER+NORMAL; }\nint main() { return target(); }\n')
    for token in ["@", "@@nested.rsp", "@relative.rsp", "@/not-authorized/args.rsp"]:
        args = ["c++", "-D", token, "-c", "main.cpp"]
        entry = {"directory": str(project), "file": "main.cpp", format: args if format == "arguments" else shlex.join(args)}
        write(project, "compile_commands.json", json.dumps([entry]))
        assert index(project, tmp_path / "index.db", code=2)["error"]["code"] == "unsafe_compilation_argument"
    # Embedded @ is ordinary macro text, not an LLVM leading-@ token.
    args = ["c++", "-DMAIL=\"a@b\"", "-DNORMAL=7", "-I", "inc", "-std=c++17", "-c", "main.cpp"]
    write(project, "compile_commands.json", json.dumps([{"directory": str(project), "file": "main.cpp", format: args if format == "arguments" else shlex.join(args)}]))
    db = tmp_path / "index.db"
    index(project, db)
    target = q(db, "search", "target")["items"][0]
    main = q(db, "search", "main")["items"][0]
    assert q(db, "callees", main["usr"])["items"][0]["target_usr"] == target["usr"]
    command = q(db, "status", "--details", "units", "--limit", 1)["items"][0]["command"]
    assert "-DMAIL=\"a@b\"" in command["arguments"] and "-DNORMAL=7" in command["arguments"]


def test_B_llvm_really_expands_option_value(project, tmp_path):
    """Direct internal component on owned input establishes the LLVM behavior."""
    write(project, "main.cpp", 'int old_target(); int new_target();\nint choose() {\n#ifdef CHANGED\n return new_target();\n#else\n return old_target();\n#endif\n}\n')
    rsp = write(project, "options.rsp", "UNUSED -DCHANGED=1\n")
    rows = [{"directory": str(project), "file": str(project / "main.cpp"), "arguments": ["/usr/bin/c++", "-D", "@" + str(rsp), "-c", str(project / "main.cpp")]}]
    write(project, "compile_commands.json", json.dumps(rows))
    facts = tmp_path / "facts.jsonl"
    p = subprocess.run([str(ANALYZER), "--project-root", str(project), "--compilation-database", str(project), "--output", str(facts)], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, p.stderr
    records = [json.loads(line) for line in facts.read_text().splitlines()]
    assert [r["target_usr"] for r in records if r["type"] == "call"] == ["c:@F@new_target#"]


def test_C_status_300_real_units_and_inputs(project, tmp_path):
    write(project, "inc/config.hpp", "#define HEADER 5\n")
    files = []
    for i in range(300):
        name = f"src/u{i:03}.cpp"
        write(project, name, '#include "config.hpp"\n' + ('#warning counted_warning\n' if i == 0 else '') + f'int unit_{i}() {{ return HEADER; }}\n')
        files.append(name)
    commands(project, files)
    db = tmp_path / "index.db"
    result = index(project, db)
    assert result["units"] == 300
    before = sha(db)
    summary = q(db, "status", "--limit", 1)
    assert summary["unit_count"] == 300 and summary["input_count"] == 301
    assert summary["diagnostic_summary"] == {"error": 0, "warning": 1, "note": 0}
    assert "units" not in summary and "inputs" not in summary and "toolchain" not in summary
    assert size(summary) < 4096
    for kind, key, total in [("units", "source_file", 300), ("inputs", "path", 301)]:
        first = q(db, "status", "--details", kind, "--limit", 1)
        assert first["returned_count"] == len(first["items"]) == 1
        assert first["total_count"] == total and first["remaining_count"] == total - 1
        all_items = paginate(db, kind)
        values = [item[key] for item in all_items]
        assert len(values) == total and len(set(values)) == total and values == sorted(values)
        middle = q(db, "status", "--details", kind, "--limit", 20, "--offset", 137)
        again = q(db, "status", "--details", kind, "--limit", 20, "--offset", 137)
        assert middle == again and middle["items"] == all_items[137:157]
    assert sha(db) == before


@pytest.fixture
def extreme_db(tmp_path):
    """Synthetic stored metadata, not a substitute for the real 300-TU test."""
    db = tmp_path / "extreme.db"
    command = json.dumps({"arguments": ["c++", "-DLONG=" + "汉\\n\"" * 8000]}, ensure_ascii=False)
    diagnostics = json.dumps([{"level": "warning", "message": "long诊断\\n\"" * 4000}], ensure_ascii=False)
    toolchain = json.dumps({"roots": ["工具链" * 10000]}, ensure_ascii=False)
    with sqlite3.connect(db) as c:
        c.executescript(SCHEMA)
        c.execute("INSERT INTO indexes VALUES ('owned',CURRENT_TIMESTAMP,'ccdb','abc','project','units_complete','',3,?)", (toolchain,))
        for i in range(30):
            c.execute("INSERT INTO units VALUES ('owned',?,?,'complete',?,?)", (f"u{i:03}.cpp", str(i), diagnostics, command))
        c.execute("INSERT INTO files VALUES ('owned',?,NULL,'abc','dependency')", ("长路径" * 4000,))
    return db, command, diagnostics, toolchain


def test_A_C_status_extreme_fields_and_lossless_chunks(extreme_db):
    db, command, diagnostics, toolchain = extreme_db
    before = sha(db)
    summary = q(db, "status")
    assert size(summary) < 4096 and summary["diagnostic_summary"]["warning"] == 30
    page = q(db, "status", "--details", "units", "--limit", 200)
    assert size(page) <= MAX_STATUS_BYTES and page["budget_limited"] and page["has_more"]
    assert 0 < page["returned_count"] < 30
    unit = page["items"][0]
    for field, value in [("command", command), ("diagnostics", diagnostics)]:
        preview = unit[field]
        assert preview["truncated"] and preview["utf8_bytes"] == len(value.encode())
        assert preview["sha256"] == hashlib.sha256(value.encode()).hexdigest()
        assert field_bytes(db, field, "units") == value.encode()
    assert field_bytes(db, "toolchain") == toolchain.encode()
    path = q(db, "status", "--details", "inputs", "--limit", 1)["items"][0]["path"]
    assert path["truncated"] and field_bytes(db, "path", "inputs") == ("长路径" * 4000).encode()
    assert len(paginate(db, "units", 200)) == 30
    assert sha(db) == before


@pytest.mark.parametrize("args,error", [
    (["--offset", -1], "invalid_offset"),
    (["--details", "units", "--offset", 2**80], "invalid_offset"),
    (["--field-offset", 1], "invalid_arguments"),
    (["--details", "units", "--field", "command"], "invalid_arguments"),
    (["--details", "units", "--field", "toolchain", "--limit", 1], "invalid_arguments"),
    (["--details", "units", "--field", "command", "--limit", 1, "--offset", 999], "status_item_not_found"),
    (["--field", "toolchain", "--field-offset", 99999999], "invalid_offset"),
])
def test_C_status_invalid_parameters(extreme_db, args, error):
    db = extreme_db[0]
    assert q(db, "status", *args, code=2)["error"]["code"] == error
