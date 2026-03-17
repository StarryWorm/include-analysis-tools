#!/usr/bin/env python3
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal


def usage() -> None:
    script_name = Path(sys.argv[0]).name
    print(f"Usage: {script_name} [-filter|-f] <directory> <pattern> <include> [<ext>]")


def find_matching_files(root_dir: Path, pattern: str, ext: str) -> list[str]:
    if shutil.which("rg") is None:
        return find_matching_files_python(root_dir, pattern, ext)

    cmd = ["rg", f"-g*.{ext}", pattern, "-l"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=root_dir)
    except FileNotFoundError:
        return find_matching_files_python(root_dir, pattern, ext)

    if result.returncode not in (0, 1):
        stderr = result.stderr.strip()
        raise RuntimeError(stderr or "Failed to run rg")

    return [line.replace("\\", "/") for line in result.stdout.splitlines() if line.strip()]


def find_matching_files_python(root_dir: Path, pattern: str, ext: str) -> list[str]:
    try:
        regex = re.compile(pattern)
    except re.error as error:
        raise RuntimeError(f"Invalid regex pattern: {error}") from error

    matches: list[str] = []
    for path in root_dir.rglob(f"*.{ext}"):
        if not path.is_file():
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        if regex.search(text):
            matches.append(path.relative_to(root_dir).as_posix())

    return matches


def read_lines(file_path: Path) -> tuple[list[str], str]:
    text = file_path.read_text(encoding="utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    return lines, newline


def write_lines(file_path: Path, lines: list[str]) -> None:
    file_path.write_text("".join(lines), encoding="utf-8")


def has_include(file_path: Path, include: str) -> bool:
    text = file_path.read_text(encoding="utf-8")
    pattern = re.compile(rf'(#include|#import) "{re.escape(include)}"')
    return pattern.search(text) is not None


def insert_include_line(
    file_path: Path, include: str, has_header: bool, header: Path
) -> Literal["core", "header", "first_include", "copyright"]:
    lines, newline = read_lines(file_path)
    include_line = f'#include "{include}"{newline}'

    insert_at = None
    insertion_reason: Literal["core", "header", "first_include", "copyright"] = "copyright"

    for index, line in enumerate(lines):
        if '#include "core/' in line:
            insert_at = index + 1
            insertion_reason = "core"
            break

    if insert_at is None and has_header:
        target_line = header.name
        compat_basename = f"{header.stem}.compat.inc"

        file_text = "".join(lines)
        if compat_basename in file_text:
            target_line = compat_basename

        for index, line in enumerate(lines):
            if f'"{target_line}"' in line:
                insert_at = index + 1
                include_line = f'{newline}#include "{include}"{newline}'
                insertion_reason = "header"
                break

    if insert_at is None:
        for index, line in enumerate(lines):
            if "#include " in line:
                insert_at = index + 1
                insertion_reason = "first_include"
                break

    if insert_at is None:
        insert_at = min(32, len(lines))
        insertion_reason = "copyright"

    lines.insert(insert_at, include_line)
    write_lines(file_path, lines)
    return insertion_reason


def parse_args(argv: list[str]) -> tuple[Path, str, str, str, bool] | None:
    filter_output = False
    positional: list[str] = []

    for arg in argv:
        if arg in ("-filter", "-f"):
            filter_output = True
            continue

        if arg.startswith("-"):
            print(f"Error: unknown argument: {arg}", file=sys.stderr)
            return None

        positional.append(arg)

    if len(positional) < 3:
        return None

    root_dir = Path(positional[0]).resolve()
    pattern = positional[1]
    include = positional[2]
    ext = positional[3] if len(positional) >= 4 else "cpp"

    return root_dir, pattern, include, ext, filter_output


def insertion_message(reason: str) -> str:
    if reason == "core":
        return "> Adding after existing 'core/' include."
    if reason == "header":
        return "> Adding after associated header."
    if reason == "first_include":
        return "> Adding after first include."
    return "> Adding after copyright header."


def main() -> int:
    parsed = parse_args(sys.argv[1:])
    if parsed is None:
        usage()
        return 1

    root_dir, pattern, include, ext, filter_output = parsed

    if not root_dir.is_dir():
        print(f"Error: directory does not exist: {root_dir}", file=sys.stderr)
        return 1

    try:
        matches = find_matching_files(root_dir, pattern, ext)
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    include_as_ext = include.replace("h", ext, 1)

    for file in matches:
        if file == include_as_ext:
            continue

        file_path = root_dir / file
        header_path = file_path.with_suffix(".h")
        has_header_file = ext != "h" and header_path.exists()

        found_in_cpp = has_include(file_path, include)
        found_in_header = has_header_file and has_include(header_path, include)

        if not found_in_cpp and not found_in_header:
            reason = insert_include_line(file_path, include, has_header_file, header_path)

            if filter_output:
                if reason != "core":
                    print(f"{file}: #include \"{include}\"")
                continue

            print(f"### ADDING MISSING EXPLICIT INCLUDE IN {file} ###")
            print(insertion_message(reason))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
