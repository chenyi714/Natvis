#!/usr/bin/env python3
"""Generate CodeLLDB/LLDB Python formatters from a practical Natvis subset.

This is intentionally conservative. Natvis is a debugger-specific language, so
the generated formatter should be treated as a strong first draft for LLDB, not
as a byte-for-byte replacement for Visual Studio.
"""

from __future__ import annotations

import argparse
import dataclasses
import pprint
import re
import sys
import textwrap
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable


SUPPORTED_EXPAND_CHILDREN = {"Item", "ArrayItems"}
KNOWN_BUT_UNSUPPORTED = {
    "CustomListItems",
    "IndexListItems",
    "LinkedListItems",
    "TreeItems",
    "Synthetic",
    "ExpandedItem",
}


@dataclasses.dataclass
class NatvisItem:
    name: str
    expression: str
    condition: str | None = None


@dataclasses.dataclass
class NatvisArrayItems:
    size: str
    value_pointer: str
    condition: str | None = None


@dataclasses.dataclass
class NatvisType:
    name: str
    regex: str
    condition: str | None
    display_string: str | None
    string_view: str | None
    items: list[NatvisItem]
    array_items: NatvisArrayItems | None
    source_line: int | None = None

    @property
    def has_summary(self) -> bool:
        return bool(self.display_string or self.string_view)

    @property
    def has_children(self) -> bool:
        return bool(self.items or self.array_items)


@dataclasses.dataclass
class NatvisAlternativeType:
    name: str
    condition: str | None = None


@dataclasses.dataclass
class ParseResult:
    types: list[NatvisType]
    warnings: list[str]


def _local_name(tag: object) -> str | None:
    if not isinstance(tag, str):
        return None
    return tag.rsplit("}", 1)[-1]


def _text(element: ET.Element | None) -> str | None:
    if element is None or element.text is None:
        return None
    value = element.text.strip()
    return value or None


def _children(element: ET.Element, local_name: str | None = None) -> Iterable[ET.Element]:
    for child in list(element):
        child_name = _local_name(child.tag)
        if child_name is None:
            continue
        if local_name is None or child_name == local_name:
            yield child


def _first_child(element: ET.Element, local_name: str) -> ET.Element | None:
    return next(_children(element, local_name), None)


def _first_child_text(element: ET.Element, local_name: str) -> str | None:
    return _text(_first_child(element, local_name))


def _attribute(element: ET.Element, name: str) -> str | None:
    value = element.attrib.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def natvis_type_to_regex(type_name: str) -> str:
    """Convert Natvis' simple '*' wildcard type pattern to an LLDB regex."""

    pieces: list[str] = ["^"]
    for char in type_name.strip():
        if char == "*":
            pieces.append(".*")
        elif char.isspace():
            pieces.append(r"\s*")
        else:
            pieces.append(re.escape(char))
    pieces.append("$")
    return "".join(pieces)


def parse_natvis(path: Path) -> ParseResult:
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    tree = ET.parse(path, parser=parser)
    root = tree.getroot()

    warnings: list[str] = []
    result: list[NatvisType] = []

    for type_elem in _children(root, "Type"):
        name = type_elem.attrib.get("Name", "").strip()
        if not name:
            warnings.append("Skipping <Type> without a Name attribute.")
            continue

        type_condition = _attribute(type_elem, "Condition")

        display_string = _first_child_text(type_elem, "DisplayString")
        string_view = _first_child_text(type_elem, "StringView")
        items: list[NatvisItem] = []
        alternative_types: list[NatvisAlternativeType] = []
        array_items: NatvisArrayItems | None = None

        expand = _first_child(type_elem, "Expand")
        if expand is not None:
            for child in _children(expand):
                tag = _local_name(child.tag)
                condition = _attribute(child, "Condition")

                if tag == "Item":
                    expr = _text(child)
                    if not expr:
                        warnings.append(f"{name}: Skipping empty <Item>.")
                        continue
                    item_name = child.attrib.get("Name", "").strip() or expr
                    items.append(NatvisItem(name=item_name, expression=expr, condition=condition))
                elif tag == "ArrayItems":
                    size = _first_child_text(child, "Size")
                    value_pointer = _first_child_text(child, "ValuePointer")
                    if size and value_pointer:
                        array_items = NatvisArrayItems(
                            size=size,
                            value_pointer=value_pointer,
                            condition=condition,
                        )
                    else:
                        warnings.append(
                            f"{name}: <ArrayItems> needs both <Size> and <ValuePointer>."
                        )
                elif tag in KNOWN_BUT_UNSUPPORTED:
                    warnings.append(f"{name}: <{tag}> is not generated yet.")
                elif tag not in SUPPORTED_EXPAND_CHILDREN:
                    warnings.append(f"{name}: Unknown <Expand> child <{tag}> is ignored.")

        for child in _children(type_elem):
            tag = _local_name(child.tag)
            if tag in {"DisplayString", "StringView", "Expand"}:
                continue
            if tag == "AlternativeType":
                alternative_name = _attribute(child, "Name")
                if alternative_name:
                    alternative_types.append(
                        NatvisAlternativeType(
                            name=alternative_name,
                            condition=_attribute(child, "Condition") or type_condition,
                        )
                    )
                else:
                    warnings.append(f"{name}: Skipping <AlternativeType> without a Name attribute.")
            elif tag in {"Intrinsic", "UIVisualizer"}:
                warnings.append(f"{name}: top-level <{tag}> is not generated yet.")
            elif tag not in {"Version"}:
                warnings.append(f"{name}: top-level <{tag}> is ignored.")

        natvis_type = NatvisType(
            name=name,
            regex=natvis_type_to_regex(name),
            condition=type_condition,
            display_string=display_string,
            string_view=string_view,
            items=items,
            array_items=array_items,
        )

        if not natvis_type.has_summary and not natvis_type.has_children:
            warnings.append(f"{name}: no supported visualization nodes found.")
            continue
        result.append(natvis_type)
        for alternative_type in alternative_types:
            result.append(
                dataclasses.replace(
                    natvis_type,
                    name=alternative_type.name,
                    regex=natvis_type_to_regex(alternative_type.name),
                    condition=alternative_type.condition,
                )
            )

    return ParseResult(types=result, warnings=warnings)


def _python_specs(types: list[NatvisType]) -> str:
    data = []
    for item in types:
        data.append(
            {
                "name": item.name,
                "regex": item.regex,
                "condition": item.condition,
                "display_string": item.display_string,
                "string_view": item.string_view,
                "items": [dataclasses.asdict(child) for child in item.items],
                "array_items": (
                    dataclasses.asdict(item.array_items) if item.array_items is not None else None
                ),
            }
        )
    return pprint.pformat(data, width=100, sort_dicts=False)


def generate_lldb_formatter(types: list[NatvisType], *, category: str, source_name: str) -> str:
    specs = _python_specs(types)
    generated = """\
# Generated by natvis_to_lldb.py from __SOURCE_NAME__.
# Load in CodeLLDB with:
#   "initCommands": ["command script import ${workspaceFolder}/path/to/this_file.py"]

from __future__ import annotations

import re

try:
    import lldb
except Exception:
    lldb = None


CATEGORY = __CATEGORY__
FORMATTERS = __FORMATTERS__
_COMPILED = [(re.compile(spec["regex"]), spec) for spec in FORMATTERS]


def _clean_type_name(type_name):
    if not type_name:
        return ""
    cleaned = type_name.strip()
    if cleaned.startswith("const "):
        cleaned = cleaned[6:].strip()
    for suffix in (" &", " *", "&", "*"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)].strip()
    return cleaned


def _condition_matches(valobj, condition):
    if not condition:
        return True
    value, error = _eval_value(valobj, condition)
    if error:
        return False

    raw = value.GetValue()
    if raw is not None:
        text = raw.strip().lower()
        if text in ("true", "false"):
            return text == "true"
        try:
            return int(text, 0) != 0
        except ValueError:
            pass

    summary = value.GetSummary()
    if summary is not None:
        text = summary.strip('"').strip().lower()
        if text in ("true", "false"):
            return text == "true"

    try:
        return value.GetValueAsUnsigned() != 0
    except Exception:
        return False


def _find_spec(valobj):
    type_name = valobj.GetTypeName()
    cleaned = _clean_type_name(type_name)
    for regex, spec in _COMPILED:
        if regex.match(cleaned) and _condition_matches(valobj, spec.get("condition")):
            return spec
    return None


def _value_error(value):
    if value is None or not value.IsValid():
        return "invalid value"
    error = value.GetError()
    if error is not None and error.Fail():
        return str(error)
    return None


def _eval_value(valobj, expr):
    try:
        value = valobj.EvaluateExpression(expr)
    except Exception as exc:
        return None, str(exc)
    error = _value_error(value)
    if error:
        return None, error
    return value, None


def _format_raw_value(value):
    if value is None:
        return "<invalid>"
    summary = value.GetSummary()
    if summary is not None:
        return summary
    value_text = value.GetValue()
    if value_text is not None:
        return value_text
    return str(value)


def _format_value(value, fmt):
    if value is None:
        return "<invalid>"
    fmt = (fmt or "").strip().lower()
    try:
        if fmt.startswith("x"):
            return hex(value.GetValueAsUnsigned())
        if fmt.startswith("d"):
            raw = value.GetValue()
            return str(int(raw, 0)) if raw else str(value.GetValueAsSigned())
        if fmt.startswith("u"):
            return str(value.GetValueAsUnsigned())
        if fmt.startswith("b"):
            return bin(value.GetValueAsUnsigned())
        if fmt.startswith("o"):
            return oct(value.GetValueAsUnsigned())
        if fmt.startswith("s"):
            summary = value.GetSummary()
            if summary is not None:
                return summary.strip('"')
    except Exception:
        pass
    return _format_raw_value(value)


def _split_display_token(token):
    # Natvis allows simple format suffixes such as {expr,x}. This deliberately
    # ignores richer Visual Studio-only specifiers.
    if "," not in token:
        return token.strip(), ""
    expr, fmt = token.rsplit(",", 1)
    return expr.strip(), fmt.strip()


def _render_display(valobj, template):
    out = []
    i = 0
    while i < len(template):
        if template.startswith("{{", i):
            out.append("{")
            i += 2
            continue
        if template.startswith("}}", i):
            out.append("}")
            i += 2
            continue
        if template[i] != "{":
            out.append(template[i])
            i += 1
            continue
        end = template.find("}", i + 1)
        if end == -1:
            out.append(template[i:])
            break
        expr, fmt = _split_display_token(template[i + 1 : end])
        value, error = _eval_value(valobj, expr)
        out.append("<{}>".format(error) if error else _format_value(value, fmt))
        i = end + 1
    return "".join(out)


def summary_provider(valobj, internal_dict, options=None):
    spec = _find_spec(valobj)
    if spec is None:
        return ""
    if spec.get("display_string"):
        return _render_display(valobj, spec["display_string"])
    if spec.get("string_view"):
        value, error = _eval_value(valobj, spec["string_view"])
        return "<{}>".format(error) if error else _format_raw_value(value)
    return ""


class NatvisSyntheticProvider:
    def __init__(self, valobj, internal_dict):
        self.valobj = valobj
        self.spec = _find_spec(valobj) or {}
        self.items = []
        self.array_items = self.spec.get("array_items")
        self.array_size = 0
        self.array_pointer = None
        self.update()

    def update(self):
        self.array_size = 0
        self.array_pointer = None
        self.items = [
            item for item in self.spec.get("items") or []
            if _condition_matches(self.valobj, item.get("condition"))
        ]
        if self.array_items and _condition_matches(
            self.valobj, self.array_items.get("condition")
        ):
            size_value, _ = _eval_value(self.valobj, self.array_items["size"])
            if size_value is not None:
                self.array_size = int(size_value.GetValueAsUnsigned())
            self.array_pointer, _ = _eval_value(self.valobj, self.array_items["value_pointer"])
        return False

    def has_children(self):
        return self.num_children() > 0

    def num_children(self):
        return len(self.items) + self.array_size

    def get_child_index(self, name):
        for index, item in enumerate(self.items):
            if item["name"] == name:
                return index
        if name.startswith("[") and name.endswith("]"):
            try:
                array_index = int(name[1:-1])
            except ValueError:
                return -1
            if 0 <= array_index < self.array_size:
                return len(self.items) + array_index
        return -1

    def get_child_at_index(self, index):
        if index < 0 or index >= self.num_children():
            return None

        if index < len(self.items):
            item = self.items[index]
            return self._make_expression_child(item["name"], item["expression"])

        array_index = index - len(self.items)
        return self._make_array_child(array_index)

    def _make_expression_child(self, name, expression):
        rendered_name = _render_display(self.valobj, name) if "{" in name else name
        try:
            return self.valobj.CreateValueFromExpression(rendered_name, expression)
        except Exception:
            return self.valobj.CreateValueFromExpression(rendered_name, "0")

    def _make_array_child(self, array_index):
        name = "[{}]".format(array_index)
        if self.array_pointer is not None and self.array_pointer.IsValid():
            try:
                pointer_type = self.array_pointer.GetType()
                if pointer_type.IsPointerType():
                    element_type = pointer_type.GetPointeeType()
                    byte_size = element_type.GetByteSize()
                    return self.array_pointer.CreateChildAtOffset(
                        name, array_index * byte_size, element_type
                    )
            except Exception:
                pass
        expr = "({})[{}]".format(self.array_items["value_pointer"], array_index)
        return self.valobj.CreateValueFromExpression(name, expr)


def _quote_lldb(text):
    return '"' + text.replace("\\\\", "\\\\\\\\").replace('"', '\\\\"') + '"'


def __lldb_init_module(debugger, internal_dict):
    debugger.HandleCommand("type category define " + _quote_lldb(CATEGORY))
    for spec in FORMATTERS:
        regex = _quote_lldb(spec["regex"])
        if spec.get("display_string") or spec.get("string_view"):
            debugger.HandleCommand(
                "type summary add -w {} -x {} --python-function {}".format(
                    _quote_lldb(CATEGORY),
                    regex,
                    _quote_lldb(__name__ + ".summary_provider"),
                )
            )
        if spec.get("items") or spec.get("array_items"):
            debugger.HandleCommand(
                "type synthetic add -w {} -x {} --python-class {}".format(
                    _quote_lldb(CATEGORY),
                    regex,
                    _quote_lldb(__name__ + ".NatvisSyntheticProvider"),
                )
            )
    debugger.HandleCommand("type category enable " + _quote_lldb(CATEGORY))
    print("Loaded {} Natvis-derived LLDB formatter(s) into category '{}'.".format(
        len(FORMATTERS), CATEGORY
    ))
"""
    return textwrap.dedent(generated).replace(
        "__SOURCE_NAME__", source_name
    ).replace(
        "__CATEGORY__", repr(category)
    ).replace(
        "__FORMATTERS__", specs
    )


def write_formatter(input_path: Path, output_path: Path, category: str) -> ParseResult:
    result = parse_natvis(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        generate_lldb_formatter(result.types, category=category, source_name=input_path.name),
        encoding="utf-8",
    )
    return result


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate CodeLLDB/LLDB Python formatters from a Natvis file."
    )
    parser.add_argument("natvis", type=Path, help="Input .natvis file.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("lldb_natvis_formatters.py"),
        help="Generated Python formatter path.",
    )
    parser.add_argument(
        "--category",
        default="natvis_generated",
        help="LLDB formatter category name.",
    )
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Return a non-zero exit code if unsupported Natvis nodes are found.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    result = write_formatter(args.natvis, args.output, args.category)

    print(f"Generated {args.output} with {len(result.types)} formatter(s).")
    if result.warnings:
        print("Warnings:", file=sys.stderr)
        for warning in result.warnings:
            print(f"  - {warning}", file=sys.stderr)
        if args.fail_on_warning:
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
