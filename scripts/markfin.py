#!/usr/bin/env python3
"""Markdown Finisher (markfin.py).

Merges individual Markdown chunk files from processing/{article}/ into a single
cohesive Markdown document in output/{article}.md, sorting chunks numerically
by starting page number.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()


def extract_start_page(filename: str) -> int:
    """Extract the starting page number from a chunk filename for numerical sorting.

    Examples:
        '1-2.md' -> 1
        '3-4.md' -> 3
        '15.md'  -> 15
        '11-12.md' -> 11
    """
    match = re.search(r"(\d+)", filename)
    if match:
        return int(match.group(1))
    return float("inf")  # Unparsed filenames go to the end


@dataclass
class MergeResult:
    """Statistics for a merged article."""

    article_name: str
    output_path: Path
    chunks_count: int
    total_lines: int
    total_chars: int
    success: bool
    error_message: Optional[str] = None


def merge_article_chunks(
    article_dir: Path,
    output_dir: Path,
    add_page_comments: bool = False,
    force: bool = False,
) -> MergeResult:
    """Merge all .md chunks within an article directory into a final output markdown file."""
    article_name = article_dir.name
    output_path = output_dir / f"{article_name}.md"

    md_files = [
        f for f in article_dir.glob("*.md")
        if not f.name.startswith(".")
    ]

    if not md_files:
        return MergeResult(
            article_name=article_name,
            output_path=output_path,
            chunks_count=0,
            total_lines=0,
            total_chars=0,
            success=False,
            error_message="No .md chunk files found in article folder.",
        )

    # Sort numerically by starting page
    md_files.sort(key=lambda p: (extract_start_page(p.name), p.name))

    merged_parts: List[str] = []

    for md_file in md_files:
        try:
            content = md_file.read_text(encoding="utf-8").strip()
            if add_page_comments:
                merged_parts.append(f"<!-- Chunk: {md_file.stem} -->\n\n{content}")
            else:
                merged_parts.append(content)
        except Exception as e:
            return MergeResult(
                article_name=article_name,
                output_path=output_path,
                chunks_count=len(md_files),
                total_lines=0,
                total_chars=0,
                success=False,
                error_message=f"Failed reading chunk {md_file.name}: {e}",
            )

    merged_text = "\n\n".join(merged_parts) + "\n"

    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        output_path.write_text(merged_text, encoding="utf-8")
    except Exception as e:
        return MergeResult(
            article_name=article_name,
            output_path=output_path,
            chunks_count=len(md_files),
            total_lines=0,
            total_chars=0,
            success=False,
            error_message=f"Failed writing merged file: {e}",
        )

    lines_count = merged_text.count("\n")
    chars_count = len(merged_text)

    return MergeResult(
        article_name=article_name,
        output_path=output_path,
        chunks_count=len(md_files),
        total_lines=lines_count,
        total_chars=chars_count,
        success=True,
    )


def run_merge(
    processing_dir: Path = Path("processing"),
    output_dir: Path = Path("output"),
    article_filter: Optional[str] = None,
    add_page_comments: bool = False,
    force: bool = False,
) -> List[MergeResult]:
    """Execute merging for all articles or a filtered single article."""
    if not processing_dir.exists():
        console.print(f"[bold red]Error: Processing directory '{processing_dir}' does not exist.[/bold red]")
        sys.exit(1)

    article_dirs = [
        d for d in processing_dir.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    ]

    if article_filter:
        article_dirs = [
            d for d in article_dirs
            if d.name == article_filter or article_filter in d.name
        ]

    if not article_dirs:
        console.print(f"[yellow]No article folders found in '{processing_dir}'.[/yellow]")
        return []

    console.print(
        Panel(
            f"[bold cyan]Markdown Merger (markfin)[/bold cyan]\n"
            f"Processing Directory: [green]{processing_dir}[/green]\n"
            f"Output Directory: [green]{output_dir}[/green]\n"
            f"Found Articles: [green]{len(article_dirs)}[/green]\n"
            f"Page Comments: [green]{add_page_comments}[/green]",
            border_style="cyan",
        )
    )

    results: List[MergeResult] = []
    table = Table(title="Merged Output Summary", show_lines=True)
    table.add_column("Article", style="cyan", no_wrap=False)
    table.add_column("Chunks", justify="center", style="green")
    table.add_column("Lines", justify="center", style="magenta")
    table.add_column("Characters", justify="center", style="white")
    table.add_column("Status", justify="center", style="bold")

    for article_dir in sorted(article_dirs, key=lambda d: d.name):
        res = merge_article_chunks(
            article_dir=article_dir,
            output_dir=output_dir,
            add_page_comments=add_page_comments,
            force=force,
        )
        results.append(res)

        if res.success:
            status_str = "[green]✓ Merged[/green]"
            table.add_row(
                res.article_name,
                str(res.chunks_count),
                f"{res.total_lines:,}",
                f"{res.total_chars:,}",
                status_str,
            )
        else:
            status_str = f"[red]✗ {res.error_message}[/red]"
            table.add_row(res.article_name, str(res.chunks_count), "-", "-", status_str)

    console.print(table)
    successful = sum(1 for r in results if r.success)
    console.print(
        f"[bold green]Merging Completed![/bold green] "
        f"Successfully generated [cyan]{successful}/{len(results)}[/cyan] final Markdown file(s) in [green]{output_dir}/[/green]\n"
    )
    return results


def main() -> None:
    """CLI entry point for markfin."""
    parser = argparse.ArgumentParser(
        description="Merge processed Markdown chunk files into unified documents for RAG."
    )
    parser.add_argument(
        "-p",
        "--processing-dir",
        type=Path,
        default=Path("processing"),
        help="Path to processing directory (default: processing)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("output"),
        help="Path to output directory (default: output)",
    )
    parser.add_argument(
        "-a",
        "--article",
        type=str,
        default=None,
        help="Specific article name to merge (optional)",
    )
    parser.add_argument(
        "--page-comments",
        action="store_true",
        help="Include HTML comments indicating chunk page bounds",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force overwrite existing output files",
    )

    args = parser.parse_args()
    run_merge(
        processing_dir=args.processing_dir,
        output_dir=args.output_dir,
        article_filter=args.article,
        add_page_comments=args.page_comments,
        force=args.force,
    )


if __name__ == "__main__":
    main()
