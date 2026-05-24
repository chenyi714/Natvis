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

Generate a pure `lldb_formatters.txt` command file using `type summary add`:

```sh
python3 tools/natvis_to_lldb_txt.py path/to/project.natvis \
  -o tools/debugvis/lldb_formatters.txt
```

This summary-only mode does not load Python and does not generate synthetic
children. It converts simple `DisplayString` / `StringView` rules to LLDB
summary strings.

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
