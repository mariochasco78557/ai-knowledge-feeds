"""local_md source: importer is idempotent, offline."""
import json
import os
import sys
from pathlib import Path

import tempfile
import unittest


class LocalMdTests(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.addCleanup(os.environ.pop, "AI_KB_ROOT", None)
        self.root, self.cfg = self._make(Path(self._td.name))

    def _make(self, tmp_path):
        src = tmp_path / "out"
        (src / "sub").mkdir(parents=True)
        (src / "one.md").write_text("---\ntitle: Demo note\npublish_date: 2026-01-05\nkind: resource\n---\n\n# Demo note\n\nA long enough sentence about golden set evals and judges here.\n")
        (src / "sub" / "two.md").write_text("# Second\n\nAnother long enough paragraph about golden set evaluation for readers.\n")
        (src / "README.md").write_text("skip me\n")
        root = tmp_path / "kbroot"
        (root / "data").mkdir(parents=True)
        os.environ["AI_KB_ROOT"] = str(root)
        for m in [m for m in sys.modules if m == "kb" or m.startswith("kb.")]:
            del sys.modules[m]
        cfg = {"key": "my-notes", "type": "local_md", "authority": "practice", "private": True,
               "importer": "generic_md", "id_prefix": "mn", "import_dir": str(src)}
        return root, cfg

    def test_import_idempotent_and_indexable(self):
        root, cfg = self.root, self.cfg
        from kb import db
        from kb.sources import local_md

        counts = local_md.import_local(cfg)
        self.assertEqual(counts, {"doc": 2})
        files = sorted(p.name for p in (root / "data" / "my-notes").rglob("*.md"))
        self.assertEqual(len(files), 2)
        manifest = root / "data" / "my-notes" / ".manifest.json"
        self.assertEqual(len(json.loads(manifest.read_text())["files"]), 2)
        mtimes = {p: p.stat().st_mtime_ns for p in (root / "data" / "my-notes").rglob("*.md")}
        self.assertEqual(local_md.import_local(cfg), counts)
        self.assertEqual(mtimes, {p: p.stat().st_mtime_ns for p in (root / "data" / "my-notes").rglob("*.md")})

        db.rebuild()
        hits = db.search("golden set", source="my-notes")
        self.assertEqual({h["id"] for h in hits}, {"mn:doc:one", "mn:doc:sub-two"})

    def test_removed_source_file_is_pruned(self):
        root, cfg = self.root, self.cfg
        from kb.sources import local_md

        local_md.import_local(cfg)
        (Path(cfg["import_dir"]) / "sub" / "two.md").unlink()
        local_md.import_local(cfg)
        self.assertEqual(len(list((root / "data" / "my-notes").rglob("*.md"))), 1)

    def test_unknown_importer_rejected(self):
        cfg = self.cfg
        from kb.sources import local_md

        with self.assertRaises(ValueError):
            local_md.import_local({**cfg, "importer": "nope"})

if __name__ == "__main__":
    unittest.main()
