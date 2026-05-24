# Natvis to CodeLLDB Formatter Toolkit

This repository contains a small converter that turns the practical subset of a
Visual Studio `.natvis` file into an LLDB Python formatter module for CodeLLDB.

It is meant to get Linux debugging close to the hand-written Visual Studio view
quickly, then leave the truly complex types as explicit follow-up work.

## Supported Natvis Subset

- `Type Name="..."`
- `DisplayString`
- `StringView`
- `Expand` / `Item`
- `Expand` / `ArrayItems` with `Size` and `ValuePointer`
- `Expand` / `CustomListItems` with `Variable`, `Size`, `Loop`, `If`,
  `Break`, `Exec`, and `Item`
- `Condition` on `Type`, `Item`, `ArrayItems`, and CustomListItems steps
- `AlternativeType`
- Common C-style pointer-cast member paths in the generated Python formatter,
  such as `((Impl*)m_impl._Mypair._Myval2)->m_id` and
  `*(((Impl*)ptr)->field)`
- MSVC smart-pointer storage paths such as `m_impl._Mypair._Myval2` are mapped
  to common LLDB-visible `std::unique_ptr` / `std::shared_ptr` payload layouts
  or `.get()` where possible.

The converter warns on advanced nodes such as `IndexListItems`,
`LinkedListItems`, and `TreeItems`. Those usually need a
hand-written LLDB synthetic provider because their traversal logic is too
specific to translate safely.

## Usage

Generate an LLDB formatter module:

```sh
python3 tools/natvis_to_lldb.py path/to/project.natvis \
  -o tools/debugvis/project_lldb_formatters.py
```

By default, the generated Python formatter avoids `SBValue.EvaluateExpression`
while variables render. This is safer for CodeLLDB because expression
evaluation from a formatter can freeze stepping or continuing when the Variables
view refreshes. If you explicitly want that fallback for expressions the path
parser cannot resolve, add `--enable-expression-eval`.

If the full synthetic provider makes a large project unstable, generate a
summary-only Python module:

```sh
python3 tools/natvis_to_lldb.py path/to/project.natvis \
  -o tools/debugvis/project_lldb_formatters.py \
  --no-synthetic
```

If CodeLLDB freezes as soon as a breakpoint is hit, generate a diagnostics-only
module. It registers `natvis-debug` / `natvis-debug-file` but no summaries or
synthetic children:

```sh
python3 tools/natvis_to_lldb.py path/to/project.natvis \
  -o tools/debugvis/project_lldb_formatters.py \
  --commands-only
```

Generate a pure `lldb_formatters.txt` command file using `type summary add`:

```sh
python3 tools/natvis_to_lldb_txt.py path/to/project.natvis \
  -o tools/debugvis/lldb_formatters.txt
```

This summary-only mode does not load Python and does not generate synthetic
children. It converts simple `DisplayString` / `StringView` rules to LLDB
summary strings. It supports simple member paths such as `size_`, nested paths
such as `node_->id`, and pointer dereference paths such as `*count_ptr_`.
C-style casts such as `((Impl*)ptr)->field` are not supported by default because
LLDB summary strings do not execute arbitrary C++ casts. You can try
`--assume-c-style-casts` when the uncast path's static debug type already exposes
the requested field, but it may produce `summary string parsing error` otherwise.

Load it from CodeLLDB:

```jsonc
{
  "name": "Debug with CodeLLDB formatters",
  "type": "lldb",
  "request": "launch",
  "program": "${workspaceFolder}/build/your_program",
  "cwd": "${workspaceFolder}",
  "initCommands": [
    "command script import ${workspaceFolder}/tools/debugvis/project_lldb_formatters.py"
  ]
}
```

Or load the generated command file:

```jsonc
{
  "initCommands": [
    "command source ${workspaceFolder}/tools/debugvis/lldb_formatters.txt"
  ]
}
```

## Debugging a Formatter

The generated Python module registers a helper command:

```lldb
natvis-debug body
```

Run it while stopped at a breakpoint, replacing `body` with the variable or
expression you are inspecting. The output shows the matched Natvis type, the raw
LLDB children, and whether each `DisplayString`, `Item`, `Condition`, and
`CustomListItems` expression was resolved through the safer SBValue path parser
or had to fall back to LLDB expression evaluation.

If your debug console does not show command output, write the same diagnostics
to a file instead:

```lldb
natvis-debug-file body
```

By default this writes `/tmp/natvis_debug.txt`. You can choose another path with:

```lldb
natvis-debug-file body /tmp/body_natvis_debug.txt
```

## Example

```sh
python3 tools/natvis_to_lldb.py examples/sample.natvis \
  -o /tmp/sample_lldb_formatters.py
```

## Tests

```sh
python3 -m unittest discover -s tests
```

## Notes

The generated module uses `SBValue.EvaluateExpression` for Natvis expressions,
so debug builds should keep enough debug information for LLDB to see the fields
referenced by the Natvis file. For Clang-heavy builds, `-g3 -O0` or `-Og -g3`
is a good starting point; `-fstandalone-debug` can help when template/debug type
information is too thin.
