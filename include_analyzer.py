from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import re
import threading
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, DoubleVar, StringVar, Tk, filedialog, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from typing import Callable, Dict, List, Optional, Set


class IncludeAnalyzer:
    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()
        self.include_cache: Dict[Path, List[str]] = {}
        self.file_paths_cache: Dict[tuple[str, Path], Optional[Path]] = {}
        self.direct_graph_cache: Dict[str, tuple[Set[Path], Dict[Path, Set[Path]]]] = {}
        self._cache_lock = threading.RLock()

    def clear_runtime_caches(self) -> None:
        with self._cache_lock:
            self.include_cache.clear()
            self.file_paths_cache.clear()
            self.direct_graph_cache.clear()

    @staticmethod
    def _emit_progress(
        progress_cb: Optional[Callable[[str, float], None]],
        message: str,
        value: float,
    ) -> None:
        if progress_cb is None:
            return
        safe_value = max(0.0, min(100.0, value))
        progress_cb(message, safe_value)

    @staticmethod
    def _canonical(path: Path) -> Path:
        return path.resolve()

    @staticmethod
    def recommended_worker_count() -> int:
        return max(1, (os.cpu_count() or 1) - 4)

    @staticmethod
    def _is_thirdparty(path: Path, project_root: Path) -> bool:
        try:
            relative_parts = path.resolve().relative_to(project_root.resolve()).parts
        except ValueError:
            return True
        return "thirdparty" in relative_parts

    def find_project_files(self) -> Set[Path]:
        extensions = {".cpp", ".h"}
        cpp_files: Set[Path] = set()

        for root, dirs, files in os.walk(self.project_root):
            dirs[:] = [
                directory
                for directory in dirs
                if directory not in {".git", "__pycache__", "build", "bin", "obj", ".vscode", ".idea", ".scu"}
            ]

            for file_name in files:
                if Path(file_name).suffix in extensions:
                    cpp_files.add(Path(root) / file_name)

        return {self._canonical(path) for path in cpp_files}

    def extract_includes(self, file_path: Path) -> List[str]:
        file_path = self._canonical(file_path)
        with self._cache_lock:
            cached = self.include_cache.get(file_path)
        if cached is not None:
            return cached

        includes: List[str] = []
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as file_handle:
                for line in file_handle:
                    match = re.match(r'^\s*#\s*include\s+[<"]([^>"]+)[>"]', line)
                    if match:
                        includes.append(match.group(1))
        except OSError:
            pass

        with self._cache_lock:
            self.include_cache[file_path] = includes
        return includes

    def resolve_include_path(self, include_name: str, current_file: Path, project_files: Set[Path]) -> Optional[Path]:
        current_file = self._canonical(current_file)
        cache_key = (include_name, current_file)

        with self._cache_lock:
            if cache_key in self.file_paths_cache:
                return self.file_paths_cache[cache_key]

        relative_path = self._canonical(current_file.parent / include_name)
        if relative_path in project_files:
            with self._cache_lock:
                self.file_paths_cache[cache_key] = relative_path
            return relative_path

        root_relative_path = self._canonical(self.project_root / include_name)
        if root_relative_path in project_files:
            with self._cache_lock:
                self.file_paths_cache[cache_key] = root_relative_path
            return root_relative_path

        include_filename = Path(include_name).name
        for project_file in project_files:
            if project_file.name == include_filename and str(project_file).endswith(include_name.replace("/", os.sep)):
                with self._cache_lock:
                    self.file_paths_cache[cache_key] = project_file
                return project_file

        include_parts = Path(include_name).parts
        if len(include_parts) > 1:
            for project_file in project_files:
                project_parts = project_file.parts
                if len(project_parts) >= len(include_parts) and project_parts[-len(include_parts):] == include_parts:
                    with self._cache_lock:
                        self.file_paths_cache[cache_key] = project_file
                    return project_file

        with self._cache_lock:
            self.file_paths_cache[cache_key] = None
        return None

    def build_include_lookup(
        self,
        progress_cb: Optional[Callable[[str, float], None]] = None,
        progress_start: float = 0.0,
        progress_end: float = 100.0,
        workers: Optional[int] = None,
    ) -> Dict[Path, Set[Path]]:
        project_files = self.find_project_files()
        filtered_files = {path for path in project_files if not self._is_thirdparty(path, self.project_root)}
        if workers is None:
            workers = self.recommended_worker_count()
        return self._build_direct_include_graph(
            project_files=project_files,
            source_files=filtered_files,
            workers=workers,
            include_thirdparty=False,
            cache_key="lookup_filtered",
            progress_cb=progress_cb,
            progress_start=progress_start,
            progress_end=progress_end,
            progress_message="Indexing include lookup",
        )

    def _resolve_direct_includes_for_file(
        self,
        file_path: Path,
        project_files: Set[Path],
        include_thirdparty: bool,
    ) -> Set[Path]:
        direct_includes: Set[Path] = set()
        for include_name in self.extract_includes(file_path):
            resolved = self.resolve_include_path(include_name, file_path, project_files)
            if not resolved:
                continue
            resolved = self._canonical(resolved)
            if resolved == file_path:
                continue
            if not include_thirdparty and self._is_thirdparty(resolved, self.project_root):
                continue
            direct_includes.add(resolved)
        return direct_includes

    def _build_direct_include_graph(
        self,
        project_files: Set[Path],
        source_files: Set[Path],
        workers: int,
        include_thirdparty: bool,
        cache_key: str,
        progress_cb: Optional[Callable[[str, float], None]],
        progress_start: float,
        progress_end: float,
        progress_message: str,
    ) -> Dict[Path, Set[Path]]:
        sorted_files = sorted(source_files)
        workers = max(1, min(workers, max(1, len(sorted_files))))

        with self._cache_lock:
            cached = self.direct_graph_cache.get(cache_key)
            if cached is not None:
                cached_source_files, cached_graph = cached
                if cached_source_files == source_files:
                    self._emit_progress(progress_cb, f"{progress_message} (cached)", progress_end)
                    return cached_graph

        graph: Dict[Path, Set[Path]] = {}
        total_files = max(1, len(sorted_files))
        progress_span = max(0.0, progress_end - progress_start)

        self._emit_progress(progress_cb, f"{progress_message}...", progress_start)

        def process_file(cpp_file: Path) -> tuple[Path, Set[Path]]:
            return cpp_file, self._resolve_direct_includes_for_file(cpp_file, project_files, include_thirdparty)

        if workers == 1:
            for index, cpp_file in enumerate(sorted_files, start=1):
                file_key, includes = process_file(cpp_file)
                graph[file_key] = includes
                progress_value = progress_start + progress_span * (index / total_files)
                self._emit_progress(progress_cb, f"{progress_message}... ({index}/{total_files})", progress_value)
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_map = {executor.submit(process_file, cpp_file): cpp_file for cpp_file in sorted_files}
                completed = 0
                for future in as_completed(future_map):
                    file_key, includes = future.result()
                    graph[file_key] = includes
                    completed += 1
                    progress_value = progress_start + progress_span * (completed / total_files)
                    self._emit_progress(progress_cb, f"{progress_message}... ({completed}/{total_files})", progress_value)

        with self._cache_lock:
            self.direct_graph_cache[cache_key] = (set(source_files), graph)

        return graph

    def collect_transitive_includes(
        self,
        file_path: Path,
        lookup: Dict[Path, Set[Path]],
        memo: Dict[Path, Set[Path]],
        visiting: Set[Path],
    ) -> Set[Path]:
        if file_path in memo:
            return memo[file_path]

        if file_path in visiting:
            return set()

        visiting.add(file_path)
        direct = lookup.get(file_path, set())
        accumulated: Set[Path] = set(direct)

        for child in direct:
            accumulated.update(self.collect_transitive_includes(child, lookup, memo, visiting))

        accumulated.discard(file_path)
        visiting.remove(file_path)
        memo[file_path] = accumulated
        return accumulated

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.project_root))
        except ValueError:
            return str(path.resolve())

    def report_file_include_analysis(
        self,
        input_file: Path,
        workers: Optional[int] = None,
        progress_cb: Optional[Callable[[str, float], None]] = None,
    ) -> str:
        start_file = self._canonical(input_file)
        if not start_file.exists():
            raise FileNotFoundError(f"Input file not found: {start_file}")

        if workers is None:
            workers = self.recommended_worker_count()
        workers = max(1, workers)

        self._emit_progress(progress_cb, "Scanning project files...", 5)
        project_files = self.find_project_files()
        include_graph = self._build_direct_include_graph(
            project_files=project_files,
            source_files=project_files,
            workers=workers,
            include_thirdparty=True,
            cache_key="file_all",
            progress_cb=progress_cb,
            progress_start=10,
            progress_end=70,
            progress_message="Pre-indexing direct includes",
        )

        visited: Set[Path] = set()
        to_visit: List[tuple[Path, List[Path]]] = [(start_file, [])]
        all_includes: Set[Path] = set()
        include_paths: Dict[Path, List[List[Path]]] = {}
        visit_budget = max(1, len(include_graph) + 1)

        while to_visit:
            current_file, current_path = to_visit.pop(0)
            if current_file in visited:
                continue

            visited.add(current_file)
            bfs_progress = 70 + 22 * (min(len(visited), visit_budget) / visit_budget)
            self._emit_progress(
                progress_cb,
                f"Resolving include graph... ({len(visited)} visited)",
                bfs_progress,
            )

            direct_includes = include_graph.get(current_file)
            if direct_includes is None:
                direct_includes = self._resolve_direct_includes_for_file(current_file, project_files, include_thirdparty=True)
                include_graph[current_file] = direct_includes

            for resolved_path in direct_includes:
                if resolved_path == start_file:
                    continue

                resolved_path = self._canonical(resolved_path)
                new_path = current_path + [current_file, resolved_path]

                if resolved_path not in all_includes:
                    all_includes.add(resolved_path)
                    include_paths[resolved_path] = [new_path]
                    to_visit.append((resolved_path, current_path + [current_file]))
                elif new_path not in include_paths[resolved_path]:
                    include_paths[resolved_path].append(new_path)

        self._emit_progress(progress_cb, "Formatting report output...", 92)
        included_split = self._split_cpp_h_counts(all_includes)

        lines: List[str] = [
            "# C++ Include Dependency Analysis",
            "",
            "## Summary",
        ]

        summary_rows = [
            ["Input File", str(start_file)],
            ["Project Root", str(self.project_root)],
            ["Total Included Files", str(len(all_includes))],
            [
                "Included split (.h / .cpp / other)",
                f"{included_split['.h']} / {included_split['.cpp']} / {included_split['other']}",
            ],
            ["Analysis Date", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
        ]
        lines.extend(self._format_markdown_table(["Metric", "Value"], summary_rows))
        lines.append("")

        include_rows: List[List[str]] = []
        for file_path in sorted(all_includes):
            include_rows.append([self._display_path(file_path), str(len(include_paths.get(file_path, [])))])

        lines.append("## Included Files")
        if include_rows:
            lines.extend(self._format_markdown_table(["File", "Path Count"], include_rows))
        else:
            lines.append("No included files found.")
        lines.append("")

        for file_path in sorted(all_includes):
            lines.append(f"### {self._display_path(file_path)}")
            path_rows: List[List[str]] = []
            for path_index, include_path in enumerate(include_paths.get(file_path, []), start=1):
                include_chain = " -> ".join(self._display_path(step) for step in include_path)
                path_rows.append([str(path_index), include_chain])

            if path_rows:
                lines.extend(self._format_markdown_table(["Path #", "Include Chain"], path_rows))
            else:
                lines.append("No include paths found.")
            lines.append("")

        self._emit_progress(progress_cb, "Done", 100)
        return "\n".join(lines)

    def report_project_totals(
        self,
        headers_only: bool = False,
        include_project_include_sum: bool = False,
        include_project_include_sum_cpp_only: bool = True,
        include_header_ranking: bool = False,
        top_n: int = 50,
        header_ranking_count_transitive: bool = True,
        header_ranking_sort_by: str = "total",
        workers: Optional[int] = None,
        progress_cb: Optional[Callable[[str, float], None]] = None,
    ) -> str:
        self._emit_progress(progress_cb, "Building project include index...", 5)
        lookup = self.build_include_lookup(
            progress_cb=progress_cb,
            progress_start=10,
            progress_end=55,
            workers=workers,
        )
        transitive_cache = self._build_transitive_cache(
            lookup,
            progress_cb=progress_cb,
            progress_start=55,
            progress_end=80,
            workers=workers,
        )
        project_wide_unique: Set[Path] = set()

        for file_path, includes in transitive_cache.items():
            if headers_only:
                project_wide_unique.update(path for path in includes if path.suffix == ".h")
            else:
                project_wide_unique.update(includes)

        project_split = self._split_cpp_h_counts(project_wide_unique)

        lines = [
            "# Project Include Totals",
            "",
            "## Summary",
        ]

        summary_rows = [
            ["Project root", str(self.project_root)],
            ["Files analyzed", str(len(lookup))],
            ["Count mode", "headers only (.h)" if headers_only else "all included files (.h + .cpp)"],
            ["Transitive includes (unique project-wide)", str(len(project_wide_unique))],
            ["Transitive split (.h / .cpp / other)", f"{project_split['.h']} / {project_split['.cpp']} / {project_split['other']}"]
        ]

        if include_project_include_sum:
            project_include_sum = 0
            project_include_sum_split = {".h": 0, ".cpp": 0, "other": 0}
            sum_source_files = (
                [file_path for file_path in transitive_cache if file_path.suffix.lower() == ".cpp"]
                if include_project_include_sum_cpp_only
                else list(transitive_cache.keys())
            )

            for file_path in sum_source_files:
                includes = transitive_cache.get(file_path, set())
                if headers_only:
                    scoped_includes = {path for path in includes if path.suffix.lower() == ".h"}
                else:
                    scoped_includes = includes

                project_include_sum += len(scoped_includes)
                split = self._split_cpp_h_counts(scoped_includes)
                project_include_sum_split[".h"] += split[".h"]
                project_include_sum_split[".cpp"] += split[".cpp"]
                project_include_sum_split["other"] += split["other"]

            summary_rows.append(["Sum of each file's unique transitive includes", str(project_include_sum)])
            summary_rows.append([
                "Files counted in include sum",
                (
                    f"{len(sum_source_files)} (.cpp only)"
                    if include_project_include_sum_cpp_only
                    else f"{len(sum_source_files)} (all files)"
                ),
            ])
            summary_rows.append(
                [
                    "Per-file transitive include sum split (.h / .cpp / other)",
                    f"{project_include_sum_split['.h']} / {project_include_sum_split['.cpp']} / {project_include_sum_split['other']}",
                ]
            )

        lines.extend(self._format_markdown_table(["Metric", "Value"], summary_rows))
        lines.append("")

        if include_header_ranking:
            header_top, header_count, used_workers = self.analyze_all_headers_include_totals(
                lookup,
                top_n=top_n,
                count_transitive=header_ranking_count_transitive,
                sort_by=header_ranking_sort_by,
                workers=workers,
                progress_cb=progress_cb,
                progress_start=80,
                progress_end=98,
            )

            lines.append("## Header Include Analysis (Top N)")
            meta_rows = [
                ["Headers analyzed", str(header_count)],
                ["Workers used", str(used_workers)],
                ["Top N", str(max(1, top_n))],
                ["Count transitive includes", "yes" if header_ranking_count_transitive else "no (direct only)"],
                ["Sort by", header_ranking_sort_by],
            ]
            lines.extend(self._format_markdown_table(["Metric", "Value"], meta_rows))
            lines.append("")

            if not header_top:
                lines.append("No headers found to analyze.")
            else:
                header_rows: List[List[str]] = []
                for rank, (header_path, total_count, h_count, cpp_count, other_count) in enumerate(header_top, start=1):
                    header_rows.append(
                        [
                            str(rank),
                            self._display_path(header_path),
                            str(total_count),
                            str(h_count),
                            str(cpp_count),
                            str(other_count),
                        ]
                    )
                lines.extend(self._format_markdown_table(["Rank", "Header", "Total", ".h", ".cpp", "Other"], header_rows))

        self._emit_progress(progress_cb, "Done", 100)
        return "\n".join(lines)

    def _validate_target(self, lookup: Dict[Path, Set[Path]], target_file: Path) -> Path:
        target = self._canonical(target_file)
        if not target.exists():
            raise FileNotFoundError(f"Target file not found: {target}")

        if self._is_thirdparty(target, self.project_root):
            raise ValueError("Target file resides in a thirdparty directory")

        if target not in lookup and not any(target in includes for includes in lookup.values()):
            return target

        return target

    def _build_transitive_cache(
        self,
        lookup: Dict[Path, Set[Path]],
        progress_cb: Optional[Callable[[str, float], None]] = None,
        progress_start: float = 0.0,
        progress_end: float = 100.0,
        workers: Optional[int] = None,
    ) -> Dict[Path, Set[Path]]:
        cache: Dict[Path, Set[Path]] = {}
        if workers is None:
            workers = self.recommended_worker_count()

        workers = max(1, min(workers, max(1, len(lookup))))

        total_files = max(1, len(lookup))
        progress_span = max(0.0, progress_end - progress_start)

        def process_file(file_path: Path) -> tuple[Path, Set[Path]]:
            return file_path, self.collect_transitive_includes(file_path, lookup, {}, set())

        if workers == 1:
            for index, file_path in enumerate(lookup, start=1):
                path_key, value = process_file(file_path)
                cache[path_key] = value
                progress_value = progress_start + progress_span * (index / total_files)
                self._emit_progress(
                    progress_cb,
                    f"Computing transitive includes... ({index}/{total_files})",
                    progress_value,
                )
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_map = {executor.submit(process_file, file_path): file_path for file_path in lookup}
                completed = 0
                for future in as_completed(future_map):
                    path_key, value = future.result()
                    cache[path_key] = value
                    completed += 1
                    progress_value = progress_start + progress_span * (completed / total_files)
                    self._emit_progress(
                        progress_cb,
                        f"Computing transitive includes... ({completed}/{total_files})",
                        progress_value,
                    )

        return cache

    @staticmethod
    def _split_cpp_h_counts(paths: Set[Path]) -> Dict[str, int]:
        counts = {".cpp": 0, ".h": 0, "other": 0}
        for path in paths:
            suffix = path.suffix.lower()
            if suffix in {".cpp", ".h"}:
                counts[suffix] += 1
            else:
                counts["other"] += 1
        return counts

    @staticmethod
    def _format_markdown_table(headers: List[str], rows: List[List[str]]) -> List[str]:
        if not headers:
            return []

        normalized_rows = [[str(cell) if cell is not None else "" for cell in row] for row in rows]
        def escape_cell(value: str) -> str:
            return value.replace("|", "\\|")

        header_line = "| " + " | ".join(escape_cell(str(header)) for header in headers) + " |"
        separator_line = "| " + " | ".join("---" for _ in headers) + " |"

        lines = [header_line, separator_line]
        for row in normalized_rows:
            cells = []
            for index in range(len(headers)):
                value = row[index] if index < len(row) else ""
                cells.append(escape_cell(value))
            lines.append("| " + " | ".join(cells) + " |")
        return lines

    @staticmethod
    def _collect_reachable_from_start(start: Path, lookup: Dict[Path, Set[Path]]) -> Set[Path]:
        visited: Set[Path] = set()
        stack: List[Path] = [start]

        while stack:
            current = stack.pop()
            for child in lookup.get(current, set()):
                if child in visited:
                    continue
                visited.add(child)
                stack.append(child)

        visited.discard(start)
        return visited

    @staticmethod
    def _build_reverse_lookup(lookup: Dict[Path, Set[Path]]) -> Dict[Path, Set[Path]]:
        reverse_lookup: Dict[Path, Set[Path]] = {}
        for parent, children in lookup.items():
            reverse_lookup.setdefault(parent, set())
            for child in children:
                reverse_lookup.setdefault(child, set()).add(parent)
        return reverse_lookup

    @staticmethod
    def _collect_reverse_reachable(start: Path, reverse_lookup: Dict[Path, Set[Path]]) -> Set[Path]:
        visited: Set[Path] = set()
        stack: List[Path] = [start]

        while stack:
            current = stack.pop()
            for parent in reverse_lookup.get(current, set()):
                if parent in visited:
                    continue
                visited.add(parent)
                stack.append(parent)

        visited.discard(start)
        return visited

    def analyze_all_headers_include_totals(
        self,
        lookup: Dict[Path, Set[Path]],
        top_n: int = 50,
        count_transitive: bool = True,
        sort_by: str = "total",
        workers: Optional[int] = None,
        progress_cb: Optional[Callable[[str, float], None]] = None,
        progress_start: float = 0.0,
        progress_end: float = 100.0,
    ) -> tuple[List[tuple[Path, int, int, int, int]], int, int]:
        headers = sorted(path for path in lookup if path.suffix.lower() == ".h")
        header_count = len(headers)

        if header_count == 0:
            self._emit_progress(progress_cb, "Header analysis complete (0 headers)", progress_end)
            return [], 0, 0

        if workers is None:
            workers = self.recommended_worker_count()

        workers = max(1, min(workers, header_count))
        top_n = max(1, top_n)
        sort_by = sort_by.lower().strip()
        if sort_by not in {"total", "h", "cpp"}:
            sort_by = "total"
        progress_span = max(0.0, progress_end - progress_start)
        reverse_lookup = self._build_reverse_lookup(lookup)

        progress_title = "Analyzing transitive dependents for headers" if count_transitive else "Analyzing direct includers for headers"
        self._emit_progress(progress_cb, f"{progress_title}...", progress_start)

        results: List[tuple[Path, int, int, int, int]] = []
        if count_transitive:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                future_to_header = {
                    executor.submit(self._collect_reverse_reachable, header, reverse_lookup): header for header in headers
                }

                completed = 0
                for future in as_completed(future_to_header):
                    header = future_to_header[future]
                    includers = future.result()
                    split = self._split_cpp_h_counts(includers)
                    total_count = len(includers)
                    results.append((header, total_count, split[".h"], split[".cpp"], split["other"]))

                    completed += 1
                    progress_value = progress_start + progress_span * (completed / header_count)
                    self._emit_progress(
                        progress_cb,
                        f"{progress_title}... ({completed}/{header_count})",
                        progress_value,
                    )
        else:
            for index, header in enumerate(headers, start=1):
                includers = reverse_lookup.get(header, set())
                split = self._split_cpp_h_counts(includers)
                total_count = len(includers)
                results.append((header, total_count, split[".h"], split[".cpp"], split["other"]))
                progress_value = progress_start + progress_span * (index / header_count)
                self._emit_progress(
                    progress_cb,
                    f"{progress_title}... ({index}/{header_count})",
                    progress_value,
                )

        def sort_key(item: tuple[Path, int, int, int, int]) -> tuple[int, int, str]:
            _, total_count, h_count, cpp_count, _ = item
            if sort_by == "h":
                metric = h_count
            elif sort_by == "cpp":
                metric = cpp_count
            else:
                metric = total_count
            return (-metric, -total_count, str(item[0]).lower())

        results.sort(key=sort_key)
        return results[:top_n], header_count, workers

    def _calculate_dependents_data(
        self,
        target_file: Path,
        workers: Optional[int] = None,
        progress_cb: Optional[Callable[[str, float], None]] = None,
    ) -> tuple[Path, Set[Path], Set[Path], Counter[Path], Dict[Path, Set[Path]], Dict[Path, Set[Path]]]:
        self._emit_progress(progress_cb, "Building project include index...", 5)
        lookup = self.build_include_lookup(
            progress_cb=progress_cb,
            progress_start=10,
            progress_end=45,
            workers=workers,
        )
        target = self._validate_target(lookup, target_file)

        self._emit_progress(progress_cb, "Computing transitive includes...", 48)
        transitive_cache = self._build_transitive_cache(
            lookup,
            progress_cb=progress_cb,
            progress_start=48,
            progress_end=75,
            workers=workers,
        )

        self._emit_progress(progress_cb, "Identifying includers...", 78)

        # Direct includers: files whose direct include set contains target.
        direct_includers = {fp for fp, includes in lookup.items() if target in includes}

        # All includers: files whose transitive include set contains target.
        including_files = {fp for fp, trans in transitive_cache.items() if target in trans}

        # For each file compute its gateway set (which direct includers
        # of target sit in the file's own transitive include set).
        self._emit_progress(progress_cb, "Computing breakdown...", 82)
        breakdown: Counter[Path] = Counter()
        gateway_map: Dict[Path, Set[Path]] = {}
        transitive_only = sorted(including_files - direct_includers)
        total = max(1, len(transitive_only))

        # Every direct includer is its own gateway (count = 1).
        for di in direct_includers:
            breakdown[di] = 1
            gateway_map[di] = {di}

        # Attribute transitive-only includers to their gateways.
        for idx, file_path in enumerate(transitive_only, start=1):
            trans = transitive_cache.get(file_path, set())
            gateways = trans & direct_includers
            gateway_map[file_path] = gateways
            for gw in gateways:
                breakdown[gw] += 1
            if idx % 200 == 0 or idx == total:
                self._emit_progress(
                    progress_cb,
                    f"Computing breakdown... ({idx}/{total})",
                    82 + 15 * (idx / total),
                )

        return target, direct_includers, including_files, breakdown, gateway_map, transitive_cache

    def report_dependents(
        self,
        target_file: Path,
        include_breakdown: bool = False,
        include_dependents_include_sum: bool = False,
        include_dependents_include_sum_cpp_only: bool = True,
        hide_cpp_in_breakdown: bool = True,
        show_detail: bool = False,
        workers: Optional[int] = None,
        progress_cb: Optional[Callable[[str, float], None]] = None,
    ) -> str:
        target, direct_includers, including_files, breakdown, gateway_map, transitive_cache = self._calculate_dependents_data(
            target_file,
            workers=workers,
            progress_cb=progress_cb,
        )

        lines = [
            "# Include Dependents Report",
            "",
            "## Summary",
        ]

        summary_rows = [
            ["Project root", str(self.project_root)],
            ["Target file", self._display_path(target)],
            ["Direct includers", str(len(direct_includers))],
            ["Total includers (direct + transitive)", str(len(including_files))],
        ]

        if include_dependents_include_sum:
            dependents_unique_include_sum = 0
            dependents_split_sum = {".h": 0, ".cpp": 0, "other": 0}
            sum_dependents = (
                {path for path in including_files if path.suffix.lower() == ".cpp"}
                if include_dependents_include_sum_cpp_only
                else set(including_files)
            )

            for dependent in sum_dependents:
                includes_for_dependent = transitive_cache.get(dependent, set())
                dependents_unique_include_sum += len(includes_for_dependent)
                split = self._split_cpp_h_counts(includes_for_dependent)
                dependents_split_sum[".h"] += split[".h"]
                dependents_split_sum[".cpp"] += split[".cpp"]
                dependents_split_sum["other"] += split["other"]

            summary_rows.append(
                [
                    "Sum of each includer's unique includes",
                    str(dependents_unique_include_sum),
                ]
            )
            summary_rows.append(
                [
                    "Includers counted in include sum",
                    (
                        f"{len(sum_dependents)} (.cpp only)"
                        if include_dependents_include_sum_cpp_only
                        else f"{len(sum_dependents)} (all includers)"
                    ),
                ]
            )
            summary_rows.append(
                [
                    "Dependent include sum split (.h / .cpp / other)",
                    f"{dependents_split_sum['.h']} / {dependents_split_sum['.cpp']} / {dependents_split_sum['other']}",
                ]
            )

        lines.extend(self._format_markdown_table(["Metric", "Value"], summary_rows))
        lines.append("")

        direct_split = self._split_cpp_h_counts(direct_includers)
        total_split = self._split_cpp_h_counts(including_files)

        lines.append("## Split Totals (.h / .cpp / other)")
        split_rows = [
            ["Direct includers", str(direct_split[".h"]), str(direct_split[".cpp"]), str(direct_split["other"])],
            ["Total includers", str(total_split[".h"]), str(total_split[".cpp"]), str(total_split["other"])],
        ]
        lines.extend(self._format_markdown_table(["Category", ".h", ".cpp", "Other"], split_rows))
        lines.append("")

        if include_breakdown:
            lines.append("## Direct Includer Breakdown")

            printed = False
            breakdown_rows: List[List[str]] = []
            for path, count in breakdown.most_common():
                if hide_cpp_in_breakdown and path.suffix.lower() == ".cpp":
                    continue
                breakdown_rows.append([self._display_path(path), str(count)])
                printed = True

            if not printed:
                lines.append("No direct includers discovered; nothing to list.")
            else:
                lines.extend(self._format_markdown_table(["Direct Includer", "Files Through Gateway"], breakdown_rows))
            lines.append("")

        if show_detail:
            transitive_only = including_files - direct_includers
            no_gateway = {fp for fp in transitive_only if not gateway_map.get(fp)}

            lines.append("## Diagnostic Detail")
            lines.append("")
            diag_rows = [
                ["Direct includers", str(len(direct_includers))],
                ["Transitive-only includers", str(len(transitive_only))],
                ["Transitive-only with NO gateway found", str(len(no_gateway))],
                ["Sum of breakdown counts", str(sum(breakdown.values()))],
            ]
            lines.extend(self._format_markdown_table(["Metric", "Value"], diag_rows))
            lines.append("")

            # --- Direct includers ---
            lines.append("### Direct Includers")
            lines.append("")
            direct_rows: List[List[str]] = []
            for fp in sorted(direct_includers):
                direct_rows.append([self._display_path(fp)])
            if direct_rows:
                lines.extend(self._format_markdown_table(["File"], direct_rows))
            else:
                lines.append("(none)")
            lines.append("")

            # --- Transitive-only includers ---
            lines.append("### Transitive-Only Includers")
            lines.append("")
            trans_rows: List[List[str]] = []
            for fp in sorted(transitive_only):
                gws = gateway_map.get(fp, set())
                gw_display = ", ".join(sorted(self._display_path(g) for g in gws)) if gws else "**NONE FOUND**"
                trans_rows.append([self._display_path(fp), gw_display])
            if trans_rows:
                lines.extend(self._format_markdown_table(["File", "Gateways"], trans_rows))
            else:
                lines.append("(none)")
            lines.append("")

            if no_gateway:
                lines.append("### Unattributed Files (no gateway found)")
                lines.append("")
                for fp in sorted(no_gateway):
                    lines.append(f"- {self._display_path(fp)}")
                lines.append("")

        self._emit_progress(progress_cb, "Done", 100)
        return "\n".join(lines)

    def report_dependents_breakdown(self, target_file: Path) -> str:
        return self.report_dependents(target_file, include_breakdown=True, hide_cpp_in_breakdown=True)


class IncludeAnalyzerGUI:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("Include Analyzer")
        self.root.geometry("1000x700")

        self.project_root_var = StringVar()
        self.file_var = StringVar()
        self.file_label_var = StringVar(value="Input file:")
        self.analysis_mode_var = StringVar(value="File include analysis")
        self.worker_count_var = StringVar(value=str(IncludeAnalyzer.recommended_worker_count()))
        self.headers_only_var = BooleanVar(value=False)
        self.include_project_include_sum_var = BooleanVar(value=False)
        self.include_project_include_sum_cpp_only_var = BooleanVar(value=True)
        self.include_header_ranking_var = BooleanVar(value=False)
        self.header_ranking_count_transitive_var = BooleanVar(value=True)
        self.header_ranking_sort_var = StringVar(value="total")
        self.top_n_var = StringVar(value="50")
        self.include_breakdown_var = BooleanVar(value=False)
        self.include_dependents_include_sum_var = BooleanVar(value=False)
        self.include_dependents_include_sum_cpp_only_var = BooleanVar(value=True)
        self.hide_cpp_in_breakdown_var = BooleanVar(value=True)
        self.show_detail_var = BooleanVar(value=False)
        self.progress_value_var = DoubleVar(value=0.0)
        self.progress_status_var = StringVar(value="Ready")
        self.last_report = ""
        self.analyzer_instances: Dict[Path, IncludeAnalyzer] = {}

        self._build_ui()

    def _build_ui(self) -> None:
        main_frame = ttk.Frame(self.root, padding=12)
        main_frame.pack(fill="both", expand=True)

        mode_frame = ttk.LabelFrame(main_frame, text="Analysis", padding=10)
        mode_frame.pack(fill="x")

        analysis_values = [
            "File include analysis",
            "Project totals",
            "Dependents report",
        ]

        ttk.Label(mode_frame, text="Mode:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        mode_combo = ttk.Combobox(
            mode_frame,
            textvariable=self.analysis_mode_var,
            values=analysis_values,
            state="readonly",
            width=30,
        )
        mode_combo.grid(row=0, column=1, sticky="w", pady=4)
        mode_combo.bind("<<ComboboxSelected>>", self._on_mode_change)

        self.options_frame = ttk.LabelFrame(main_frame, text="Options", padding=10)
        self.options_frame.pack(fill="x", pady=(10, 0))

        self.worker_count_label = ttk.Label(self.options_frame, text="Threads:")
        self.worker_count_entry = ttk.Entry(self.options_frame, textvariable=self.worker_count_var, width=10)

        self.headers_only_check = ttk.Checkbutton(
            self.options_frame,
            text="Headers only (.h)",
            variable=self.headers_only_var,
        )
        self.include_header_ranking_check = ttk.Checkbutton(
            self.options_frame,
            text="Analyze all headers and show Top N",
            variable=self.include_header_ranking_var,
            command=self._refresh_dynamic_sections,
        )
        self.include_project_include_sum_check = ttk.Checkbutton(
            self.options_frame,
            text="Add sum of each file's unique transitive includes",
            variable=self.include_project_include_sum_var,
            command=self._refresh_dynamic_sections,
        )
        self.include_project_include_sum_cpp_only_check = ttk.Checkbutton(
            self.options_frame,
            text="Only count .cpp files for include sum",
            variable=self.include_project_include_sum_cpp_only_var,
        )
        self.header_ranking_count_transitive_check = ttk.Checkbutton(
            self.options_frame,
            text="Count transitive includes",
            variable=self.header_ranking_count_transitive_var,
        )
        self.header_ranking_sort_label = ttk.Label(self.options_frame, text="Sort by:")
        self.header_ranking_sort_combo = ttk.Combobox(
            self.options_frame,
            textvariable=self.header_ranking_sort_var,
            values=["total", "h", "cpp"],
            state="readonly",
            width=10,
        )
        self.top_n_label = ttk.Label(self.options_frame, text="Top N:")
        self.top_n_entry = ttk.Entry(self.options_frame, textvariable=self.top_n_var, width=10)
        self.include_breakdown_check = ttk.Checkbutton(
            self.options_frame,
            text="Show breakdown",
            variable=self.include_breakdown_var,
        )
        self.include_dependents_include_sum_check = ttk.Checkbutton(
            self.options_frame,
            text="Add sum of each includer's unique includes",
            variable=self.include_dependents_include_sum_var,
            command=self._refresh_dynamic_sections,
        )
        self.include_dependents_include_sum_cpp_only_check = ttk.Checkbutton(
            self.options_frame,
            text="Only count .cpp includers for include sum",
            variable=self.include_dependents_include_sum_cpp_only_var,
        )
        self.hide_cpp_in_breakdown_check = ttk.Checkbutton(
            self.options_frame,
            text="Hide .cpp files in includer list",
            variable=self.hide_cpp_in_breakdown_var,
        )
        self.show_detail_check = ttk.Checkbutton(
            self.options_frame,
            text="Show diagnostic detail (all includers + gateways)",
            variable=self.show_detail_var,
        )

        self.paths_frame = ttk.LabelFrame(main_frame, text="Paths", padding=10)
        self.paths_frame.pack(fill="x", pady=(10, 0))

        self.project_root_label = ttk.Label(self.paths_frame, text="Project root:")
        self.project_root_entry = ttk.Entry(self.paths_frame, textvariable=self.project_root_var)
        self.project_root_button = ttk.Button(self.paths_frame, text="Browse...", command=self._browse_project)

        self.file_label = ttk.Label(self.paths_frame, textvariable=self.file_label_var)
        self.file_entry = ttk.Entry(self.paths_frame, textvariable=self.file_var)
        self.file_button = ttk.Button(self.paths_frame, text="Browse...", command=self._browse_file)

        self.paths_frame.columnconfigure(1, weight=1)

        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill="x", pady=(10, 0))

        self.run_button = ttk.Button(button_frame, text="Run Analysis", command=self.run_analysis)
        self.run_button.pack(side="left")
        self.clear_cache_button = ttk.Button(button_frame, text="Clear Cache", command=self.clear_cache)
        self.clear_cache_button.pack(side="left", padx=(8, 0))
        self.copy_report_button = ttk.Button(button_frame, text="Copy Report", command=self.copy_report)
        self.copy_report_button.pack(side="left", padx=(8, 0))
        ttk.Button(button_frame, text="Save Report", command=self.save_report).pack(side="left", padx=(8, 0))
        ttk.Button(button_frame, text="Clear Output", command=self.clear_output).pack(side="left", padx=(8, 0))

        progress_frame = ttk.LabelFrame(main_frame, text="Progress", padding=8)
        progress_frame.pack(fill="x", pady=(10, 0))

        self.progress_label = ttk.Label(progress_frame, textvariable=self.progress_status_var)
        self.progress_label.pack(anchor="w")
        self.progress_bar = ttk.Progressbar(
            progress_frame,
            mode="determinate",
            maximum=100,
            variable=self.progress_value_var,
        )
        self.progress_bar.pack(fill="x", pady=(6, 0))

        output_frame = ttk.LabelFrame(main_frame, text="Output", padding=8)
        output_frame.pack(fill="both", expand=True, pady=(10, 0))

        self.output_text = ScrolledText(output_frame, wrap="word")
        self.output_text.pack(fill="both", expand=True)
        self.output_text.configure(state="disabled")

        self._refresh_dynamic_sections()

    def _on_mode_change(self, _event=None) -> None:
        self._refresh_dynamic_sections()

    def _refresh_dynamic_sections(self) -> None:
        mode = self.analysis_mode_var.get()

        for child in self.options_frame.winfo_children():
            grid_forget = getattr(child, "grid_forget", None)
            if callable(grid_forget):
                grid_forget()

        self.worker_count_label.grid(row=0, column=0, sticky="w", pady=4)
        self.worker_count_entry.grid(row=0, column=1, sticky="w", padx=(6, 0), pady=4)

        if mode == "Project totals":
            self.headers_only_check.grid(row=1, column=0, sticky="w", pady=4)
            self.include_project_include_sum_check.grid(row=2, column=0, sticky="w", pady=4)
            if self.include_project_include_sum_var.get():
                self.include_project_include_sum_cpp_only_check.grid(row=3, column=0, sticky="w", pady=4)
            self.include_header_ranking_check.grid(row=4, column=0, sticky="w", pady=4)
            if self.include_header_ranking_var.get():
                self.header_ranking_count_transitive_check.grid(row=5, column=0, sticky="w", pady=4)
                self.top_n_label.grid(row=6, column=0, sticky="w", pady=4)
                self.top_n_entry.grid(row=6, column=1, sticky="w", padx=(6, 0), pady=4)
                self.header_ranking_sort_label.grid(row=7, column=0, sticky="w", pady=4)
                self.header_ranking_sort_combo.grid(row=7, column=1, sticky="w", padx=(6, 0), pady=4)
        else:
            self.headers_only_var.set(False)
            self.include_project_include_sum_var.set(False)
            self.include_project_include_sum_cpp_only_var.set(True)
            self.include_header_ranking_var.set(False)
            self.header_ranking_count_transitive_var.set(True)
            self.header_ranking_sort_var.set("total")

        if mode == "Dependents report":
            self.include_breakdown_check.grid(row=1, column=0, sticky="w", pady=4)
            self.include_dependents_include_sum_check.grid(row=2, column=0, sticky="w", pady=4)
            if self.include_dependents_include_sum_var.get():
                self.include_dependents_include_sum_cpp_only_check.grid(row=3, column=0, sticky="w", pady=4)
            self.hide_cpp_in_breakdown_check.grid(row=4, column=0, sticky="w", pady=4)
            self.show_detail_check.grid(row=5, column=0, sticky="w", pady=4)
        else:
            self.include_breakdown_var.set(False)
            self.include_dependents_include_sum_var.set(False)
            self.include_dependents_include_sum_cpp_only_var.set(True)
            self.hide_cpp_in_breakdown_var.set(True)
            self.show_detail_var.set(False)

        for child in self.paths_frame.winfo_children():
            grid_forget = getattr(child, "grid_forget", None)
            if callable(grid_forget):
                grid_forget()

        self.project_root_label.grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        self.project_root_entry.grid(row=0, column=1, sticky="ew", pady=4)
        self.project_root_button.grid(row=0, column=2, padx=(6, 0), pady=4)

        requires_file = mode in {"File include analysis", "Dependents report"}
        if requires_file:
            if mode == "File include analysis":
                self.file_label_var.set("Input file:")
            else:
                self.file_label_var.set("Target file:")

            self.file_label.grid(row=1, column=0, sticky="w", padx=(0, 6), pady=4)
            self.file_entry.grid(row=1, column=1, sticky="ew", pady=4)
            self.file_button.grid(row=1, column=2, padx=(6, 0), pady=4)

    def _browse_project(self) -> None:
        selected = filedialog.askdirectory(title="Select project root")
        if selected:
            self.project_root_var.set(selected)

    def _browse_file(self) -> None:
        selected = filedialog.askopenfilename(title="Select input/target file")
        if selected:
            self.file_var.set(selected)

    def _set_progress(self, status: str, value: float) -> None:
        safe_value = max(0.0, min(100.0, value))
        self.progress_status_var.set(status)
        self.progress_value_var.set(safe_value)
        self.root.update_idletasks()

    def _get_analyzer(self, project_root: Path) -> IncludeAnalyzer:
        analyzer = self.analyzer_instances.get(project_root)
        if analyzer is None:
            analyzer = IncludeAnalyzer(project_root)
            self.analyzer_instances[project_root] = analyzer
        return analyzer

    def clear_cache(self) -> None:
        project_text = self.project_root_var.get().strip()
        if project_text:
            project_root = Path(project_text).resolve()
            analyzer = self.analyzer_instances.get(project_root)
            if analyzer is not None:
                analyzer.clear_runtime_caches()
                self._set_progress("Cache cleared for selected project", 0)
                messagebox.showinfo("Cache cleared", f"Cleared cached analysis data for:\n{project_root}")
                return

        if not self.analyzer_instances:
            self._set_progress("No cache to clear", 0)
            messagebox.showinfo("Cache cleared", "No analyzer cache is currently loaded.")
            return

        for analyzer in self.analyzer_instances.values():
            analyzer.clear_runtime_caches()

        self._set_progress("Cache cleared for all loaded projects", 0)
        messagebox.showinfo("Cache cleared", "Cleared cached analysis data for all loaded projects.")

    def clear_output(self) -> None:
        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", "end")
        self.output_text.configure(state="disabled")
        self.last_report = ""

    def copy_report(self) -> None:
        report_text = self.last_report.strip()
        if not report_text:
            messagebox.showwarning("No report", "Run an analysis first before copying.")
            return

        self.root.clipboard_clear()
        self.root.clipboard_append(report_text)
        self.root.update()
        self._set_progress("Report copied to clipboard", self.progress_value_var.get())

    def _require_project_root(self) -> Path:
        project_root = Path(self.project_root_var.get().strip())
        if not project_root or not project_root.is_dir():
            raise ValueError("Please choose a valid project root folder.")
        return project_root.resolve()

    def _require_file(self) -> Path:
        file_path = Path(self.file_var.get().strip())
        if not file_path or not file_path.exists():
            raise ValueError("Please choose a valid input/target file.")
        return file_path.resolve()

    @staticmethod
    def _require_positive_int(value_text: str, field_name: str) -> int:
        try:
            parsed = int(value_text)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be a positive integer.") from exc

        if parsed <= 0:
            raise ValueError(f"{field_name} must be a positive integer.")

        return parsed

    def run_analysis(self) -> None:
        self.run_button.state(["disabled"])
        try:
            self._set_progress("Starting analysis...", 0)
            project_root = self._require_project_root()
            analyzer = self._get_analyzer(project_root)
            mode = self.analysis_mode_var.get()
            workers = self._require_positive_int(self.worker_count_var.get().strip(), "Threads")

            if mode == "File include analysis":
                file_path = self._require_file()
                report = analyzer.report_file_include_analysis(
                    file_path,
                    workers=workers,
                    progress_cb=self._set_progress,
                )
            elif mode == "Project totals":
                include_header_ranking = self.include_header_ranking_var.get()
                top_n = 50
                if include_header_ranking:
                    top_n = self._require_positive_int(self.top_n_var.get().strip(), "Top N")

                report = analyzer.report_project_totals(
                    headers_only=self.headers_only_var.get(),
                    include_project_include_sum=self.include_project_include_sum_var.get(),
                    include_project_include_sum_cpp_only=self.include_project_include_sum_cpp_only_var.get(),
                    include_header_ranking=include_header_ranking,
                    top_n=top_n,
                    header_ranking_count_transitive=self.header_ranking_count_transitive_var.get(),
                    header_ranking_sort_by=self.header_ranking_sort_var.get(),
                    workers=workers,
                    progress_cb=self._set_progress,
                )
            elif mode == "Dependents report":
                file_path = self._require_file()
                report = analyzer.report_dependents(
                    file_path,
                    include_breakdown=self.include_breakdown_var.get(),
                    include_dependents_include_sum=self.include_dependents_include_sum_var.get(),
                    include_dependents_include_sum_cpp_only=self.include_dependents_include_sum_cpp_only_var.get(),
                    hide_cpp_in_breakdown=self.hide_cpp_in_breakdown_var.get(),
                    show_detail=self.show_detail_var.get(),
                    workers=workers,
                    progress_cb=self._set_progress,
                )
            else:
                raise ValueError("Unsupported analysis mode selected.")

            self.last_report = report
            self.output_text.configure(state="normal")
            self.output_text.delete("1.0", "end")
            self.output_text.insert("1.0", report)
            self.output_text.configure(state="disabled")
            self._set_progress("Done", 100)

        except Exception as exc:
            error_message = f"{exc}\n\n{traceback.format_exc()}"
            messagebox.showerror("Analysis error", str(exc))
            self.output_text.configure(state="normal")
            self.output_text.delete("1.0", "end")
            self.output_text.insert("1.0", error_message)
            self.output_text.configure(state="disabled")
            self.last_report = ""
            self._set_progress("Failed", 0)
        finally:
            self.run_button.state(["!disabled"])

    def save_report(self) -> None:
        if not self.last_report:
            messagebox.showwarning("No report", "Run an analysis first before saving.")
            return

        default_name = f"include_analysis_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        save_path = filedialog.asksaveasfilename(
            title="Save report",
            defaultextension=".txt",
            initialfile=default_name,
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )

        if not save_path:
            return

        with open(save_path, "w", encoding="utf-8") as file_handle:
            file_handle.write(self.last_report)

        messagebox.showinfo("Saved", f"Report saved to:\n{save_path}")


def main() -> None:
    root = Tk()
    app = IncludeAnalyzerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
