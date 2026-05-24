import importlib.util
import py_compile
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "natvis_to_lldb.py"
TOOL_TXT = ROOT / "tools" / "natvis_to_lldb_txt.py"

spec = importlib.util.spec_from_file_location("natvis_to_lldb", TOOL)
natvis_to_lldb = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["natvis_to_lldb"] = natvis_to_lldb
spec.loader.exec_module(natvis_to_lldb)

txt_spec = importlib.util.spec_from_file_location("natvis_to_lldb_txt", TOOL_TXT)
natvis_to_lldb_txt = importlib.util.module_from_spec(txt_spec)
assert txt_spec.loader is not None
txt_spec.loader.exec_module(natvis_to_lldb_txt)


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

    def test_custom_list_items_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "custom_list_items.natvis"
            path.write_text(
                """<?xml version="1.0" encoding="utf-8"?>
<AutoVisualizer xmlns="http://schemas.microsoft.com/vstudio/debugger/natvis/2010">
  <Type Name="demo::BucketList">
    <DisplayString>size={size_}</DisplayString>
    <Expand>
      <CustomListItems MaxItemsPerView="128">
        <Variable Name="i" InitialValue="0" />
        <Size>size_</Size>
        <Loop Condition="i &lt; size_">
          <Item Name="[{i}]">data_[i],na</Item>
          <Exec>i++</Exec>
        </Loop>
      </CustomListItems>
    </Expand>
  </Type>
</AutoVisualizer>
""",
                encoding="utf-8",
            )
            result = natvis_to_lldb.parse_natvis(path)

        self.assertEqual(result.warnings, [])
        custom_list = result.types[0].custom_list_items[0]
        self.assertEqual(custom_list.max_items_per_view, 128)
        self.assertEqual(custom_list.size, "size_")
        self.assertEqual(custom_list.variables[0].name, "i")
        self.assertEqual(custom_list.steps[0].kind, "Loop")
        self.assertEqual(custom_list.steps[0].children[0].kind, "Item")

        generated = natvis_to_lldb.generate_lldb_formatter(
            result.types,
            category="test_natvis",
            source_name="custom_list_items.natvis",
        )
        with tempfile.TemporaryDirectory() as tmp:
            generated_path = Path(tmp) / "generated.py"
            generated_path.write_text(generated, encoding="utf-8")
            py_compile.compile(str(generated_path), doraise=True)

    def test_txt_entrypoint_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            command_file = tmp_path / "lldb_formatters.txt"
            python_file = tmp_path / "lldb_formatters.py"

            exit_code = natvis_to_lldb_txt.main(
                [
                    str(ROOT / "examples" / "sample.natvis"),
                    "-o",
                    str(command_file),
                    "--python-output",
                    str(python_file),
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(command_file.exists())
            self.assertTrue(python_file.exists())
            self.assertIn(
                "command script import",
                command_file.read_text(encoding="utf-8"),
            )
            self.assertIn(
                str(python_file.resolve()),
                command_file.read_text(encoding="utf-8"),
            )
            py_compile.compile(str(python_file), doraise=True)


if __name__ == "__main__":
    unittest.main()
