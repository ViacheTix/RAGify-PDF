#!/usr/bin/env python3
"""Unified Pipeline Orchestrator for PDF-to-Markdown RAG.

Provides subcommands:
  - slice: Slice source PDFs into 2-page chunks and extract figures.
  - process: Process pending chunks via Gemini API / Docling with HIL retry.
  - merge: Assemble final Markdown documents into output/.
  - status: Inspect the current state across all pipeline directories.
  - all: Run slice, process, and merge sequentially.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from scripts.slicer import process_slicing
from scripts.ai_processor import run_ai_processing
from scripts.markfin import run_merge

console = Console()


def show_status(
    source_dir: Path = Path("source_pdfs"),
    processing_dir: Path = Path("processing"),
    figures_dir: Path = Path("figures"),
    output_dir: Path = Path("output"),
) -> None:
    """Display comprehensive status of all documents in the pipeline."""
    source_pdfs = sorted(
        [p for p in source_dir.glob("*.pdf") if not p.name.startswith(".")],
        key=lambda p: p.name.lower(),
    ) if source_dir.exists() else []

    table = Table(title="Pipeline State Overview", show_lines=True)
    table.add_column("Article Name", style="cyan", no_wrap=False)
    table.add_column("Source PDF", justify="center", style="white")
    table.add_column("PDF Chunks", justify="center", style="blue")
    table.add_column("MD Chunks", justify="center", style="green")
    table.add_column("HIL Retries (!)", justify="center", style="yellow")
    table.add_column("Figures", justify="center", style="magenta")
    table.add_column("Final Output", justify="center", style="bold")

    article_names = set(p.stem for p in source_pdfs)
    if processing_dir.exists():
        for d in processing_dir.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                article_names.add(d.name)

    for name in sorted(article_names):
        src_exists = "✓" if (source_dir / f"{name}.pdf").exists() else "-"
        art_proc_dir = processing_dir / name
        art_fig_dir = figures_dir / name
        final_md = output_dir / f"{name}.md"

        pdf_chunks_count = 0
        md_chunks_count = 0
        hil_retries_count = 0
        figures_count = 0

        if art_proc_dir.exists():
            for p in art_proc_dir.glob("*.pdf"):
                pdf_chunks_count += 1
                if p.stem.endswith("!") or p.stem.endswith("!d"):
                    hil_retries_count += 1
            md_chunks_count = len(list(art_proc_dir.glob("*.md")))

        if art_fig_dir.exists():
            figures_count = len(list(art_fig_dir.glob("*.png")))

        if final_md.exists():
            final_status = f"[green]✓ Ready ({final_md.stat().st_size // 1024} KB)[/green]"
        elif pdf_chunks_count > 0 and pdf_chunks_count == md_chunks_count:
            final_status = "[yellow]Ready to merge[/yellow]"
        elif pdf_chunks_count > 0:
            final_status = f"[cyan]Processing ({md_chunks_count}/{pdf_chunks_count})[/cyan]"
        else:
            final_status = "[dim]Not sliced[/dim]"

        hil_str = f"[bold yellow]{hil_retries_count}[/bold yellow]" if hil_retries_count > 0 else "0"

        table.add_row(
            name,
            src_exists,
            str(pdf_chunks_count),
            str(md_chunks_count),
            hil_str,
            str(figures_count),
            final_status,
        )

    console.print(table)


def main() -> None:
    """Main CLI entry point for the pipeline orchestrator."""
    parser = argparse.ArgumentParser(
        description="PDF to Markdown RAG Pipeline Orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", help="Available pipeline commands")

    # Status command
    status_parser = subparsers.add_parser("status", help="Show current state across all stages")
    status_parser.add_argument("-s", "--source-dir", type=Path, default=Path("source_pdfs"))
    status_parser.add_argument("-p", "--processing-dir", type=Path, default=Path("processing"))
    status_parser.add_argument("-f", "--figures-dir", type=Path, default=Path("figures"))
    status_parser.add_argument("-o", "--output-dir", type=Path, default=Path("output"))

    # Slice command
    slice_parser = subparsers.add_parser("slice", help="Slice PDFs into chunks & extract figures")
    slice_parser.add_argument("source", nargs="?", type=Path, default=Path("source_pdfs"))
    slice_parser.add_argument("-p", "--processing-dir", type=Path, default=Path("processing"))
    slice_parser.add_argument("-f", "--figures-dir", type=Path, default=Path("figures"))
    slice_parser.add_argument("-c", "--chunk-size", type=int, default=2)
    slice_parser.add_argument("--no-images", action="store_true")
    slice_parser.add_argument("--force", action="store_true")

    # Process command
    proc_parser = subparsers.add_parser("process", help="Extract Markdown via Gemini API / Docling")
    proc_parser.add_argument("-p", "--processing-dir", type=Path, default=Path("processing"))
    proc_parser.add_argument("--prompt-path", type=Path, default=Path("prompts/processing-prompt.md"))
    proc_parser.add_argument("-a", "--article", type=str, default=None)
    proc_parser.add_argument("-m", "--model", type=str, default="gemini-3.5-flash-lite")
    proc_parser.add_argument("--rpm-limit", type=int, default=15)
    proc_parser.add_argument("--max-retries", type=int, default=3)

    # Merge command
    merge_parser = subparsers.add_parser("merge", help="Merge chunk Markdown files into output/")
    merge_parser.add_argument("-p", "--processing-dir", type=Path, default=Path("processing"))
    merge_parser.add_argument("-o", "--output-dir", type=Path, default=Path("output"))
    merge_parser.add_argument("-a", "--article", type=str, default=None)
    merge_parser.add_argument("--page-comments", action="store_true")
    merge_parser.add_argument("--force", action="store_true")

    # All command
    all_parser = subparsers.add_parser("all", help="Run full pipeline: slice -> process -> merge")
    all_parser.add_argument("-s", "--source", type=Path, default=Path("source_pdfs"))
    all_parser.add_argument("-p", "--processing-dir", type=Path, default=Path("processing"))
    all_parser.add_argument("-f", "--figures-dir", type=Path, default=Path("figures"))
    all_parser.add_argument("-o", "--output-dir", type=Path, default=Path("output"))
    all_parser.add_argument("--prompt-path", type=Path, default=Path("prompts/processing-prompt.md"))
    all_parser.add_argument("-m", "--model", type=str, default="gemini-3.5-flash-lite")
    all_parser.add_argument("--rpm-limit", type=int, default=15)

    args = parser.parse_args()

    if args.command is None or args.command == "status":
        source_dir = getattr(args, "source_dir", Path("source_pdfs"))
        proc_dir = getattr(args, "processing_dir", Path("processing"))
        fig_dir = getattr(args, "figures_dir", Path("figures"))
        out_dir = getattr(args, "output_dir", Path("output"))
        show_status(source_dir, proc_dir, fig_dir, out_dir)

    elif args.command == "slice":
        process_slicing(
            source_path=args.source,
            processing_dir=args.processing_dir,
            figures_dir=args.figures_dir,
            chunk_size=args.chunk_size,
            extract_images=not args.no_images,
            force=args.force,
        )

    elif args.command == "process":
        run_ai_processing(
            processing_dir=args.processing_dir,
            prompt_path=args.prompt_path,
            article_filter=args.article,
            model_name=args.model,
            rpm_limit=args.rpm_limit,
            max_retries=args.max_retries,
        )

    elif args.command == "merge":
        run_merge(
            processing_dir=args.processing_dir,
            output_dir=args.output_dir,
            article_filter=args.article,
            add_page_comments=args.page_comments,
            force=args.force,
        )

    elif args.command == "all":
        console.print(Panel("[bold green]Running Full Pipeline: Slice -> Process -> Merge[/bold green]"))
        process_slicing(
            source_path=args.source,
            processing_dir=args.processing_dir,
            figures_dir=args.figures_dir,
            chunk_size=2,
            extract_images=True,
            force=False,
        )
        run_ai_processing(
            processing_dir=args.processing_dir,
            prompt_path=args.prompt_path,
            article_filter=None,
            model_name=args.model,
            rpm_limit=args.rpm_limit,
        )
        run_merge(
            processing_dir=args.processing_dir,
            output_dir=args.output_dir,
            article_filter=None,
        )


if __name__ == "__main__":
    main()
