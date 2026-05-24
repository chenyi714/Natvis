#!/usr/bin/env python3
"""Generate a pure LLDB type-summary command file from a Natvis file.

This deliberately avoids Python data formatter providers. The generated
lldb_formatters.txt only contains LLDB commands such as:

    type summary add -x "..." --summary-string "..."

That makes it much less likely to break variable display in CodeLLDB, but it
also means only DisplayString/StringView summaries are generated.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from natvis_to_lldb import NatvisType, parse_natvis


SUPPORTED_FORMATS = {
    "d": "d",
    "i": "d",
    "u": "u",
    "x": "x",
    "xb": "x",
    "b": "b",
    "o": "o",
    "s": "s",
    "sb": "s",
}


def _quote_lldb(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _escape_summary_literal(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("$", "\\$")
        .replace("{", "\\{")
        .replace("}", "\\}")
    )


def _lldb_regex(regex: str) -> str:
    return regex.replace(r"\s*", r"[[:space:]]*")


def _split_natvis_token(token: str) -> tuple[str, str | None]:
    if "," not in token:
        return token.strip(), None
    expression, fmt = token.rsplit(",", 1)
    return expression.strip(), fmt.strip() or None


def _strip_this(expression: str) -> str:
    if expression.startswith("this->"):
        return expression[6:].strip()
    if expression.startswith("this."):
        return expression[5:].strip()
    return expression


def _unwrap_parenthesized(expression: str) -> str:
    expression = expression.strip()
    while expression.startswith("(") and expression.endswith(")"):
        depth = 0
        wraps = True
        for index, char in enumerate(expression):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(expression) - 1:
                    wraps = False
                    break
        if not wraps:
            break
        expression = expression[1:-1].strip()
    return expression


def _normalize_child_path(expression: str) -> str | None:
    expression = expression.strip()
    dereference = False

    if expression.startswith("*"):
        dereference = True
        expression = _unwrap_parenthesized(expression[1:].strip())

    match = re.match(r"^\(\s*\*\s*([A-Za-z_]\w*)\s*\)\s*\.(.+)$", expression)
    if match:
        expression = match.group(1) + "->" + match.group(2).strip()

    expression = _strip_this(expression)

    if not expression:
        return None

    if any(char in expression for char in '()+*/%&|^!=?:\\'):
        return None

    segment = r"[A-Za-z_]\w*(?:\[[0-9]+\])*"
    path_pattern = rf"^{segment}(?:(?:\.|->){segment})*$"
    if not re.match(path_pattern, expression):
        return None

    return ("*" if dereference else "") + "var." + expression


def _format_suffix(fmt: str | None, warnings: list[str], type_name: str, expression: str) -> str:
    if fmt is None:
        return ""
    key = fmt.strip().lower()
    if key in SUPPORTED_FORMATS:
        return "%" + SUPPORTED_FORMATS[key]

    # Natvis uses many Visual Studio-only modifiers such as "na". Dropping them
    # is usually better than generating an invalid LLDB summary string.
    if key not in {"na", "nd", "hr", "wm", "su"}:
        warnings.append(f"{type_name}: DisplayString format {fmt!r} on {expression!r} is ignored.")
    return ""


def _convert_expression_token(token: str, type_name: str, warnings: list[str]) -> str:
    expression, fmt = _split_natvis_token(token)
    child_path = _normalize_child_path(expression)
    if child_path is None:
        warnings.append(
            f"{type_name}: DisplayString expression {expression!r} cannot be represented "
            "as an LLDB summary string."
        )
        return "<unsupported:{}>".format(_escape_summary_literal(expression))
    return "${" + child_path + _format_suffix(fmt, warnings, type_name, expression) + "}"


def natvis_display_to_lldb_summary(
    display_string: str,
    type_name: str,
    warnings: list[str],
) -> str:
    output: list[str] = []
    i = 0
    while i < len(display_string):
        if display_string.startswith("{{", i):
            output.append(_escape_summary_literal("{"))
            i += 2
            continue
        if display_string.startswith("}}", i):
            output.append(_escape_summary_literal("}"))
            i += 2
            continue
        if display_string[i] != "{":
            start = i
            while i < len(display_string) and display_string[i] != "{":
                i += 1
            output.append(_escape_summary_literal(display_string[start:i]))
            continue

        end = display_string.find("}", i + 1)
        if end == -1:
            output.append(_escape_summary_literal(display_string[i:]))
            break
        output.append(_convert_expression_token(display_string[i + 1 : end], type_name, warnings))
        i = end + 1

    return "".join(output)


def _summary_for_type(natvis_type: NatvisType, warnings: list[str]) -> str | None:
    if natvis_type.condition:
        warnings.append(f"{natvis_type.name}: Type Condition is ignored in type-summary mode.")

    if natvis_type.display_string:
        return natvis_display_to_lldb_summary(
            natvis_type.display_string,
            natvis_type.name,
            warnings,
        )

    if natvis_type.string_view:
        child_path = _normalize_child_path(natvis_type.string_view)
        if child_path is None:
            warnings.append(
                f"{natvis_type.name}: StringView expression {natvis_type.string_view!r} "
                "cannot be represented as an LLDB summary string."
            )
            return None
        return "${" + child_path + "}"

    return None


def generate_lldb_summary_commands(
    types: list[NatvisType],
    *,
    category: str,
    pointer_depth: int,
) -> tuple[str, int, list[str]]:
    warnings: list[str] = []
    lines = [
        "# Generated by natvis_to_lldb_txt.py.",
        "# Pure LLDB type-summary mode: summaries only, no Python providers.",
        "type category define " + _quote_lldb(category),
    ]

    count = 0
    for natvis_type in types:
        summary = _summary_for_type(natvis_type, warnings)
        if summary is None:
            warnings.append(f"{natvis_type.name}: no DisplayString/StringView summary generated.")
            continue

        lines.append(
            "type summary add -w {category} -x {regex} -d {depth} --summary-string {summary}".format(
                category=_quote_lldb(category),
                regex=_quote_lldb(_lldb_regex(natvis_type.regex)),
                depth=pointer_depth,
                summary=_quote_lldb(summary),
            )
        )
        count += 1

    lines.append("type category enable " + _quote_lldb(category))
    lines.append("")
    return "\n".join(lines), count, warnings


def write_command_file(
    natvis: Path,
    output: Path,
    *,
    category: str,
    pointer_depth: int,
) -> tuple[int, list[str]]:
    result = parse_natvis(natvis)
    commands, summary_count, summary_warnings = generate_lldb_summary_commands(
        result.types,
        category=category,
        pointer_depth=pointer_depth,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(commands, encoding="utf-8")
    return summary_count, result.warnings + summary_warnings


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a pure lldb_formatters.txt type-summary command file from Natvis."
    )
    parser.add_argument("natvis", type=Path, help="Input .natvis file.")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("lldb_formatters.txt"),
        help="Generated LLDB command file path.",
    )
    parser.add_argument(
        "--category",
        default="natvis_summary",
        help="LLDB formatter category name.",
    )
    parser.add_argument(
        "--pointer-depth",
        type=int,
        default=1,
        help="Pointer indirection depth for type summary matching.",
    )
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Return a non-zero exit code if warnings are found.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    summary_count, warnings = write_command_file(
        args.natvis,
        args.output,
        category=args.category,
        pointer_depth=args.pointer_depth,
    )

    print(f"Generated {args.output} with {summary_count} type summary command(s).")
    if warnings:
        print("Warnings:", file=sys.stderr)
        for warning in warnings:
            print(f"  - {warning}", file=sys.stderr)
        if args.fail_on_warning:
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
