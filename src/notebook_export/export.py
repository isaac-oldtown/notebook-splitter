# This ensures compatibility with future Python versions for type hinting and annotations
from __future__ import annotations

# Standard libraries
import argparse  # For parsing command-line arguments
import ast       # Abstract Syntax Tree: lets us analyze Python code programmatically
import sys       # Provides access to system-specific parameters and functions
from dataclasses import dataclass, field  # For creating simple classes to store data
from pathlib import Path  # Object-oriented filesystem paths
from typing import Sequence  # Type hint for sequences (like list or tuple)

# Third-party library for working with Jupyter notebooks
import nbformat  # Read/write Jupyter notebooks in Python

# --- Configuration constants ---
NOTEBOOK = Path("notebooks/main.ipynb")  # Default notebook path to export from
DEFAULT_TARGET = "src/main.py"           # Default Python file to export code into
EXPORT_PREFIX = "# EXPORT:"              # Prefix in a cell to define a custom target file

# --- Data structures ---
@dataclass
class ImportStmt:
    """Represents a single import statement and the symbols it defines."""
    code: str             # The actual import code (e.g., 'import os')
    bound_names: set[str] # Names introduced into the namespace by this import

@dataclass
class Module:
    """Represents a Python module/file with its code and defined symbols."""
    path: Path                    # Path to the target Python file
    code_chunks: list[str] = field(default_factory=list)  # Code pieces from notebook cells
    defined: set[str] = field(default_factory=set)        # Names defined in this module

# --- Utility functions ---
def module_name(path: Path) -> str:
    """
    Converts a file path into a Python module path.
    Example: 'src/utils/helpers.py' -> 'utils.helpers'
    """
    parts = path.with_suffix("").parts  # Remove file extension and get path parts
    # If path starts with 'src', omit it
    return ".".join(parts[1:]) if parts and parts[0] == "src" else ".".join(parts)

# --- Notebook analysis ---
def analyze_notebook():
    """
    Reads the notebook, splits code cells, and organizes them into modules.
    Detects imports and defined symbols.
    """
    # Read the notebook file in version 4 format
    nb = nbformat.read(NOTEBOOK, as_version=4)

    modules: dict[str, Module] = {}   # Map target file path -> Module object
    global_imports: list[ImportStmt] = []  # List of import statements across all cells
    seen_imports = set()              # Deduplicate repeated imports

    # Loop over all cells in the notebook
    for cell in nb.cells:
        if cell.cell_type != "code":  # Skip non-code cells (e.g., markdown)
            continue

        lines = cell.source.splitlines()  # Split cell into individual lines
        if not lines:  # Skip empty cells
            continue

        target = DEFAULT_TARGET  # Default export target
        first = lines[0].strip()  # First line of the cell

        # If the cell starts with '# EXPORT: some_file.py', override target
        if first.startswith(EXPORT_PREFIX):
            target = first[len(EXPORT_PREFIX):].strip()
            lines = lines[1:]  # Remove the first line (export directive) from code

        code = "\n".join(lines)  # Recombine lines into a single string
        if not code.strip():  # Skip cells with no actual code
            continue

        # Get or create a Module object for this target
        module = modules.setdefault(target, Module(Path(target)))

        # Parse the code into an Abstract Syntax Tree (AST)
        tree = ast.parse(code)

        # Analyze each top-level statement in the AST
        for node in tree.body:
            segment = ast.get_source_segment(code, node)  # Extract code snippet
            if segment is None:
                continue

            # Handle import statements separately
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = set()
                if isinstance(node, ast.Import):
                    # 'import os' -> adds 'os' to names
                    for alias in node.names:
                        names.add(alias.asname or alias.name.split(".")[0])
                else:
                    # 'from x import y' -> adds 'y' to names, skip '*'
                    for alias in node.names:
                        if alias.name != "*":
                            names.add(alias.asname or alias.name)

                # Add to global imports if not already added
                if segment not in seen_imports:
                    seen_imports.add(segment)
                    global_imports.append(ImportStmt(segment, names))
                continue  # Skip further processing for import nodes

            # Detect defined variable names
            for n in ast.walk(node):
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                    module.defined.add(n.id)

            # Detect defined functions and classes
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                module.defined.add(node.name)

            # Store the actual code segment
            module.code_chunks.append(segment)

    return modules, global_imports  # Return all modules and imports found

# --- Normalize the main file ---
def normalize_main_file(chunks: list[str]):
    """
    Combine code chunks into a single Python script.
    Ensures that top-level code runs inside a 'main()' function.
    """
    whole = "\n".join(chunks)

    if not whole.strip():
        # If no code, create a minimal main.py template
        return 'def main():\n    pass\n\nif __name__ == "__main__":\n    main()\n'

    tree = ast.parse(whole)
    defs = []    # Holds function/class definitions
    runtime = [] # Holds top-level code to run

    for node in tree.body:
        seg = ast.get_source_segment(whole, node)
        if seg is None:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs.append(seg)
        else:
            runtime.append(seg)

    # Check if there's already a '__main__' guard
    has_guard = any("__name__" in s and "__main__" in s for s in runtime)

    parts = []

    if defs:
        parts.append("\n".join(defs))

    if has_guard:
        # If main guard exists, keep runtime code as is
        if runtime:
            parts.append("\n".join(runtime))
        return "\n\n".join(parts)

    # Otherwise, wrap runtime code inside a main() function
    body = "\n".join(runtime)
    indented = (
        "\n".join(("    " + l) if l else "" for l in body.splitlines())
        if body.strip()
        else "    pass"
    )
    parts.append(f"def main():\n{indented}")
    parts.append('if __name__ == "__main__":\n    main()')

    return "\n\n".join(parts)

# --- Resolve cross-module dependencies ---
def resolve_dependencies(modules, global_imports):
    """
    Analyze which imports are needed per module and add them.
    Also adds cross-module imports for symbols defined elsewhere.
    """
    # Map symbol -> the file where it is defined
    symbol_to_file = {
        sym: module.path
        for module in modules.values()
        for sym in module.defined
    }

    # Map symbol -> list of import statements that provide it
    import_by_name = {}
    for imp in global_imports:
        for name in imp.bound_names:
            import_by_name.setdefault(name, []).append(imp.code)

    rendered = {}  # Final rendered code per module

    for module in modules.values():
        # Normalize main.py; leave others as-is
        body = (
            normalize_main_file(module.code_chunks)
            if module.path == Path("src/main.py")
            else "\n".join(module.code_chunks)
        )

        used = set()  # Symbols used in this module

        if body.strip():
            tree = ast.parse(body)
            used = {
                n.id
                for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            }

        # Gather imports needed for used symbols
        external_imports = [
            stmt
            for name in sorted(used)
            for stmt in import_by_name.get(name, [])
        ]

        imported_names = {n for n in used if n in import_by_name}

        # Determine imports from other modules in our project
        cross_imports = []
        for name in sorted(used):
            owner = symbol_to_file.get(name)
            if not owner or owner == module.path or name in imported_names:
                continue
            cross_imports.append(f"from {module_name(owner)} import {name}")

        # Combine and deduplicate imports
        imports = sorted(dict.fromkeys(external_imports + cross_imports))

        parts = []
        if imports:
            parts.append("\n".join(imports))
        if body:
            parts.append(body)

        rendered[module.path] = "\n\n".join(parts)

    return rendered

# --- Write rendered modules to disk ---
def write_files(rendered):
    """
    Save each module's content to its respective file path.
    Creates parent directories if they don't exist.
    """
    # Ensure __init__.py exists in lib folder
    rendered.setdefault(Path("src/lib/__init__.py"), "")

    for path, content in rendered.items():
        path.parent.mkdir(parents=True, exist_ok=True)  # Create folders if needed
        path.write_text(content, encoding="utf-8")      # Write code to file

# --- Main export runner ---
def run_export():
    """Perform the notebook export process."""
    if not NOTEBOOK.exists():
        print(f"Notebook not found: {NOTEBOOK}", file=sys.stderr)
        return 1

    modules, imports = analyze_notebook()
    if not modules:
        print(f"No code cells found in {NOTEBOOK}", file=sys.stderr)
        return 1

    rendered = resolve_dependencies(modules, imports)
    write_files(rendered)

    # Print status for each file written
    for path in sorted(rendered):
        print(f"wrote {path}")

    return 0

# --- Command-line argument parser ---
def parse_args(argv: Sequence[str]):
    """
    Parse command-line arguments for pre-commit hook or manual run.
    --all-files: export regardless of changed files
    filenames: list of files changed in git (for pre-commit)
    """
    parser = argparse.ArgumentParser(
        description="Export code cells from notebooks/main.ipynb into src/."
    )
    parser.add_argument("--all-files", action="store_true")
    parser.add_argument("filenames", nargs="*")
    return parser.parse_args(argv)

# --- Main entrypoint ---
def main(argv: Sequence[str] | None = None):
    """
    Determine whether to export the notebook based on CLI args.
    Used by pre-commit to skip notebooks that weren't changed.
    """
    args = parse_args(argv if argv is not None else sys.argv[1:])

    # If not exporting all files and notebook wasn't modified, skip
    if not args.all_files and NOTEBOOK.name not in {Path(f).name for f in args.filenames}:
        print("Skipping export.")
        return 0

    return run_export()

# Only run if this script is executed directly
if __name__ == "__main__":
    raise SystemExit(main())