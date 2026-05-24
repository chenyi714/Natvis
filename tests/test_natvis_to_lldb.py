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

    def test_xml_comments_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "comments.natvis"
            path.write_text(
                """<?xml version="1.0" encoding="utf-8"?>
<AutoVisualizer xmlns="http://schemas.microsoft.com/vstudio/debugger/natvis/2010">
  <!-- top-level comment -->
  <Type Name="demo::WithComments">
    <DisplayString>value={value_}</DisplayString>
    <Expand>
      <!-- comments inside Expand used to crash _local_name(child.tag) -->
      <Item Name="[value]">value_</Item>
    </Expand>
  </Type>
</AutoVisualizer>
""",
                encoding="utf-8",
            )
            result = natvis_to_lldb.parse_natvis(path)

        self.assertEqual(result.warnings, [])
        self.assertEqual(len(result.types), 1)
        self.assertEqual(result.types[0].items[0].expression, "value_")

    def test_conditions_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "conditions.natvis"
            path.write_text(
                """<?xml version="1.0" encoding="utf-8"?>
<AutoVisualizer xmlns="http://schemas.microsoft.com/vstudio/debugger/natvis/2010">
  <Type Name="demo::Conditional" Condition="is_valid_">
    <DisplayString>kind={kind_}</DisplayString>
    <Expand>
      <Item Name="[small]" Condition="kind_ == 0">small_</Item>
      <Item Name="[large]" Condition="kind_ != 0">large_</Item>
      <ArrayItems Condition="data_ != 0">
        <Size>size_</Size>
        <ValuePointer>data_</ValuePointer>
      </ArrayItems>
    </Expand>
  </Type>
</AutoVisualizer>
""",
                encoding="utf-8",
            )
            result = natvis_to_lldb.parse_natvis(path)

        self.assertEqual(result.warnings, [])
        self.assertEqual(result.types[0].condition, "is_valid_")
        self.assertEqual(result.types[0].items[0].condition, "kind_ == 0")
        self.assertEqual(result.types[0].items[1].condition, "kind_ != 0")
        self.assertEqual(result.types[0].array_items.condition, "data_ != 0")

    def test_alternative_types_reuse_visualization(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alternative_types.natvis"
            path.write_text(
                """<?xml version="1.0" encoding="utf-8"?>
<AutoVisualizer xmlns="http://schemas.microsoft.com/vstudio/debugger/natvis/2010">
  <Type Name="demo::Primary&lt;*&gt;" Condition="enabled_">
    <AlternativeType Name="demo::Alias&lt;*&gt;" />
    <AlternativeType Name="demo::AliasWhenReady" Condition="ready_" />
    <DisplayString>size={size_}</DisplayString>
    <Expand>
      <Item Name="[size]">size_</Item>
    </Expand>
  </Type>
</AutoVisualizer>
""",
                encoding="utf-8",
            )
            result = natvis_to_lldb.parse_natvis(path)

        self.assertEqual(result.warnings, [])
        self.assertEqual([item.name for item in result.types], [
            "demo::Primary<*>",
            "demo::Alias<*>",
            "demo::AliasWhenReady",
        ])
        self.assertEqual(result.types[1].display_string, "size={size_}")
        self.assertEqual(result.types[1].items[0].expression, "size_")
        self.assertEqual(result.types[1].condition, "enabled_")
        self.assertEqual(result.types[2].condition, "ready_")


if __name__ == "__main__":
    unittest.main()
