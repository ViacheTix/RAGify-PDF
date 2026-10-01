#!/usr/bin/env python3
"""PDF Slicer and Image Extractor.

Slices PDF documents into 2-page chunks for LLM processing and extracts
embedded figures and vector charts into the figures directory using Docling
(with PyMuPDF fallback).
"""

from __future__ import annotations

import argparse
import io
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import fitz  # PyMuPDF
from PIL import Image
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

console = Console()


def sanitize_folder_name(name: str) -> str:
    """Sanitize article name for filesystem compatibility."""
    return "".join(c if c.isalnum() or c in ("-", "_", " ", ".") else "_" for c in name).strip()


def extract_figures_with_docling(
    pdf_path: Path,
    output_dir: Path,
    force: bool = False,
    min_width: int = 80,
    min_height: int = 40,
) -> int:
    """Extract figures, charts, and diagrams using Docling's layout AI model.

    This captures full multi-panel figures and vector charts that standard
    raster extractors miss.
    """
    try:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.datamodel.base_models import InputFormat
        from docling_core.types.doc import PictureItem

        pipeline_options = PdfPipelineOptions()
        pipeline_options.generate_picture_images = True
        pipeline_options.images_scale = 2.0

        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )

        conv_res = converter.convert(pdf_path)
        output_dir.mkdir(parents=True, exist_ok=True)

        extracted_count = 0
        for item, level in conv_res.document.iterate_items():
            if isinstance(item, PictureItem):
                try:
                    img = item.get_image(conv_res.document)
                    if not img:
                        continue

                    # Filter out tiny logos / header noise
                    if img.width < min_width and img.height < min_height:
                        continue

                    extracted_count += 1
                    target_path = output_dir / f"{extracted_count}.png"

                    if target_path.exists() and not force:
                        continue

                    # Ensure standard RGB/RGBA PNG
                    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                        img = img.convert("RGBA")
                    elif img.mode != "RGB":
                        img = img.convert("RGB")

                    img.save(target_path, format="PNG")
                except Exception as e:
                    console.print(f"[yellow]Warning: Could not save Docling picture: {e}[/yellow]")

        return extracted_count

    except Exception as e:
        console.print(f"[yellow]Docling figure extraction failed ({e}), falling back to PyMuPDF...[/yellow]")
        return 0


def extract_images_from_doc(
    doc: fitz.Document,
    output_dir: Path,
    force: bool = False,
) -> int:
    """Extract embedded raster images from a PDF document via PyMuPDF.

    Images are saved as figures/{article_name}/{index}.png.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    extracted_count = 0
    seen_xrefs = set()

    for page_num in range(len(doc)):
        page = doc[page_num]
        image_list = page.get_images(full=True)

        for img_info in image_list:
            xref = img_info[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)

            try:
                base_image = doc.extract_image(xref)
                if not base_image or "image" not in base_image:
                    continue

                image_bytes = base_image["image"]
                extracted_count += 1
                target_path = output_dir / f"{extracted_count}.png"

                if target_path.exists() and not force:
                    continue

                img = Image.open(io.BytesIO(image_bytes))
                if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
                    img = img.convert("RGBA")
                elif img.mode != "RGB":
                    img = img.convert("RGB")

                img.save(target_path, format="PNG")

            except Exception as e:
                console.print(
                    f"[yellow]Warning: Could not extract image xref {xref} from page {page_num + 1}: {e}[/yellow]"
                )

    return extracted_count


def slice_pdf_file(
    pdf_path: Path,
    processing_base_dir: Path,
    figures_base_dir: Path,
    chunk_size: int = 2,
    extract_images: bool = True,
    extractor: str = "docling",
    force: bool = False,
) -> Tuple[int, int]:
    """Slice a single PDF file into chunk_size page chunks and extract images.

    Returns (chunks_created, images_extracted).
    """
    article_name = pdf_path.stem
    article_processing_dir = processing_base_dir / article_name
    article_figures_dir = figures_base_dir / article_name

    article_processing_dir.mkdir(parents=True, exist_ok=True)

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        console.print(f"[bold red]Error opening PDF {pdf_path.name}:[/bold red] {e}")
        return 0, 0

    if doc.is_encrypted:
        console.print(f"[bold yellow]Warning: PDF is encrypted, skipping:[/bold yellow] {pdf_path.name}")
        doc.close()
        return 0, 0

    total_pages = len(doc)
    if total_pages == 0:
        console.print(f"[bold yellow]Warning: PDF has 0 pages:[/bold yellow] {pdf_path.name}")
        doc.close()
        return 0, 0

    chunks_count = 0

    # 1. Slice into chunks
    for start_idx in range(0, total_pages, chunk_size):
        end_idx = min(start_idx + chunk_size, total_pages)
        start_page_num = start_idx + 1
        end_page_num = end_idx

        if start_page_num == end_page_num:
            chunk_filename = f"{start_page_num}.pdf"
        else:
            chunk_filename = f"{start_page_num}-{end_page_num}.pdf"

        chunk_path = article_processing_dir / chunk_filename

        if chunk_path.exists() and not force:
            chunks_count += 1
            continue

        try:
            chunk_doc = fitz.open()
            chunk_doc.insert_pdf(doc, from_page=start_idx, to_page=end_idx - 1)
            chunk_doc.save(chunk_path, garbage=3, deflate=True)
            chunk_doc.close()
            chunks_count += 1
        except Exception as e:
            console.print(f"[red]Error saving chunk {chunk_filename} for {article_name}: {e}[/red]")

    # 2. Extract Figures
    images_count = 0
    if extract_images:
        if force and article_figures_dir.exists():
            shutil.rmtree(article_figures_dir)
            article_figures_dir.mkdir(parents=True, exist_ok=True)

        if extractor in ("docling", "auto"):
            images_count = extract_figures_with_docling(
                pdf_path=pdf_path,
                output_dir=article_figures_dir,
                force=force,
            )

        # Fallback to PyMuPDF if docling returned 0 or wasn't used
        if images_count == 0:
            images_count = extract_images_from_doc(
                doc=doc,
                output_dir=article_figures_dir,
                force=force,
            )

    doc.close()
    return chunks_count, images_count


def process_slicing(
    source_path: Path,
    processing_dir: Path = Path("processing"),
    figures_dir: Path = Path("figures"),
    chunk_size: int = 2,
    extract_images: bool = True,
    extractor: str = "docling",
    force: bool = False,
) -> None:
    """Run slicing pipeline across all source PDFs or a single PDF file."""
    if not source_path.exists():
        console.print(f"[bold red]Error: Source path '{source_path}' does not exist.[/bold red]")
        sys.exit(1)

    pdf_files: List[Path] = []
    if source_path.is_file():
        if source_path.suffix.lower() == ".pdf":
            pdf_files = [source_path]
        else:
            console.print(f"[bold red]Error: File '{source_path}' is not a PDF.[/bold red]")
            sys.exit(1)
    else:
        pdf_files = sorted(
            [p for p in source_path.glob("*.pdf") if not p.name.startswith(".")],
            key=lambda p: p.name.lower(),
        )

    if not pdf_files:
        console.print(f"[yellow]No PDF files found in '{source_path}'.[/yellow]")
        return

    console.print(
        Panel(
            f"[bold cyan]PDF Slicing & Figure Extraction[/bold cyan]\n"
            f"Source: [green]{source_path}[/green] ({len(pdf_files)} PDF file(s))\n"
            f"Processing Directory: [green]{processing_dir}[/green]\n"
            f"Figures Directory: [green]{figures_dir}[/green]\n"
            f"Extractor Engine: [green]{extractor.upper()}[/green]\n"
            f"Chunk Size: [green]{chunk_size} pages[/green]",
            border_style="cyan",
        )
    )

    table = Table(title="Slicing Results", show_lines=True)
    table.add_column("Article", style="cyan", no_wrap=False)
    table.add_column("Chunks", justify="center", style="green")
    table.add_column("Figures", justify="center", style="magenta")
    table.add_column("Status", justify="center", style="bold")

    total_chunks = 0
    total_figures = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Processing PDFs...", total=len(pdf_files))

        for pdf_file in pdf_files:
            progress.update(task, description=f"[cyan]Slicing: {pdf_file.stem[:30]}...")
            chunks, figures = slice_pdf_file(
                pdf_path=pdf_file,
                processing_base_dir=processing_dir,
                figures_base_dir=figures_dir,
                chunk_size=chunk_size,
                extract_images=extract_images,
                extractor=extractor,
                force=force,
            )
            total_chunks += chunks
            total_figures += figures

            status_str = "[green]✓ Success[/green]" if chunks > 0 else "[red]✗ Failed[/red]"
            table.add_row(pdf_file.name, str(chunks), str(figures), status_str)
            progress.advance(task)

    console.print(table)
    console.print(
        f"[bold green]Complete![/bold green] Total chunks: [cyan]{total_chunks}[/cyan], "
        f"Total figures extracted: [magenta]{total_figures}[/magenta]\n"
    )


def main() -> None:
    """CLI entry point for slicer."""
    parser = argparse.ArgumentParser(
        description="Slice PDFs into 2-page chunks and extract figures for multimodal RAG processing."
    )
    parser.add_argument(
        "source",
        nargs="?",
        type=Path,
        default=Path("source_pdfs"),
        help="Path to source PDF directory or a single PDF file (default: source_pdfs)",
    )
    parser.add_argument(
        "-p",
        "--processing-dir",
        type=Path,
        default=Path("processing"),
        help="Target processing directory (default: processing)",
    )
    parser.add_argument(
        "-f",
        "--figures-dir",
        type=Path,
        default=Path("figures"),
        help="Target figures directory (default: figures)",
    )
    parser.add_argument(
        "-c",
        "--chunk-size",
        type=int,
        default=2,
        help="Number of pages per chunk (default: 2)",
    )
    parser.add_argument(
        "--extractor",
        choices=["docling", "pymupdf", "auto"],
        default="docling",
        help="Figure extraction engine (default: docling)",
    )
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="Skip figure extraction",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing chunks and figures",
    )

    args = parser.parse_args()
    process_slicing(
        source_path=args.source,
        processing_dir=args.processing_dir,
        figures_dir=args.figures_dir,
        chunk_size=args.chunk_size,
        extract_images=not args.no_images,
        extractor=args.extractor,
        force=args.force,
    )


if __name__ == "__main__":
    main()
