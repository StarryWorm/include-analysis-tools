from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
import copy
import os
import re
import threading
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, DoubleVar, IntVar, StringVar, Tk, filedialog, messagebox
from tkinter import ttk
from tkinter.scrolledtext import ScrolledText
from typing import Callable, Dict, List, Optional, Set


@dataclass(slots=True)
class FileIncludeData:
    file_path: Path
    direct_includes: Set[Path] = field(default_factory=set)
    transitive_includes: Set[Path] = field(default_factory=set)

@dataclass(slots=True)
class FileIncludersData:
    file_path: Path
    direct_includers: Set[Path] = field(default_factory=set)
    transitive_includers: Set[Path] = field(default_factory=set)

class IncludeAnalyzer:
    project_root: Path
    progress_cb: Optional[Callable[[str, float], None]]

    _cache_lock: threading.RLock = threading.RLock()
    project_files_cache: Set[Path] = set()
    file_include_data_cache: Dict[Path, FileIncludeData] = {}
    file_includers_data_cache: Dict[Path, FileIncludersData] = {}

    workers: int = 1
    
    class ProgressValues(Enum):
        INIT_START = 0
        INIT_END = 5
        SCANNING = INIT_END
        SCANNED = 15
        RESOLVING_DIRECT_INCLUDES = SCANNED
        RESOLVED_DIRECT_INCLUDES = 30
        RESOLVING_TRANSITIVE_INCLUDES = RESOLVED_DIRECT_INCLUDES
        RESOLVED_TRANSITIVE_INCLUDES = 55
        RESOLVING_INCLUDERS = RESOLVED_TRANSITIVE_INCLUDES
        RESOLVED_INCLUDERS = 75
        COMPUTING_REPORT_VALUES = RESOLVED_INCLUDERS
        COMPUTED_REPORT_VALUES = 90
        FORMATTING_OUTPUT = COMPUTED_REPORT_VALUES
        DONE = 100

    class ProgressMessages(Enum):
        INIT = "Initializing analysis context"
        SCANNING = "Scanning project files"
        RESOLVING_DIRECT_INCLUDES = "Resolving direct includes"
        RESOLVED_DIRECT_INCLUDES = "Direct includes resolved"
        RESOLVING_TRANSITIVE_INCLUDES = "Resolving transitive includes"
        RESOLVED_TRANSITIVE_INCLUDES = "Transitive includes resolved"
        RESOLVING_INCLUDERS = "Resolving includers"
        RESOLVED_INCLUDERS = "Includers resolved"
        COMPUTING_REPORT_VALUES = "Computing report values"
        COMPUTED_REPORT_VALUES = "Report values computed"
        FORMATTING_OUTPUT = "Formatting report output"
        DONE = "Done"

    def __init__(self, project_root: Path, progress_cb: Optional[Callable[[str, float], None]] = None) -> None:
        self.project_root = project_root
        self.progress_cb = progress_cb

    # Helper Methods #

    def clear_runtime_caches(self) -> None:
        with self._cache_lock:
            self.project_files_cache.clear()
            self.file_include_data_cache.clear()

    def _emit_progress(self, message: str, value: float) -> None:
        if self.progress_cb is None:
            return

        self.progress_cb(message, max(0.0, min(100.0, value)))

    @staticmethod
    def recommended_worker_count() -> int:
        return max(1, (os.cpu_count() or 1) - 4)

    def _is_thirdparty(self, path: Path) -> bool:
        try:
            relative_parts = path.relative_to(self.project_root).parts
        except ValueError:
            return True
        return "thirdparty" in relative_parts

    # Analysis Methods #

    def find_project_files(self) -> None:
        extensions = {".cpp", ".h"}
        cpp_files: Set[Path] = set()

        for root, dirs, files in os.walk(self.project_root):
            dirs[:] = [directory for directory in dirs if directory not in {".git", "__pycache__", "build", "bin", "obj", ".vscode", ".idea", ".scu"}]

            for file_name in files:
                if Path(file_name).suffix in extensions:
                    cpp_files.add(Path(root) / file_name)

        self.project_files_cache = {path.resolve() for path in cpp_files}

    def _resolve_include_path(self, include_name: str, current_file: Path) -> Optional[Path]:
        with self._cache_lock:
            project_files = self.project_files_cache

        relative_path = (current_file.parent / include_name).resolve()
        if relative_path in project_files:
            return relative_path

        root_relative_path = (self.project_root / include_name).resolve()
        if root_relative_path in project_files:
            return root_relative_path

        include_filename = Path(include_name).name
        for project_file in project_files:
            if project_file.name == include_filename and str(project_file).endswith(include_name.replace("/", os.sep)):
                return project_file

        return None

    def _resolve_direct_includes_for_file(self, file_path: Path) -> None:
        with self._cache_lock:
            if file_path in self.file_include_data_cache:
                return

        includes: Set[Path] = set()
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as file_handle:
                for line in file_handle:
                    match = re.match(r'^\s*#\s*include\s+[<"]([^>"]+)[>"]', line)
                    if not match:
                        continue
                    resolved = self._resolve_include_path(match.group(1), file_path)
                    if not resolved:
                        continue
                    if resolved == file_path:
                        continue
                    if self._is_thirdparty(resolved):
                        continue
                    includes.add(resolved)
        except OSError:
            pass

        includes.discard(file_path)
        with self._cache_lock:
            self.file_include_data_cache[file_path] = FileIncludeData(file_path, includes)

    def _build_direct_include_cache(self) -> None:
        files = self.project_files_cache

        total_files = len(files)
        progress_span = self.ProgressValues.RESOLVED_DIRECT_INCLUDES.value - self.ProgressValues.RESOLVING_DIRECT_INCLUDES.value

        self._emit_progress(f"{self.ProgressMessages.RESOLVING_DIRECT_INCLUDES.value}...", self.ProgressValues.RESOLVING_DIRECT_INCLUDES.value)

        with ThreadPoolExecutor(max_workers=self.workers) as executor:
            completed = 0
            future_map = {executor.submit(self._resolve_direct_includes_for_file, cpp_file): cpp_file for cpp_file in files}
            for future in as_completed(future_map):
                completed += 1
                progress_value = self.ProgressValues.RESOLVING_DIRECT_INCLUDES.value + progress_span * (completed / total_files)
                self._emit_progress(f"{self.ProgressMessages.RESOLVING_DIRECT_INCLUDES.value}... ({completed}/{total_files})", progress_value)

        self._emit_progress(f"{self.ProgressMessages.RESOLVED_DIRECT_INCLUDES.value}", self.ProgressValues.RESOLVED_DIRECT_INCLUDES.value)

    def _populate_transitive_includes(self, file_path: Path,) -> bool:
        with self._cache_lock:
            file_data = self.file_include_data_cache.get(file_path)

        if file_data is None:
            self._resolve_direct_includes_for_file(file_path)
            with self._cache_lock:
                file_data = self.file_include_data_cache[file_path]

        direct = file_data.direct_includes
        previous_transitive = set(file_data.transitive_includes)
        accumulated: Set[Path] = set()

        for child in direct:
            with self._cache_lock:
                child_data = self.file_include_data_cache.get(child)
                if child_data is None:
                    continue
                child_direct = child_data.direct_includes
                child_transitive = child_data.transitive_includes

            accumulated.update(child_direct)
            accumulated.update(child_transitive)

        accumulated.difference_update(direct)
        accumulated.discard(file_path)

        if accumulated == previous_transitive:
            return False

        file_data.transitive_includes = accumulated
        with self._cache_lock:
            self.file_include_data_cache[file_path] = file_data
        return True

    def _prepare_file_data_map(self) -> None:
        with self._cache_lock:
            files = list(self.file_include_data_cache.keys())

        total_files = max(1, len(files))
        progress_span = self.ProgressValues.RESOLVED_TRANSITIVE_INCLUDES.value - self.ProgressValues.RESOLVING_TRANSITIVE_INCLUDES.value
        max_passes = 15

        self._emit_progress(self.ProgressMessages.RESOLVING_TRANSITIVE_INCLUDES.value, self.ProgressValues.RESOLVING_TRANSITIVE_INCLUDES.value)

        for pass_index in range(max_passes):
            changed = False
            with ThreadPoolExecutor(max_workers=max(1, self.workers)) as executor:
                future_map = {executor.submit(self._populate_transitive_includes, file_path): file_path for file_path in files}
                completed = 0
                for future in as_completed(future_map):
                    completed += 1
                    if future.result():
                        changed = True

                    pass_progress = (pass_index + (completed / total_files)) / max_passes
                    progress_value = self.ProgressValues.RESOLVING_TRANSITIVE_INCLUDES.value + progress_span * pass_progress
                    self._emit_progress(f"Computing transitive includes... (pass {pass_index + 1}, {completed}/{total_files})", progress_value)

            if not changed:
                break
        
        self._emit_progress(self.ProgressMessages.RESOLVED_TRANSITIVE_INCLUDES.value, self.ProgressValues.RESOLVED_TRANSITIVE_INCLUDES.value)

    def _prepare_includers_data_map(self) -> None:
        self._emit_progress(self.ProgressMessages.RESOLVING_INCLUDERS.value, self.ProgressValues.RESOLVING_INCLUDERS.value)

        file_data_map = self.file_include_data_cache
        
        progress_span : int = self.ProgressValues.RESOLVED_INCLUDERS.value - self.ProgressValues.RESOLVING_INCLUDERS.value
        processed : int = 0
        total_files : int = len(file_data_map)

        includers_data: Dict[Path, FileIncludersData] = {}
        for file_path in file_data_map:
            for included in file_data_map[file_path].direct_includes:
                includers_data.setdefault(included, FileIncludersData(included)).direct_includers.add(file_path)
            for included in file_data_map[file_path].transitive_includes:
                includers_data.setdefault(included, FileIncludersData(included)).transitive_includers.add(file_path)
            processed += 1
            self._emit_progress(f"Resolving includers... ({processed}/{total_files})", self.ProgressValues.RESOLVING_INCLUDERS.value + progress_span * (processed / total_files))

        self.file_includers_data_cache = includers_data

        self._emit_progress(self.ProgressMessages.RESOLVED_INCLUDERS.value, self.ProgressValues.RESOLVED_INCLUDERS.value)

    def build_project_file_include_data(self) -> None:
        if self.file_include_data_cache:
            return

        self.find_project_files()
        if len(self.project_files_cache) == 0:
            raise ValueError("No C++ source files found in the project root")
        
        self._build_direct_include_cache()
        self._prepare_file_data_map()
        self._prepare_includers_data_map()

    # Reporting methods #

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.project_root))
        except ValueError:
            return str(path)

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

    def report_file_include_analysis(self, input_file: Path) -> str:
        self._emit_progress(self.ProgressMessages.COMPUTING_REPORT_VALUES.value, self.ProgressValues.COMPUTING_REPORT_VALUES.value)

        start_file = input_file.resolve()
        if not start_file.exists():
            raise FileNotFoundError(f"Input file not found: {start_file}")

        with self._cache_lock:
            file_data_map = copy.deepcopy(self.file_include_data_cache)

        start_data = file_data_map.get(start_file)
        if start_data is None:
            raise ValueError(f"Input file is not part of the indexed project: {start_file}")

        all_includes: Set[Path] = start_data.direct_includes | start_data.transitive_includes
        include_paths: Dict[Path, List[List[Path]]] = {path: [] for path in all_includes}
        to_visit: List[tuple[Path, List[Path]]] = [(start_file, [start_file])]
        visited_for_progress: Set[Path] = set()
        visit_budget = len(file_data_map) + 1
        bfs_progress_start = self.ProgressValues.COMPUTING_REPORT_VALUES.value
        bfs_progress_span = self.ProgressValues.COMPUTED_REPORT_VALUES.value - self.ProgressValues.COMPUTING_REPORT_VALUES.value

        while to_visit:
            current_file, current_chain = to_visit.pop()
            if current_file not in visited_for_progress:
                visited_for_progress.add(current_file)
                bfs_progress = bfs_progress_start + bfs_progress_span * (min(len(visited_for_progress), visit_budget) / visit_budget)
                self._emit_progress(f"Resolving include graph... ({len(visited_for_progress)} visited)", bfs_progress)

            for resolved_path in file_data_map.get(current_file, FileIncludeData(current_file)).direct_includes:
                if resolved_path == start_file or resolved_path not in all_includes:
                    continue

                new_chain = current_chain + [resolved_path]
                if new_chain not in include_paths[resolved_path]:
                    include_paths[resolved_path].append(new_chain)

                if resolved_path in current_chain:
                    continue
                to_visit.append((resolved_path, new_chain))

        self._emit_progress(self.ProgressMessages.FORMATTING_OUTPUT.value, self.ProgressValues.FORMATTING_OUTPUT.value)
        included_split = self._split_cpp_h_counts(all_includes)

        lines: List[str] = [
            "# C++ Include Dependency Analysis",
            "",
            "## Summary",
        ]

        summary_rows = [
            ["Input File", str(start_file)],
            ["Project Root", str(self.project_root.parts[-1])],
            ["Analysis Date", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
            ["Total Included Files", str(len(all_includes))],
            ["Included split (.h / .cpp / other)", f"{included_split['.h']} / {included_split['.cpp']} / {included_split['other']}"]
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

        self._emit_progress(self.ProgressMessages.DONE.value, self.ProgressValues.DONE.value)
        return "\n".join(lines)

    def report_project_totals(
        self,
        include_project_include_sum: bool = False,
        include_sum_cpp_only: bool = True,
        include_header_ranking: bool = False,
        top_n: int = 50,
        header_ranking_count_transitive: bool = True,
        header_ranking_sort_by: str = "total",
    ) -> str:
        self._emit_progress(self.ProgressMessages.COMPUTING_REPORT_VALUES.value, self.ProgressValues.COMPUTING_REPORT_VALUES.value)

        with self._cache_lock:
            file_data = copy.deepcopy(self.file_include_data_cache)

        project_wide_unique: Set[Path] = set()
        for data in file_data.values():
            project_wide_unique.update(data.transitive_includes)

        project_split = self._split_cpp_h_counts(project_wide_unique)

        total_direct_includes = 0
        direct_include_splits = {".h": 0, ".cpp": 0, "other": 0}
        total_transitive_includes = 0
        transitive_include_splits = {".h": 0, ".cpp": 0, "other": 0}
        sum_source_files = {}
        if include_project_include_sum:
            sum_source_files = [file_path for file_path in file_data if file_path.suffix.lower() == ".cpp"] if include_sum_cpp_only else list(file_data.keys())
            for file_path in sum_source_files:
                total_direct_includes += len(file_data[file_path].direct_includes)
                split = self._split_cpp_h_counts(file_data[file_path].direct_includes)
                direct_include_splits[".h"] += split[".h"]
                direct_include_splits[".cpp"] += split[".cpp"]
                direct_include_splits["other"] += split["other"]

                total_transitive_includes += len(file_data[file_path].transitive_includes)
                split = self._split_cpp_h_counts(file_data[file_path].transitive_includes)
                transitive_include_splits[".h"] += split[".h"]
                transitive_include_splits[".cpp"] += split[".cpp"]
                transitive_include_splits["other"] += split["other"]

        header_top, header_count = ([], 0)
        if include_header_ranking:
            header_top, header_count = self.analyze_all_headers_include_totals(top_n=top_n, count_transitive=header_ranking_count_transitive, sort_by=header_ranking_sort_by)

        self._emit_progress(self.ProgressMessages.FORMATTING_OUTPUT.value, self.ProgressValues.FORMATTING_OUTPUT.value)

        lines = [
            "# Project Include Totals",
            "",
            "## Summary",
        ]

        summary_rows = [
            ["Project root", str(self.project_root.parts[-1])],
            ["Files analyzed", str(len(file_data))],
            ["Analysis Date", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
            ["Transitive includes (unique project-wide)", str(len(project_wide_unique))],
            ["Transitive split (.h / .cpp / other)", f"{project_split['.h']} / {project_split['.cpp']} / {project_split['other']}"]
        ]

        if include_project_include_sum:
            summary_rows.append(["Files counted in include sum", (f"{len(sum_source_files)} (.cpp only)" if include_sum_cpp_only else f"{len(sum_source_files)} (all files)")])
            summary_rows.append(["Sum of each files unique includes: ", str(total_direct_includes + total_transitive_includes)])
            summary_rows.append(["Sum of each file's unique direct includes", str(total_direct_includes)])
            summary_rows.append(["Direct includes sum split (.h / .cpp / other)", f"{direct_include_splits['.h']} / {direct_include_splits['.cpp']} / {direct_include_splits['other']}"]) 
            summary_rows.append(["Sum of each file's unique transitive includes", str(total_transitive_includes)])
            summary_rows.append(["Transitive includes sum split (.h / .cpp / other)", f"{transitive_include_splits['.h']} / {transitive_include_splits['.cpp']} / {transitive_include_splits['other']}"])

        lines.extend(self._format_markdown_table(["Metric", "Value"], summary_rows))
        lines.append("")

        if include_header_ranking:
            lines.append("## Header Include Analysis (Top N)")
            meta_rows = [
                ["Headers analyzed", str(header_count)],
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

        self._emit_progress(self.ProgressMessages.DONE.value, self.ProgressValues.DONE.value)
        return "\n".join(lines)

    def analyze_all_headers_include_totals(
        self,
        top_n: int = 50,
        count_transitive: bool = True,
        sort_by: str = "total"
    ) -> tuple[List[tuple[Path, int, int, int, int]], int]:
        file_includers_map = copy.deepcopy(self.file_includers_data_cache)
        headers = sorted(path for path in file_includers_map if path.suffix.lower() == ".h")
        header_count = len(headers)

        sort_by = sort_by.lower().strip()
        if sort_by not in {"total", "h", "cpp"}:
            sort_by = "total"

        results: List[tuple[Path, int, int, int, int]] = []
        for header in headers:
            if header not in file_includers_map:
                results.append((header, 0, 0, 0, 0))
                continue
            header_data = file_includers_map[header]
            includers = header_data.transitive_includers if count_transitive else header_data.direct_includers
            split = self._split_cpp_h_counts(includers)
            results.append((header, len(includers), split[".h"], split[".cpp"], split["other"]))

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
        return results[:top_n], header_count

    def report_dependents(
        self,
        target_file: Path,
        include_breakdown: bool = False,
        include_dependents_include_sum: bool = False,
        include_sum_cpp_only: bool = True,
        hide_cpp_in_breakdown: bool = True
    ) -> str:
        self._emit_progress(self.ProgressMessages.COMPUTING_REPORT_VALUES.value, self.ProgressValues.COMPUTING_REPORT_VALUES.value)

        file_data = copy.deepcopy(self.file_include_data_cache)
        file_includers_data = self.file_includers_data_cache[target_file]

        direct_includers = file_includers_data.direct_includers
        transitive_includers = file_includers_data.transitive_includers
        including_files = direct_includers | transitive_includers

        breakdown: Counter[Path] = Counter()

        for includer in direct_includers:
            breakdown[includer] = 0
            includer_data = self.file_includers_data_cache.get(includer)
            if includer_data is None:
                continue
            breakdown[includer] += len(includer_data.direct_includers | includer_data.transitive_includers)
        
        dependents_unique_include_sum = 0
        dependents_split_sum = {".h": 0, ".cpp": 0, "other": 0}
        sum_dependents = set()
        if include_dependents_include_sum:            
            sum_dependents = {path for path in including_files if path.suffix.lower() == ".cpp"} if include_sum_cpp_only else set(including_files)
            for dependent in sum_dependents:
                includes_for_dependent = file_data[dependent].transitive_includes
                dependents_unique_include_sum += len(includes_for_dependent)
                split = self._split_cpp_h_counts(includes_for_dependent)
                dependents_split_sum[".h"] += split[".h"]
                dependents_split_sum[".cpp"] += split[".cpp"]
                dependents_split_sum["other"] += split["other"]

        self._emit_progress(self.ProgressMessages.FORMATTING_OUTPUT.value, self.ProgressValues.FORMATTING_OUTPUT.value)

        lines = [
            "# Include Dependents Report",
            "",
            "## Summary",
        ]

        summary_rows = [
            ["Project root", str(self.project_root.parts[-1])],
            ["Target file", self._display_path(target_file)],
            ["Analysis Date", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
            ["Direct includers", str(len(direct_includers))],
            ["Transitive includers", str(len(transitive_includers))],
            ["Total includers (direct + transitive)", str(len(including_files))]
        ]

        if include_dependents_include_sum:
            summary_rows.append(["Sum of each includer's unique includes", str(dependents_unique_include_sum)])
            summary_rows.append(["Includers counted in include sum",(f"{len(sum_dependents)} (.cpp only)" if include_sum_cpp_only else f"{len(sum_dependents)} (all includers)")])
            summary_rows.append(["Dependent include sum split (.h / .cpp / other)", f"{dependents_split_sum['.h']} / {dependents_split_sum['.cpp']} / {dependents_split_sum['other']}"])

        lines.extend(self._format_markdown_table(["Metric", "Value"], summary_rows))
        lines.append("")

        direct_split = self._split_cpp_h_counts(direct_includers)
        transitive_split = self._split_cpp_h_counts(transitive_includers)
        total_split = self._split_cpp_h_counts(including_files)

        lines.append("## Split Totals (.h / .cpp / other)")
        split_rows = [
            ["Direct includers", str(direct_split[".h"]), str(direct_split[".cpp"]), str(direct_split["other"])],
            ["Transitive includers", str(transitive_split[".h"]), str(transitive_split[".cpp"]), str(transitive_split["other"])],
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
                if count < 1:
                    continue
                breakdown_rows.append([self._display_path(path), str(count)])
                printed = True

            if not printed:
                lines.append("No direct includers discovered; nothing to list.")
            else:
                lines.extend(self._format_markdown_table(["Direct Includer", "Files Through Gateway (excludes self)"], breakdown_rows))
            lines.append("")

        self._emit_progress(self.ProgressMessages.DONE.value, self.ProgressValues.DONE.value)
        return "\n".join(lines)


class IncludeAnalyzerGUI:
    root: Tk
    
    class AnalysisModes(Enum):
        FILE_INCLUDE_ANALYSIS = "File include analysis"
        PROJECT_TOTALS = "Project totals"
        DEPENDENTS_REPORT = "Dependents report"

    project_root_var: StringVar
    file_var: StringVar
    analysis_mode_var: StringVar
    worker_count_var: IntVar
    include_project_include_sum_var: BooleanVar
    include_project_include_sum_cpp_only_var: BooleanVar
    include_header_ranking_var: BooleanVar
    header_ranking_count_transitive_var: BooleanVar
    header_ranking_sort_str: StringVar
    top_n_var: IntVar
    include_breakdown_var: BooleanVar
    include_dependents_include_sum_var: BooleanVar
    include_dependents_include_sum_cpp_only_var: BooleanVar
    hide_cpp_in_breakdown_var: BooleanVar
    progress_value_var: DoubleVar
    progress_status_var: StringVar
    reports: List[str]
    report_index: int
    analyzer_instances: Dict[Path, IncludeAnalyzer]

    def __init__(self):
        self.root = Tk()
        self.root.title("Include Analyzer")
        self.root.geometry("1000x700")

        self.project_root_var = StringVar()
        self.file_var = StringVar()
        self.analysis_mode_var = StringVar(value=self.AnalysisModes.FILE_INCLUDE_ANALYSIS.value)
        self.worker_count_var = IntVar(value=IncludeAnalyzer.recommended_worker_count())
        self.include_project_include_sum_var = BooleanVar(value=False)
        self.include_project_include_sum_cpp_only_var = BooleanVar(value=True)
        self.include_header_ranking_var = BooleanVar(value=False)
        self.header_ranking_count_transitive_var = BooleanVar(value=True)
        self.header_ranking_sort_str = StringVar(value="total")
        self.top_n_var = IntVar(value=50)
        self.include_breakdown_var = BooleanVar(value=False)
        self.include_dependents_include_sum_var = BooleanVar(value=False)
        self.include_dependents_include_sum_cpp_only_var = BooleanVar(value=True)
        self.hide_cpp_in_breakdown_var = BooleanVar(value=True)
        self.progress_value_var = DoubleVar(value=0.0)
        self.progress_status_var = StringVar(value="Ready")
        self.reports = []
        self.report_index = 0
        self.analyzer_instances = {}

        self._build_ui()
        self.root.mainloop()

    def _build_ui(self) -> None:
        main_frame = ttk.Frame(self.root, padding=12)
        main_frame.pack(fill="both", expand=True)

        mode_frame = ttk.LabelFrame(main_frame, text="Analysis", padding=10)
        mode_frame.pack(fill="x")

        ttk.Label(mode_frame, text="Mode:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        mode_combo = ttk.Combobox(
            mode_frame,
            textvariable=self.analysis_mode_var,
            values=[mode.value for mode in self.AnalysisModes],
            state="readonly",
            width=30,
        )
        mode_combo.grid(row=0, column=1, sticky="w", pady=4)
        mode_combo.bind("<<ComboboxSelected>>", self._on_mode_change)

        self.options_frame = ttk.LabelFrame(main_frame, text="Options", padding=10)
        self.options_frame.pack(fill="x", pady=(10, 0))

        self.worker_count_label = ttk.Label(self.options_frame, text="Threads:")
        self.worker_count_entry = ttk.Entry(self.options_frame, textvariable=self.worker_count_var, width=10)

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
            textvariable=self.header_ranking_sort_str,
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

        self.paths_frame = ttk.LabelFrame(main_frame, text="Paths", padding=10)
        self.paths_frame.pack(fill="x", pady=(10, 0))

        self.project_root_label = ttk.Label(self.paths_frame, text="Project root:")
        self.project_root_entry = ttk.Entry(self.paths_frame, textvariable=self.project_root_var)
        self.project_root_button = ttk.Button(self.paths_frame, text="Browse...", command=self._browse_project)

        self.file_label = ttk.Label(self.paths_frame, text="Input file:")
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
        self.next_report_button = ttk.Button(button_frame, text=">", command=self.show_next_report)
        self.next_report_button.pack(side="right")
        self.prev_report_button = ttk.Button(button_frame, text="<", command=self.show_previous_report)
        self.prev_report_button.pack(side="right", padx=(0, 8))

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
        self._update_report_nav_state()

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
            self.include_project_include_sum_var.set(False)
            self.include_project_include_sum_cpp_only_var.set(True)
            self.include_header_ranking_var.set(False)
            self.header_ranking_count_transitive_var.set(True)
            self.header_ranking_sort_str.set("total")

        if mode == "Dependents report":
            self.include_breakdown_check.grid(row=1, column=0, sticky="w", pady=4)
            self.include_dependents_include_sum_check.grid(row=2, column=0, sticky="w", pady=4)
            if self.include_dependents_include_sum_var.get():
                self.include_dependents_include_sum_cpp_only_check.grid(row=3, column=0, sticky="w", pady=4)
            self.hide_cpp_in_breakdown_check.grid(row=4, column=0, sticky="w", pady=4)
        else:
            self.include_breakdown_var.set(False)
            self.include_dependents_include_sum_var.set(False)
            self.include_dependents_include_sum_cpp_only_var.set(True)
            self.hide_cpp_in_breakdown_var.set(True)

        for child in self.paths_frame.winfo_children():
            grid_forget = getattr(child, "grid_forget", None)
            if callable(grid_forget):
                grid_forget()

        self.project_root_label.grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        self.project_root_entry.grid(row=0, column=1, sticky="ew", pady=4)
        self.project_root_button.grid(row=0, column=2, padx=(6, 0), pady=4)

        if mode in {self.AnalysisModes.FILE_INCLUDE_ANALYSIS.value, self.AnalysisModes.DEPENDENTS_REPORT.value}:
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
            analyzer = IncludeAnalyzer(project_root, self._set_progress)
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
        self.reports.clear()
        self.output_text.delete("1.0", "end")
        self.output_text.configure(state="disabled")

    def _show_report(self, index: int) -> None:
        if not self.reports:
            return

        clamped_index = max(0, min(index, len(self.reports) - 1))
        self.report_index = clamped_index

        self.output_text.configure(state="normal")
        self.output_text.delete("1.0", "end")
        self.output_text.insert("1.0", self.reports[self.report_index])
        self.output_text.configure(state="disabled")
        self._update_report_nav_state()

    def _update_report_nav_state(self) -> None:
        has_reports = len(self.reports) > 0

        if not has_reports:
            self.prev_report_button.state(["disabled"])
            self.next_report_button.state(["disabled"])
            self.copy_report_button.state(["disabled"])
            return

        self.copy_report_button.state(["!disabled"])
        if self.report_index <= 0:
            self.prev_report_button.state(["disabled"])
        else:
            self.prev_report_button.state(["!disabled"])

        if self.report_index >= len(self.reports) - 1:
            self.next_report_button.state(["disabled"])
        else:
            self.next_report_button.state(["!disabled"])

    def show_previous_report(self) -> None:
        if not self.reports:
            return
        self._show_report(self.report_index - 1)

    def show_next_report(self) -> None:
        if not self.reports:
            return
        self._show_report(self.report_index + 1)

    def copy_report(self) -> None:
        if not self.reports:
            messagebox.showwarning("No report", "Run an analysis first before copying.")
            return

        report_text = self.reports[self.report_index].strip()
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

    def run_analysis(self) -> None:
        self.run_button.state(["disabled"])
        analyzer: Optional[IncludeAnalyzer] = None
        try:
            self._set_progress(IncludeAnalyzer.ProgressMessages.INIT.value, IncludeAnalyzer.ProgressValues.INIT_START.value)
            analyzer = self._get_analyzer(self._require_project_root())
            analyzer.workers = max(1, self.worker_count_var.get())
            analyzer.build_project_file_include_data()

            match self.analysis_mode_var.get():
                case self.AnalysisModes.FILE_INCLUDE_ANALYSIS.value:
                    file_path = self._require_file()
                    report = analyzer.report_file_include_analysis(file_path)

                case self.AnalysisModes.PROJECT_TOTALS.value:
                    report = analyzer.report_project_totals(
                        include_project_include_sum=self.include_project_include_sum_var.get(),
                        include_sum_cpp_only=self.include_project_include_sum_cpp_only_var.get(),
                        include_header_ranking=self.include_header_ranking_var.get(),
                        top_n=min(self.top_n_var.get(), 0),
                        header_ranking_count_transitive=self.header_ranking_count_transitive_var.get(),
                        header_ranking_sort_by=self.header_ranking_sort_str.get()
                    )

                case self.AnalysisModes.DEPENDENTS_REPORT.value:
                    file_path = self._require_file()
                    report = analyzer.report_dependents(
                        file_path,
                        include_breakdown=self.include_breakdown_var.get(),
                        include_dependents_include_sum=self.include_dependents_include_sum_var.get(),
                        include_sum_cpp_only=self.include_dependents_include_sum_cpp_only_var.get(),
                        hide_cpp_in_breakdown=self.hide_cpp_in_breakdown_var.get()
                    )

                case _:
                    raise ValueError("Unsupported analysis mode selected.")

            self.reports.append(report)
            self._show_report(len(self.reports) - 1)
            self._set_progress("Done", 100)

        except Exception as exc:
            error_message = f"{exc}\n\n{traceback.format_exc()}"
            messagebox.showerror("Analysis error", str(exc))
            self.output_text.configure(state="normal")
            self.output_text.delete("1.0", "end")
            self.output_text.insert("1.0", error_message)
            self.output_text.configure(state="disabled")
            self._set_progress("Failed", 0)
            self._update_report_nav_state()
        finally:
            self.run_button.state(["!disabled"])

    def save_report(self) -> None:
        if len(self.reports) == 0:
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
            file_handle.write(self.reports[self.report_index])

        messagebox.showinfo("Saved", f"Report saved to:\n{save_path}")


def main() -> None:
    app = IncludeAnalyzerGUI()


if __name__ == "__main__":
    main()
