import importlib.util
import py_compile
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "natvis_to_lldb.py"

spec = importlib.util.spec_from_file_location("natvis_to_lldb", TOOL)
natvis_to_lldb = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["natvis_to_lldb"] = natvis_to_lldb
spec.loader.exec_module(natvis_to_lldb)


class NatvisToLldbTests(unittest.TestCase):
    def test_parse_display_items_and_array_items(self):
        result = natvis_to_lldb.parse_natvis(ROOT / "examples" / "sample.natvis")

        self.assertEqual(result.warnings, [])
        self.assertEqual(len(result.types), 1)

        formatter = result.types[0]
        self.assertEqual(formatter.name, "demo::SmallVector<*>")
        self.assertEqual(formatter.display_string, "size={size_}")
        self.assertEqual(len(formatter.items), 2)
        self.assertEqual(formatter.array_items.size, "size_")
        self.assertEqual(formatter.array_items.value_pointer, "data_")
        self.assertTrue(formatter.regex.startswith("^demo::SmallVector"))

    def test_generated_formatter_is_valid_python(self):
        result = natvis_to_lldb.parse_natvis(ROOT / "examples" / "sample.natvis")
        generated = natvis_to_lldb.generate_lldb_formatter(
            result.types,
            category="test_natvis",
            source_name="sample.natvis",
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "generated.py"
            path.write_text(generated, encoding="utf-8")
            py_compile.compile(str(path), doraise=True)

    def test_unsupported_nodes_emit_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unsupported.natvis"
            path.write_text(
                """<?xml version="1.0" encoding="utf-8"?>
<AutoVisualizer xmlns="http://schemas.microsoft.com/vstudio/debugger/natvis/2010">
  <Type Name="demo::List">
    <Expand>
      <LinkedListItems>
        <HeadPointer>head_</HeadPointer>
        <NextPointer>next_</NextPointer>
        <ValueNode>value_</ValueNode>
      </LinkedListItems>
    </Expand>
  </Type>
</AutoVisualizer>
""",
                encoding="utf-8",
            )
            result = natvis_to_lldb.parse_natvis(path)

        self.assertEqual(result.types, [])
        self.assertTrue(any("LinkedListItems" in item for item in result.warnings))


if __name__ == "__main__":
    unittest.main()
