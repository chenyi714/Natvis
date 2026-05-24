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

            exit_code = natvis_to_lldb_txt.main(
                [
                    str(ROOT / "examples" / "sample.natvis"),
                    "-o",
                    str(command_file),
                ]
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(command_file.exists())
            contents = command_file.read_text(encoding="utf-8")
            self.assertIn("type category define", contents)
            self.assertIn("type summary add", contents)
            self.assertIn('--summary-string "size=${var.size_}"', contents)
            self.assertNotIn("command script import", contents)

    def test_txt_summary_generation_warns_on_complex_expressions(self):
        warnings = []
        summary = natvis_to_lldb_txt.natvis_display_to_lldb_summary(
            "size={size_} ptr={data_ + 1}",
            "demo::Complex",
            warnings,
        )

        self.assertEqual(summary, "size=${var.size_} ptr=<unsupported:data_ + 1>")
        self.assertTrue(any("cannot be represented" in warning for warning in warnings))

    def test_txt_summary_generation_supports_pointer_paths(self):
        warnings = []
        summary = natvis_to_lldb_txt.natvis_display_to_lldb_summary(
            "node={node_->id} first={*count_ptr_,d} self={this->owner_->name}",
            "demo::PointerPaths",
            warnings,
        )

        self.assertEqual(
            summary,
            "node=${var.node_.id} first=${*var.count_ptr_%d} self=${var.owner_.name}",
        )
        self.assertEqual(warnings, [])

    def test_txt_summary_generation_supports_parenthesized_pointer_path(self):
        warnings = []
        summary = natvis_to_lldb_txt.natvis_display_to_lldb_summary(
            "node={(*node_).id}",
            "demo::PointerPaths",
            warnings,
        )

        self.assertEqual(summary, "node=${var.node_.id}")
        self.assertEqual(warnings, [])

    def test_txt_summary_generation_does_not_strip_c_style_pointer_casts_by_default(self):
        warnings = []
        summary = natvis_to_lldb_txt.natvis_display_to_lldb_summary(
            "tag={((entityImpl*)m_impl._Mypair.Myval2)->m_tag}",
            "demo::CastPath",
            warnings,
        )

        self.assertEqual(
            summary,
            "tag=<unsupported:((entityImpl*)m_impl._Mypair.Myval2)->m_tag>",
        )
        self.assertTrue(any("cannot be represented" in warning for warning in warnings))

    def test_txt_summary_generation_can_assume_c_style_pointer_casts(self):
        warnings = []
        summary = natvis_to_lldb_txt.natvis_display_to_lldb_summary(
            "tag={((entityImpl*)m_impl._Mypair.Myval2)->m_tag}",
            "demo::CastPath",
            warnings,
            assume_c_style_casts=True,
        )

        self.assertEqual(summary, "tag=${var.m_impl._Mypair.Myval2.m_tag}")
        self.assertEqual(warnings, [])

    def test_txt_summary_generation_can_assume_dereferenced_c_style_pointer_casts(self):
        warnings = []
        summary = natvis_to_lldb_txt.natvis_display_to_lldb_summary(
            "tag={(*(entityImpl*)m_impl._Mypair.Myval2).m_tag}",
            "demo::CastPath",
            warnings,
            assume_c_style_casts=True,
        )

        self.assertEqual(summary, "tag=${var.m_impl._Mypair.Myval2.m_tag}")
        self.assertEqual(warnings, [])

    def test_generated_formatter_dereferences_pointer_context(self):
        result = natvis_to_lldb.parse_natvis(ROOT / "examples" / "sample.natvis")
        generated = natvis_to_lldb.generate_lldb_formatter(
            result.types,
            category="test_natvis",
            source_name="sample.natvis",
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "generated.py"
            path.write_text(generated, encoding="utf-8")
            generated_spec = importlib.util.spec_from_file_location(
                "generated_pointer_context",
                path,
            )
            generated_module = importlib.util.module_from_spec(generated_spec)
            assert generated_spec.loader is not None
            generated_spec.loader.exec_module(generated_module)

        class FakeError:
            def Fail(self):
                return False

        class FakeType:
            def __init__(self, is_pointer=False):
                self.is_pointer = is_pointer

            def IsPointerType(self):
                return self.is_pointer

            def IsReferenceType(self):
                return False

        class FakeResult:
            def __init__(self, value, type_obj=None):
                self.value = value
                self.type_obj = type_obj or FakeType()

            def IsValid(self):
                return True

            def GetError(self):
                return FakeError()

            def GetValue(self):
                return str(self.value)

            def GetSummary(self):
                return None

            def GetValueAsUnsigned(self):
                return int(self.value)

            def GetData(self):
                return self.value

            def GetType(self):
                return self.type_obj

        class FakeObject:
            def __init__(self):
                self.evaluated = []
                self.member_reads = []
                self.created_from_data = []

            def IsValid(self):
                return True

            def GetNonSyntheticValue(self):
                return self

            def GetID(self):
                return 1

            def GetTypeName(self):
                return "demo::SmallVector<int>"

            def GetType(self):
                return FakeType()

            def GetChildMemberWithName(self, name):
                self.member_reads.append(name)
                if name == "size_":
                    return FakeResult(3)
                return None

            def EvaluateExpression(self, expr):
                self.evaluated.append(expr)
                return FakeResult(3)

            def CreateValueFromData(self, name, data, type_obj):
                self.created_from_data.append((name, data))
                return FakeResult(data, type_obj)

            def CreateValueFromExpression(self, name, expr):
                raise AssertionError("simple children should be copied from member data")

            def Dereference(self):
                return None

            def GetChildAtIndex(self, index):
                return None

        class FakePointer:
            def __init__(self, pointee):
                self.pointee = pointee

            def IsValid(self):
                return True

            def GetNonSyntheticValue(self):
                return self

            def GetID(self):
                return 2

            def GetTypeName(self):
                return "demo::SmallVector<int> *"

            def GetType(self):
                return FakeType(is_pointer=True)

            def Dereference(self):
                return self.pointee

            def EvaluateExpression(self, expr):
                raise AssertionError("pointer context should be dereferenced first")

            def CreateValueFromExpression(self, name, expr):
                raise AssertionError("pointer context should be dereferenced first")

            def GetChildMemberWithName(self, name):
                return None

            def GetChildAtIndex(self, index):
                return None

            def CreateValueFromData(self, name, data, type_obj):
                raise AssertionError("copied child should be created on pointee context")

        pointee = FakeObject()
        pointer = FakePointer(pointee)

        summary = generated_module.summary_provider(pointer, {})
        provider = generated_module.NatvisSyntheticProvider(pointer, {})
        child = provider.get_child_at_index(0)

        self.assertEqual(summary, "size=3")
        self.assertIsInstance(child, FakeResult)
        self.assertIn("size_", pointee.member_reads)
        self.assertIn(("[size]", 3), pointee.created_from_data)

    def test_generated_formatter_falls_back_to_evaluation_for_expressions(self):
        result = natvis_to_lldb.parse_natvis(ROOT / "examples" / "sample.natvis")
        generated = natvis_to_lldb.generate_lldb_formatter(
            result.types,
            category="test_natvis",
            source_name="sample.natvis",
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "generated.py"
            path.write_text(generated, encoding="utf-8")
            generated_spec = importlib.util.spec_from_file_location(
                "generated_expression_fallback",
                path,
            )
            generated_module = importlib.util.module_from_spec(generated_spec)
            assert generated_spec.loader is not None
            generated_spec.loader.exec_module(generated_module)

        class FakeError:
            def Fail(self):
                return False

        class FakeType:
            def IsPointerType(self):
                return False

            def IsReferenceType(self):
                return False

        class FakeResult:
            def __init__(self, value):
                self.value = value

            def IsValid(self):
                return True

            def GetError(self):
                return FakeError()

            def GetValue(self):
                return str(self.value)

            def GetSummary(self):
                return None

            def GetValueAsUnsigned(self):
                return int(self.value)

            def GetData(self):
                return self.value

            def GetType(self):
                return FakeType()

        class FakeObject:
            def __init__(self):
                self.evaluated = []
                self.created = []

            def IsValid(self):
                return True

            def GetNonSyntheticValue(self):
                return self

            def GetID(self):
                return 1

            def GetTypeName(self):
                return "demo::SmallVector<int>"

            def GetType(self):
                return FakeType()

            def GetChildMemberWithName(self, name):
                return None

            def EvaluateExpression(self, expr):
                self.evaluated.append(expr)
                return FakeResult(4)

            def CreateValueFromData(self, name, data, type_obj):
                self.created.append((name, data))
                return FakeResult(data)

            def CreateValueFromExpression(self, name, expr):
                raise AssertionError("evaluated children should be copied from data")

        fake = FakeObject()

        generated_module.FORMATTERS[0]["items"][0]["expression"] = "size_ + 1"
        provider = generated_module.NatvisSyntheticProvider(fake, {})
        child = provider.get_child_at_index(0)

        self.assertIsInstance(child, FakeResult)
        self.assertIn("size_ + 1", fake.evaluated)
        self.assertIn(("[size]", 4), fake.created)

    def test_generated_formatter_does_not_raise_when_lldb_calls_fail(self):
        result = natvis_to_lldb.parse_natvis(ROOT / "examples" / "sample.natvis")
        generated = natvis_to_lldb.generate_lldb_formatter(
            result.types,
            category="test_natvis",
            source_name="sample.natvis",
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "generated.py"
            path.write_text(generated, encoding="utf-8")
            generated_spec = importlib.util.spec_from_file_location(
                "generated_no_raise",
                path,
            )
            generated_module = importlib.util.module_from_spec(generated_spec)
            assert generated_spec.loader is not None
            generated_spec.loader.exec_module(generated_module)

        class FakeType:
            def IsPointerType(self):
                return False

            def IsReferenceType(self):
                return False

        class BrokenValue:
            def IsValid(self):
                return True

            def GetTypeName(self):
                return "demo::SmallVector<int>"

            def GetType(self):
                return FakeType()

            def EvaluateExpression(self, expr):
                raise RuntimeError("simulated lldb expression failure")

            def CreateValueFromExpression(self, name, expr):
                raise RuntimeError("simulated lldb child failure")

        value = BrokenValue()
        summary = generated_module.summary_provider(value, {})
        provider = generated_module.NatvisSyntheticProvider(value, {})

        self.assertIn("simulated lldb expression failure", summary)
        self.assertGreaterEqual(provider.num_children(), 0)
        self.assertIsNone(provider.get_child_at_index(0))


if __name__ == "__main__":
    unittest.main()
