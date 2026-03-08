from __future__ import annotations

import argparse
import ast
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

import nbformat


NOTEBOOK = Path("notebooks/main.ipynb")
SRC_DIR = Path("src")
BACKUP_DIR = Path("_backup_src")
DEFAULT_TARGET = "src/main.py"
EXPORT_PREFIX = "# EXPORT:"


@dataclass
class ImportStmt:
    code: str
    bound_names: set[str]


@dataclass
class Block:
    target: str
    code: str
    cell_index: int


@dataclass
class TargetFile:
    path: Path
    raw_blocks: list[str] = field(default_factory=list)
    other_code_chunks: list[str] = field(default_factory=list)
    defined_symbols: set[str] = field(default_factory=set)


def backup_and_reset_src() -> None:
    if BACKUP_DIR.exists():
        shutil.rmtree(BACKUP_DIR)

    if SRC_DIR.exists():
        shutil.move(str(SRC_DIR), str(BACKUP_DIR))

    SRC_DIR.mkdir(parents=True, exist_ok=True)


def notebook_to_blocks(nb_path: Path) -> list[Block]:
    nb = nbformat.read(nb_path, as_version=4)
    blocks: list[Block] = []

    for i, cell in enumerate(nb.cells):
        if cell.cell_type != "code":
            continue

        lines = cell.source.splitlines()
        if not lines:
            continue

        first = lines[0].strip()
        target = DEFAULT_TARGET
        body_lines = lines

        if first.startswith(EXPORT_PREFIX):
            target = first[len(EXPORT_PREFIX):].strip()
            body_lines = lines[1:]

        body = "\n".join(body_lines).strip()
        if body:
            blocks.append(Block(target=target, code=body, cell_index=i))

    return blocks


def get_import_bound_names(node: ast.AST) -> set[str]:
    names: set[str] = set()

    if isinstance(node, ast.Import):
        for alias in node.names:
            names.add(alias.asname or alias.name.split(".")[0])

    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            if alias.name == "*":
                continue
            names.add(alias.asname or alias.name)

    return names


def extract_assigned_names(node: ast.AST) -> set[str]:
    out: set[str] = set()

    if isinstance(node, ast.Name):
        out.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            out.update(extract_assigned_names(elt))

    return out


def parse_code_chunk(code: str) -> tuple[list[ImportStmt], list[str], set[str]]:
    tree = ast.parse(code)
    imports: list[ImportStmt] = []
    code_chunks: list[str] = []
    defined_symbols: set[str] = set()

    for node in tree.body:
        segment = ast.get_source_segment(code, node)
        if not segment:
            continue

        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports.append(
                ImportStmt(code=segment, bound_names=get_import_bound_names(node))
            )
            continue

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined_symbols.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                defined_symbols.update(extract_assigned_names(target))
        elif isinstance(node, ast.AnnAssign):
            defined_symbols.update(extract_assigned_names(node.target))

        code_chunks.append(segment)

    return imports, code_chunks, defined_symbols


def collect_used_names(code: str) -> set[str]:
    tree = ast.parse(code)
    used: set[str] = set()

    class Visitor(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Load):
                used.add(node.id)
            self.generic_visit(node)

    Visitor().visit(tree)
    return used


def dedupe_imports(imports: Iterable[ImportStmt]) -> list[ImportStmt]:
    seen: set[str] = set()
    out: list[ImportStmt] = []

    for imp in imports:
        if imp.code not in seen:
            seen.add(imp.code)
            out.append(imp)

    return out


def module_name_from_path(path: Path) -> str:
    parts = list(path.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts)


def normalize_main_file(code_chunks: list[str]) -> str:
    whole = "\n\n".join(chunk.strip() for chunk in code_chunks if chunk.strip()).strip()
    if not whole:
        return 'def main():\n    pass\n\nif __name__ == "__main__":\n    main()\n'

    tree = ast.parse(whole)
    defs_and_classes: list[str] = []
    runtime_stmts: list[str] = []

    for node in tree.body:
        seg = ast.get_source_segment(whole, node)
        if not seg:
            continue

        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs_and_classes.append(seg)
        else:
            runtime_stmts.append(seg)

    has_existing_guard = any(
        "__name__" in stmt and "__main__" in stmt for stmt in runtime_stmts
    )

    pieces: list[str] = []
    if defs_and_classes:
        pieces.append("\n\n".join(defs_and_classes).strip())

    if has_existing_guard:
        if runtime_stmts:
            pieces.append("\n\n".join(runtime_stmts).strip())
        return "\n\n\n".join(pieces).strip() + "\n"

    body = "\n\n".join(runtime_stmts).strip()
    indented = (
        "\n".join(("    " + line) if line else "" for line in body.splitlines())
        if body
        else "    pass"
    )

    pieces.append(f"def main():\n{indented}")
    pieces.append('if __name__ == "__main__":\n    main()')
    return "\n\n\n".join(pieces).strip() + "\n"


def build_target_files(blocks: list[Block]) -> tuple[dict[str, TargetFile], list[ImportStmt]]:
    targets: dict[str, TargetFile] = {}
    global_imports: list[ImportStmt] = []

    for block in blocks:
        tf = targets.setdefault(block.target, TargetFile(path=Path(block.target)))
        tf.raw_blocks.append(block.code)

        imports, code_chunks, defined = parse_code_chunk(block.code)
        global_imports.extend(imports)
        tf.other_code_chunks.extend(code_chunks)
        tf.defined_symbols.update(defined)

    return targets, dedupe_imports(global_imports)


def render_files(targets: dict[str, TargetFile], global_imports: list[ImportStmt]) -> dict[Path, str]:
    symbol_to_file: dict[str, Path] = {}
    for tf in targets.values():
        for sym in tf.defined_symbols:
            symbol_to_file[sym] = tf.path

    import_by_name: dict[str, list[str]] = {}
    for imp in global_imports:
        for name in imp.bound_names:
            import_by_name.setdefault(name, []).append(imp.code)

    rendered: dict[Path, str] = {}

    for tf in targets.values():
        raw_body = "\n\n".join(
            chunk.strip() for chunk in tf.other_code_chunks if chunk.strip()
        ).strip()

        if tf.path == Path("src/main.py"):
            body = normalize_main_file(tf.other_code_chunks).rstrip()
        else:
            body = raw_body

        used_names = collect_used_names(body) if body else set()

        needed_external_imports: list[str] = []
        for name in sorted(used_names):
            for stmt in import_by_name.get(name, []):
                needed_external_imports.append(stmt)

        needed_cross_imports: list[str] = []
        imported_names_from_external = {name for name in used_names if name in import_by_name}

        for name in sorted(used_names):
            owner = symbol_to_file.get(name)
            if not owner or owner == tf.path:
                continue
            if name in imported_names_from_external:
                continue

            mod = module_name_from_path(owner)
            needed_cross_imports.append(f"from {mod} import {name}")

        import_lines = sorted(dict.fromkeys(needed_external_imports + needed_cross_imports))

        parts: list[str] = []
        if import_lines:
            parts.append("\n".join(import_lines))
        if body:
            parts.append(body)

        rendered[tf.path] = "\n\n\n".join(parts).rstrip() + "\n"

    return rendered


def ensure_package_files(rendered: dict[Path, str]) -> None:
    rendered.setdefault(Path("src/lib/__init__.py"), "")


def write_rendered_files(rendered: dict[Path, str]) -> None:
    for path, content in rendered.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def should_run(changed_files: Sequence[str], all_files: bool) -> bool:
    if all_files:
        return True

    changed_paths = {Path(name) for name in changed_files}
    return NOTEBOOK in changed_paths


def run_export() -> int:
    if not NOTEBOOK.exists():
        print(f"Notebook not found: {NOTEBOOK}", file=sys.stderr)
        return 1

    blocks = notebook_to_blocks(NOTEBOOK)
    if not blocks:
        print(f"No code cells found in {NOTEBOOK}", file=sys.stderr)
        return 1

    backup_and_reset_src()

    targets, global_imports = build_target_files(blocks)
    rendered = render_files(targets, global_imports)
    ensure_package_files(rendered)
    write_rendered_files(rendered)

    for path in sorted(rendered):
        print(f"wrote {path}")

    return 0


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export code cells from notebooks/main.ipynb into src/."
    )
    parser.add_argument(
        "--all-files",
        action="store_true",
        help="Run regardless of the filenames passed by pre-commit.",
    )
    parser.add_argument(
        "filenames",
        nargs="*",
        help="Filenames passed by pre-commit.",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if not should_run(args.filenames, args.all_files):
        print(f"Skipping export: {NOTEBOOK} not in staged files.")
        return 0

    return run_export()


if __name__ == "__main__":
    raise SystemExit(main())
