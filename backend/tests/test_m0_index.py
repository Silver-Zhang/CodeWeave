import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from codeweave.index import IndexError, build_index, query


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "m0"


class M0IndexTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.analyzer = ROOT / ".build" / "analyzer" / "codeweave-analyzer"
        if not cls.analyzer.exists():
            raise unittest.SkipTest("build analyzer first")

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="codeweave-m0-test-"))
        self.source = self.directory / "source"
        shutil.copytree(FIXTURE, self.source)
        self.build = self.directory / "build"
        subprocess.run(["cmake", "-S", str(self.source), "-B", str(self.build), "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON"], check=True, capture_output=True, text=True)
        self.database = self.directory / "index.sqlite3"
        self.result = build_index(self.database, self.build, self.analyzer)

    def tearDown(self):
        shutil.rmtree(self.directory)

    def test_expected_symbols_and_locations(self):
        result = query(self.database, "search", "overloaded")
        self.assertEqual({item["usr"] for item in result["items"]}, {"c:@F@overloaded#I#", "c:@F@overloaded#d#"})
        integer = query(self.database, "show", "c:@F@overloaded#I#")["items"][0]
        self.assertTrue(integer["definition_file"].endswith("alpha.cpp"))
        self.assertEqual(integer["definition_line"], 3)

    def test_calls_preserve_call_sites_and_unknown_dispatch(self):
        integer = query(self.database, "show", "c:@F@overloaded#I#")["items"][0]
        callers = query(self.database, "callers", integer["usr"])["items"]
        self.assertEqual([(item["dispatch"], item["line"]) for item in callers], [("direct", 5), ("direct", 7)])
        unknown = [item for item in query(self.database, "callees", "c:@F@use_callback#*FI(#I)#")["items"] if item["dispatch"] == "unknown-function-pointer"]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["line"], 7)
        entry = query(self.database, "search", "entry")["items"][0]
        callees = query(self.database, "callees", entry["usr"])["items"]
        self.assertEqual([(item["dispatch"], item["other_name"]) for item in callees], [("direct", "local"), ("direct", "overloaded")])
        virtual_user = query(self.database, "search", "virtual_user")["items"][0]
        virtual_calls = query(self.database, "callees", virtual_user["usr"])["items"]
        self.assertEqual([(item["dispatch"], item["other_name"], item["line"]) for item in virtual_calls], [("virtual", "Base::run", 18)])
        main = query(self.database, "search", "main")["items"][0]
        main_calls = query(self.database, "callees", main["usr"])["items"]
        self.assertFalse(any(item["line"] == 21 for item in main_calls))

    def test_snapshot_survives_source_changes_and_new_process(self):
        integer = query(self.database, "show", "c:@F@overloaded#I#")["items"][0]
        old_path = integer["definition_file"]
        source = query(self.database, "source", old_path, start_line=3, end_line=3)
        self.assertIn("value + 1", source["content"])
        Path(old_path).write_text("\n" * 30 + "int changed() {}\n", encoding="utf-8")
        program = "from pathlib import Path; from codeweave.index import query; import json; print(json.dumps(query(Path(" + repr(str(self.database)) + "), 'source', " + repr(old_path) + ", start_line=3, end_line=3)))"
        persisted = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True, check=False, env={**os.environ, "PYTHONPATH": str(ROOT / "backend")}, cwd=ROOT)
        self.assertEqual(persisted.returncode, 0, persisted.stderr)
        self.assertIn("value + 1", persisted.stdout)

    def test_invalid_inputs_and_failed_index_keep_previous_database(self):
        with self.assertRaises(IndexError):
            query(self.database, "source", "/etc/passwd", start_line=1, end_line=1)
        with self.assertRaises(IndexError):
            build_index(self.database, self.directory / "missing-build", self.analyzer)
        self.assertEqual(query(self.database, "status")["index_id"], self.result["index_id"])


if __name__ == "__main__":
    unittest.main()
