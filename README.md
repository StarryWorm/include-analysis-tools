# Include Analyzer (GUI)

Single-file C++ include analysis tool with a desktop GUI.

## What this does

`include_analyzer.py` provides these analysis modes:

- **File include analysis**: shows transitive includes for a selected input file, with include paths.
- **Project totals**: shows unique transitive include totals project-wide, with an optional per-file transitive include sum (project "total include cost") and an optional all-header include analysis that reports Top N headers by transitive include reach.
- **Dependents report**: shows how many files include a target file (directly and transitively), with optional breakdown and a default-on option to hide `.cpp` entries in the includer list. This can also optionally add the sum of each includer's own unique transitive includes (equivalent to summing file-include-analysis totals over all dependents).

## Run

From this folder:

```bash
python include_analyzer.py
```

## GUI usage (quick)

1. Set **Project root** to your C++ source root.
2. Choose **Mode**.
3. If mode needs a file, set **Input/Target file**.
4. Click **Run Analysis**.
5. Optional: click **Copy Report** to copy markdown-formatted output to clipboard.
6. Optional: click **Save Report** to export output to `.txt`.

## Notes

- The scanner targets `.cpp` and `.h` files.
- Common build/cache folders are skipped (`.git`, `build`, `bin`, `obj`, etc.).
- Paths under a `thirdparty` directory are ignored for dependency counting reports.
- **Threads** is available for all modes (default `cpu_count - 4`, minimum `1`).
- **Reuse cache between runs** is available for all modes (enabled by default) to speed up repeated analyses in the same project.
- In **Project totals**, the optional header ranking additionally supports:
	- **Add sum of each file's unique transitive includes** (project-wide include "cost" by summing each analyzed file's own unique transitive include count)
	- **Only count `.cpp` files for include sum** (default `on`; limits the above sum to translation units)
	- **Top N** (default `50`)
	- **Count transitive includes** (default `on`; when on, ranks headers by total transitive includers like Dependents mode, and when off ranks by direct includers only)
	- **Sort by**: `total`, `h`, or `cpp`
- In **Dependents report**, the include-sum option additionally supports:
	- **Only count `.cpp` includers for include sum** (default `on`; limits dependent include-cost sums to translation units)
- Header ranking rows show split counts per header: `total`, `.h`, `.cpp`, and `other`.
- All report modes include `.h` / `.cpp` / `other` split totals in their outputs.
- Reports are formatted as Markdown (tables + headings) for easy pasting into GitHub PR descriptions and other markdown-supported tools.

## AI Disclosure

This is completely vibe-coded, but it works so that's that.