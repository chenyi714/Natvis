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


SUPPORTED_EXPAND_CHILDREN = {"Item", "ArrayItems", "CustomListItems"}
KNOWN_BUT_UNSUPPORTED = {
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
class NatvisCustomListVariable:
    name: str
    initial_value: str


@dataclasses.dataclass
class NatvisCustomListStep:
    kind: str
    expression: str | None = None
    name: str | None = None
    condition: str | None = None
    children: list["NatvisCustomListStep"] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class NatvisCustomListItems:
    condition: str | None
    max_items_per_view: int | None
    size: str | None
    variables: list[NatvisCustomListVariable]
    steps: list[NatvisCustomListStep]


@dataclasses.dataclass
class NatvisType:
    name: str
    regex: str
    condition: str | None
    display_string: str | None
    string_view: str | None
    items: list[NatvisItem]
    array_items: NatvisArrayItems | None
    custom_list_items: list[NatvisCustomListItems]
    source_line: int | None = None

    @property
    def has_summary(self) -> bool:
        return bool(self.display_string or self.string_view)

    @property
    def has_children(self) -> bool:
        return bool(self.items or self.array_items or self.custom_list_items)


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


def _int_attribute(element: ET.Element, name: str, type_name: str, warnings: list[str]) -> int | None:
    value = _attribute(element, name)
    if value is None:
        return None
    try:
        return int(value, 0)
    except ValueError:
        warnings.append(f"{type_name}: Ignoring non-integer {name}={value!r}.")
        return None


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


def _parse_custom_list_steps(
    element: ET.Element,
    type_name: str,
    warnings: list[str],
) -> list[NatvisCustomListStep]:
    steps: list[NatvisCustomListStep] = []
    for child in _children(element):
        tag = _local_name(child.tag)
        condition = _attribute(child, "Condition")

        if tag == "Exec":
            expression = _text(child)
            if expression:
                steps.append(
                    NatvisCustomListStep(
                        kind="Exec",
                        expression=expression,
                        condition=condition,
                    )
                )
            else:
                warnings.append(f"{type_name}: Skipping empty <Exec> in <CustomListItems>.")
        elif tag == "Item":
            expression = _text(child)
            if expression:
                steps.append(
                    NatvisCustomListStep(
                        kind="Item",
                        expression=expression,
                        name=_attribute(child, "Name"),
                        condition=condition,
                    )
                )
            else:
                warnings.append(f"{type_name}: Skipping empty <Item> in <CustomListItems>.")
        elif tag == "Break":
            steps.append(NatvisCustomListStep(kind="Break", condition=condition))
        elif tag == "If":
            if not condition:
                warnings.append(f"{type_name}: <If> without Condition is ignored.")
                continue
            steps.append(
                NatvisCustomListStep(
                    kind="If",
                    condition=condition,
                    children=_parse_custom_list_steps(child, type_name, warnings),
                )
            )
        elif tag == "Loop":
            steps.append(
                NatvisCustomListStep(
                    kind="Loop",
                    condition=condition,
                    children=_parse_custom_list_steps(child, type_name, warnings),
                )
            )
        elif tag in {"Variable", "Size"}:
            warnings.append(f"{type_name}: <{tag}> must be a direct <CustomListItems> child.")
        else:
            warnings.append(f"{type_name}: <{tag}> in <CustomListItems> is ignored.")
    return steps


def _parse_custom_list_items(
    element: ET.Element,
    type_name: str,
    warnings: list[str],
) -> NatvisCustomListItems:
    variables: list[NatvisCustomListVariable] = []
    steps: list[NatvisCustomListStep] = []
    size: str | None = None

    for child in _children(element):
        tag = _local_name(child.tag)
        if tag == "Variable":
            variable_name = _attribute(child, "Name")
            initial_value = _attribute(child, "InitialValue")
            if variable_name and initial_value:
                variables.append(
                    NatvisCustomListVariable(
                        name=variable_name,
                        initial_value=initial_value,
                    )
                )
            else:
                warnings.append(
                    f"{type_name}: <Variable> needs both Name and InitialValue attributes."
                )
        elif tag == "Size":
            size = _text(child)
            if not size:
                warnings.append(f"{type_name}: Skipping empty <Size> in <CustomListItems>.")
        elif tag in {"Exec", "Item", "Break", "If", "Loop"}:
            wrapper = ET.Element("CustomListCode")
            wrapper.append(child)
            steps.extend(_parse_custom_list_steps(wrapper, type_name, warnings))
        else:
            warnings.append(f"{type_name}: <{tag}> in <CustomListItems> is ignored.")

    return NatvisCustomListItems(
        condition=_attribute(element, "Condition"),
        max_items_per_view=_int_attribute(element, "MaxItemsPerView", type_name, warnings),
        size=size,
        variables=variables,
        steps=steps,
    )


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
        custom_list_items: list[NatvisCustomListItems] = []

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
                elif tag == "CustomListItems":
                    custom_list_items.append(_parse_custom_list_items(child, name, warnings))
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
            custom_list_items=custom_list_items,
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
                "custom_list_items": [
                    dataclasses.asdict(custom_list_item)
                    for custom_list_item in item.custom_list_items
                ],
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


def _substitute_context(expr, context):
    if not context or not expr:
        return expr

    def replace(match):
        name = match.group(0)
        if name not in context:
            return name
        start = match.start()
        if start > 0 and expr[start - 1] in (".", ">"):
            return name
        return "({})".format(context[name])

    return re.sub(r"\\b[A-Za-z_]\\w*\\b", replace, expr)


def _eval_with_context(valobj, expr, context):
    return _eval_value(valobj, _substitute_context(expr, context))


def _condition_matches(valobj, condition, context=None):
    if not condition:
        return True
    value, error = _eval_with_context(valobj, condition, context)
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
    try:
        type_name = valobj.GetTypeName()
        cleaned = _clean_type_name(type_name)
        for regex, spec in _COMPILED:
            if regex.match(cleaned) and _condition_matches(valobj, spec.get("condition")):
                return spec
    except Exception:
        return None
    return None


def _expression_context(valobj):
    context = valobj
    for _ in range(4):
        if context is None:
            return valobj
        try:
            if not context.IsValid():
                return valobj
        except Exception:
            return valobj
        try:
            value_type = context.GetType()
            if value_type.IsPointerType() or value_type.IsReferenceType():
                dereferenced = context.Dereference()
                if dereferenced is not None and dereferenced.IsValid():
                    context = dereferenced
                    continue
        except Exception:
            pass
        return context
    return context


def _value_error(value):
    try:
        if value is None or not value.IsValid():
            return "invalid value"
        error = value.GetError()
        if error is not None and error.Fail():
            return str(error)
    except Exception as exc:
        return str(exc)
    return None


def _eval_value(valobj, expr):
    context = _expression_context(valobj)
    candidates = [context]
    if context is not valobj:
        candidates.append(valobj)
    last_error = None
    for candidate in candidates:
        try:
            value = candidate.EvaluateExpression(expr)
        except Exception as exc:
            last_error = str(exc)
            continue
        error = _value_error(value)
        if not error:
            return value, None
        last_error = error
    return None, last_error or "invalid value"


def _create_value_from_expression(valobj, name, expr):
    context = _expression_context(valobj)
    candidates = [context]
    if context is not valobj:
        candidates.append(valobj)
    last_value = None
    for candidate in candidates:
        try:
            value = candidate.CreateValueFromExpression(name, expr)
        except Exception:
            continue
        if not _value_error(value):
            return value
        last_value = value
    for candidate in candidates:
        try:
            value = candidate.CreateValueFromExpression(name, "0")
        except Exception:
            continue
        if value is not None:
            return value
    return last_value


def _value_as_int(value):
    raw = value.GetValue()
    if raw is not None:
        text = raw.strip().lower()
        if text == "true":
            return 1
        if text in ("false", "nullptr", "null"):
            return 0
        return int(text, 0)
    return int(value.GetValueAsSigned())


def _eval_int_expression(valobj, expr):
    value, error = _eval_value(valobj, expr)
    if error:
        return None
    try:
        return _value_as_int(value)
    except Exception:
        return None


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


def _strip_natvis_format(expression):
    if not expression or "," not in expression:
        return expression
    expr, suffix = expression.rsplit(",", 1)
    suffix = suffix.strip()
    if re.match(r"^[A-Za-z][A-Za-z0-9_]{0,8}$", suffix):
        return expr.strip()
    return expression


def _render_display(valobj, template, context=None):
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
        value, error = _eval_with_context(valobj, expr, context)
        out.append("<{}>".format(error) if error else _format_value(value, fmt))
        i = end + 1
    return "".join(out)


def summary_provider(valobj, internal_dict, options=None):
    try:
        spec = _find_spec(valobj)
        if spec is None:
            return ""
        if spec.get("display_string"):
            return _render_display(valobj, spec["display_string"])
        if spec.get("string_view"):
            value, error = _eval_value(valobj, spec["string_view"])
            return "<{}>".format(error) if error else _format_raw_value(value)
    except Exception as exc:
        return "<formatter error: {}>".format(exc)
    return ""


def _safe_expression_child(valobj, name):
    try:
        return valobj.CreateValueFromExpression(name, "0")
    except Exception:
        return None


def _context_arithmetic_result(valobj, expression):
    value = _eval_int_expression(valobj, expression)
    if value is None:
        return "({})".format(expression)
    return str(value)


def _split_top_level_args(text):
    args = []
    start = 0
    depth = 0
    for index, char in enumerate(text):
        if char in "([{<":
            depth += 1
        elif char in ")]}>":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            args.append(text[start:index].strip())
            start = index + 1
    args.append(text[start:].strip())
    return args


def _eval_findnonnull(valobj, args, context):
    if len(args) != 2:
        return None
    base = _substitute_context(args[0], context)
    count_expr = _substitute_context(args[1], context)
    count = _eval_int_expression(valobj, count_expr)
    if count is None:
        return None
    for index in range(max(0, min(count, 100000))):
        value, error = _eval_value(valobj, "({})[{}]".format(base, index))
        if error:
            continue
        try:
            if _value_as_int(value) != 0:
                return str(index)
        except Exception:
            continue
    return "-1"


def _eval_supported_intrinsic(valobj, expression, context):
    expression = expression.strip()
    if expression.startswith("__findnonnull(") and expression.endswith(")"):
        return _eval_findnonnull(
            valobj,
            _split_top_level_args(expression[len("__findnonnull(") : -1]),
            context,
        )
    return None


def _execute_custom_exec(valobj, statement, context):
    statement = (statement or "").strip().rstrip(";").strip()
    if not statement:
        return

    match = re.match(r"^([A-Za-z_]\\w*)\\s*(\\+\\+|--)$", statement)
    if match:
        name, op = match.groups()
        current = context.get(name, name)
        delta = "1" if op == "++" else "-1"
        context[name] = _context_arithmetic_result(valobj, "({}) + ({})".format(current, delta))
        return

    match = re.match(r"^(\\+\\+|--)([A-Za-z_]\\w*)$", statement)
    if match:
        op, name = match.groups()
        current = context.get(name, name)
        delta = "1" if op == "++" else "-1"
        context[name] = _context_arithmetic_result(valobj, "({}) + ({})".format(current, delta))
        return

    match = re.match(r"^([A-Za-z_]\\w*)\\s*(=|\\+=|-=|\\*=|/=|%=)\\s*(.+)$", statement)
    if not match:
        return

    name, op, rhs = match.groups()
    rhs = _substitute_context(rhs, context)
    intrinsic_value = _eval_supported_intrinsic(valobj, rhs, context)
    if op == "=":
        context[name] = intrinsic_value if intrinsic_value is not None else "({})".format(rhs)
        return

    current = context.get(name, name)
    operator = op[:-1]
    context[name] = _context_arithmetic_result(
        valobj,
        "({}) {} ({})".format(current, operator, rhs),
    )


def _execute_custom_steps(valobj, steps, context, out, max_items, budget):
    for step in steps:
        if budget[0] <= 0 or len(out) >= max_items:
            return "stop"
        budget[0] -= 1

        kind = step.get("kind")
        condition = step.get("condition")

        if kind == "Exec":
            if _condition_matches(valobj, condition, context):
                _execute_custom_exec(valobj, step.get("expression"), context)
        elif kind == "Item":
            if not _condition_matches(valobj, condition, context):
                continue
            expression = _substitute_context(
                _strip_natvis_format(step.get("expression") or "0"),
                context,
            )
            raw_name = step.get("name")
            if raw_name:
                name = (
                    _render_display(valobj, raw_name, context)
                    if "{" in raw_name
                    else _substitute_context(raw_name, context)
                )
            else:
                name = "[{}]".format(len(out))
            out.append({"name": name, "expression": expression})
        elif kind == "Break":
            if _condition_matches(valobj, condition, context):
                return "break"
        elif kind == "If":
            if _condition_matches(valobj, condition, context):
                result = _execute_custom_steps(
                    valobj, step.get("children") or [], context, out, max_items, budget
                )
                if result:
                    return result
        elif kind == "Loop":
            while budget[0] > 0 and len(out) < max_items:
                if condition and not _condition_matches(valobj, condition, context):
                    break
                result = _execute_custom_steps(
                    valobj, step.get("children") or [], context, out, max_items, budget
                )
                if result == "break":
                    break
                if result == "stop":
                    return "stop"
    return None


def _build_custom_list_children(valobj, custom_list):
    if not _condition_matches(valobj, custom_list.get("condition")):
        return []

    context = {}
    for variable in custom_list.get("variables") or []:
        name = variable.get("name")
        initial = variable.get("initial_value")
        if name and initial is not None:
            context[name] = "({})".format(_substitute_context(initial, context))

    size = None
    if custom_list.get("size"):
        size = _eval_int_expression(
            valobj,
            _substitute_context(custom_list["size"], context),
        )

    max_items = custom_list.get("max_items_per_view") or size or 1000
    if size is not None:
        max_items = min(max_items, size)
    max_items = max(0, int(max_items))

    budget = [max(1000, max_items * 100)]
    out = []
    _execute_custom_steps(
        valobj,
        custom_list.get("steps") or [],
        context,
        out,
        max_items,
        budget,
    )
    return out


class NatvisSyntheticProvider:
    def __init__(self, valobj, internal_dict):
        self.valobj = valobj
        self.spec = {}
        self.items = []
        self.array_items = None
        self.custom_children = []
        self.array_size = 0
        self.array_pointer = None
        try:
            self.spec = _find_spec(valobj) or {}
            self.array_items = self.spec.get("array_items")
            self.update()
        except Exception:
            pass

    def update(self):
        try:
            self.array_size = 0
            self.array_pointer = None
            self.custom_children = []
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
                self.array_pointer, _ = _eval_value(
                    self.valobj, self.array_items["value_pointer"]
                )
            for custom_list in self.spec.get("custom_list_items") or []:
                self.custom_children.extend(_build_custom_list_children(self.valobj, custom_list))
        except Exception:
            pass
        return False

    def has_children(self):
        try:
            return self.num_children() > 0
        except Exception:
            return False

    def num_children(self):
        try:
            return len(self.items) + self.array_size + len(self.custom_children)
        except Exception:
            return 0

    def get_child_index(self, name):
        try:
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
            custom_offset = len(self.items) + self.array_size
            for index, child in enumerate(self.custom_children):
                if child["name"] == name:
                    return custom_offset + index
        except Exception:
            return -1
        return -1

    def get_child_at_index(self, index):
        try:
            if index < 0 or index >= self.num_children():
                return None

            if index < len(self.items):
                item = self.items[index]
                return self._make_expression_child(item["name"], item["expression"])

            array_index = index - len(self.items)
            if array_index < self.array_size:
                return self._make_array_child(array_index)

            custom_index = array_index - self.array_size
            child = self.custom_children[custom_index]
            return self._make_expression_child(child["name"], child["expression"])
        except Exception:
            return _safe_expression_child(self.valobj, "[{}]".format(index))

    def _make_expression_child(self, name, expression):
        try:
            rendered_name = _render_display(self.valobj, name) if "{" in name else name
            expression = _strip_natvis_format(expression)
            value = _create_value_from_expression(self.valobj, rendered_name, expression)
            return value if value is not None else _safe_expression_child(self.valobj, rendered_name)
        except Exception:
            return _safe_expression_child(self.valobj, name)

    def _make_array_child(self, array_index):
        name = "[{}]".format(array_index)
        try:
            if self.array_pointer is not None and self.array_pointer.IsValid():
                pointer_type = self.array_pointer.GetType()
                if pointer_type.IsPointerType():
                    element_type = pointer_type.GetPointeeType()
                    byte_size = element_type.GetByteSize()
                    return self.array_pointer.CreateChildAtOffset(
                        name, array_index * byte_size, element_type
                    )
        except Exception:
            pass
        try:
            expr = "({})[{}]".format(self.array_items["value_pointer"], array_index)
            return _create_value_from_expression(self.valobj, name, expr)
        except Exception:
            return _safe_expression_child(self.valobj, name)


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
        if spec.get("items") or spec.get("array_items") or spec.get("custom_list_items"):
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
