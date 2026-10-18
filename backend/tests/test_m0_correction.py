import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from codeweave.index import IndexError, build_index, query


ROOT = Path(__file__).resolve().parents[2]
ANALYZER = ROOT / ".build" / "analyzer" / "codeweave-analyzer"


class M0CorrectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ANALYZER.is_file():
            raise unittest.SkipTest("run the M0 acceptance build first")

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="codeweave-m0r-"))
        self.project = self.temp / "project"
        self.project.mkdir()
        (self.project / "main.cpp").write_text("""#include \"shared.hpp\"\nint later(int);\nint invoke() { return header_inline() + later(1); }\nint main() { return invoke(); }\n""", encoding="utf-8")
        (self.project / "other.cpp").write_text("""#include \"shared.hpp\"\nint later(int value) { return value + header_inline(); }\n""", encoding="utf-8")
        (self.project / "shared.hpp").write_text("""#pragma once\ninline int header_inline() { return 4; }\n""", encoding="utf-8")
        self.build = self.temp / "build"
        subprocess.run(["cmake", "-S", str(self._write_cmake()), "-B", str(self.build), "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"], check=True, capture_output=True, text=True)
        self.database = self.temp / "index.sqlite3"

    def tearDown(self):
        shutil.rmtree(self.temp)

    def _write_cmake(self) -> Path:
        (self.project / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.20)\nproject(m0r LANGUAGES CXX)\nadd_executable(m0r main.cpp other.cpp)\n", encoding="utf-8")
        return self.project

    def test_safe_index_and_header_definition(self):
        result = build_index(self.database, self.build, ANALYZER)
        self.assertEqual(result["coverage"], "units_complete")
        later = query(self.database, "search", "later")["items"][0]
        self.assertTrue(later["definition_file"].endswith("other.cpp"))
        header = query(self.database, "search", "header_inline")["items"][0]
        self.assertTrue(header["definition_file"].endswith("shared.hpp"))

    def test_rejects_output_and_response_side_effects(self):
        commands = json.loads((self.build / "compile_commands.json").read_text())
        sentinel = self.temp / "sentinel"
        sentinel.write_text("unchanged", encoding="utf-8")
        commands[0]["arguments"] = commands[0]["command"].split() + ["-serialize-diagnostics", str(sentinel)] if "arguments" not in commands[0] else commands[0]["arguments"] + ["-serialize-diagnostics", str(sentinel)]
        (self.build / "compile_commands.json").write_text(json.dumps(commands), encoding="utf-8")
        with self.assertRaisesRegex(IndexError, "rejected compiler argument"):
            build_index(self.database, self.build, ANALYZER)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")

    def test_line_directive_uses_physical_file(self):
        private = self.temp / "private.txt"
        private.write_text("PRIVATE_SENTINEL", encoding="utf-8")
        (self.project / "main.cpp").write_text(f'#line 1 "{private}"\nint physical() {{ return 0; }}\n', encoding="utf-8")
        result = build_index(self.database, self.build, ANALYZER)
        symbol = query(self.database, "search", "physical")["items"][0]
        self.assertTrue(symbol["definition_file"].endswith("main.cpp"))
        with self.assertRaises(IndexError):
            query(self.database, "source", str(private), start_line=1, end_line=1)
        self.assertEqual(query(self.database, "status")["index_id"], result["index_id"])

    def test_cli_errors_are_json(self):
        build_index(self.database, self.build, ANALYZER)
        completed = subprocess.run([sys.executable, "-m", "codeweave", "query", "show", "--database", str(self.database)], cwd=ROOT, text=True, capture_output=True, env={**os.environ, "PYTHONPATH": str(ROOT / "backend")})
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(json.loads(completed.stderr)["error"]["code"], "missing_argument")

    def test_source_limits_and_duplicate_commands(self):
        commands = json.loads((self.build / "compile_commands.json").read_text())
        commands.append(commands[0])
        (self.build / "compile_commands.json").write_text(json.dumps(commands), encoding="utf-8")
        build_index(self.database, self.build, ANALYZER)
        invoke = query(self.database, "search", "invoke")["items"][0]
        calls = query(self.database, "callees", invoke["usr"])["items"]
        self.assertEqual(len(calls), 2)
        main_path = str(self.project / "main.cpp")
        with self.assertRaisesRegex(IndexError, "exceeds 200 lines"):
            query(self.database, "source", main_path, start_line=1, end_line=201)


if __name__ == "__main__":
    unittest.main()
