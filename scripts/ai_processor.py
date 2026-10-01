#!/usr/bin/env python3
"""AI Processor for PDF to Markdown Pipeline.

Processes 2-page PDF chunks using Google Gemini API (gemini-3.5-flash-lite)
with API key rotation (Round-Robin), 15 RPM rate limiting, and Human-in-the-Loop
(HIL) retry mechanisms (! suffix) or Docling fallback (!d suffix).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import APIError
from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn
from rich.table import Table

console = Console()


@dataclass
class ChunkTask:
    """Represents a PDF chunk to be processed."""

    article_name: str
    pdf_path: Path
    output_md_path: Path
    is_force_retry: bool
    is_docling_fallback: bool
    clean_pdf_path: Path


class APIKeyManager:
    """Manages API keys with Round-Robin rotation and strict RPM rate limiting."""

    def __init__(self, rpm_limit: int = 15, env_file: Optional[Path] = None) -> None:
        if env_file and env_file.exists():
            load_dotenv(dotenv_path=env_file)
        else:
            load_dotenv()

        raw_keys = os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY") or ""
        self.keys: List[str] = [
            k.strip()
            for k in raw_keys.split(",")
            if k.strip()
        ]

        if not self.keys:
            console.print(
                "[bold red]Error: No Gemini API keys found in GEMINI_API_KEYS or GEMINI_API_KEY environment variables / .env file.[/bold red]"
            )
            sys.exit(1)

        self.rpm_limit = rpm_limit
        self.clients: Dict[str, genai.Client] = {
            k: genai.Client(api_key=k) for k in self.keys
        }
        self.request_history: Dict[str, Deque[float]] = {
            k: deque() for k in self.keys
        }
        self.current_index = 0
        self.cooldown_until: Dict[str, float] = {k: 0.0 for k in self.keys}

    @property
    def key_count(self) -> int:
        return len(self.keys)

    def _cleanup_history(self, key: str, now: float) -> None:
        """Remove timestamps older than 60 seconds from the history deque."""
        history = self.request_history[key]
        while history and (now - history[0]) >= 60.0:
            history.popleft()

    def get_key_alias(self, key: str) -> str:
        """Return a masked alias for logging."""
        if len(key) <= 8:
            return f"Key-{self.keys.index(key) + 1} (***)"
        return f"Key-{self.keys.index(key) + 1} (...{key[-4:]})"

    def mark_rate_limited(self, key: str, cooldown_seconds: float = 60.0) -> None:
        """Mark a key as temporarily rate-limited upon receiving 429."""
        now = time.time()
        self.cooldown_until[key] = now + cooldown_seconds
        console.print(
            f"[yellow]Warning: {self.get_key_alias(key)} received 429 rate limit. Cooling down for {cooldown_seconds:.0f}s.[/yellow]"
        )

    def get_next_client(self) -> Tuple[genai.Client, str]:
        """Obtain the next available Gemini Client adhering to the 15 RPM limit.

        Uses round-robin rotation. If all keys are at capacity, sleeps until the
        earliest key's quota window resets.
        """
        while True:
            now = time.time()
            best_wait_time = float("inf")
            available_key: Optional[str] = None

            # Try finding an available key starting from current_index
            for i in range(len(self.keys)):
                idx = (self.current_index + i) % len(self.keys)
                key = self.keys[idx]

                # Check cooldown
                if now < self.cooldown_until[key]:
                    wait_for_cooldown = self.cooldown_until[key] - now
                    if wait_for_cooldown < best_wait_time:
                        best_wait_time = wait_for_cooldown
                    continue

                self._cleanup_history(key, now)
                history = self.request_history[key]

                if len(history) < self.rpm_limit:
                    self.current_index = (idx + 1) % len(self.keys)
                    available_key = key
                    break
                else:
                    # Calculate wait time for this key's oldest request to roll off
                    earliest = history[0]
                    wait_for_key = max(0.0, 60.0 - (now - earliest) + 0.1)
                    if wait_for_key < best_wait_time:
                        best_wait_time = wait_for_key

            if available_key is not None:
                self.request_history[available_key].append(time.time())
                return self.clients[available_key], available_key

            # All keys are currently busy or cooling down
            sleep_duration = min(max(best_wait_time, 0.5), 60.0)
            console.print(
                f"[cyan]Rate limit reached across all {len(self.keys)} key(s). Pausing for {sleep_duration:.1f}s...[/cyan]"
            )
            time.sleep(sleep_duration)


def read_prompt(prompt_path: Path) -> str:
    """Read the system prompt from prompts directory."""
    if not prompt_path.exists():
        console.print(f"[bold red]Error: System prompt file not found at '{prompt_path}'.[/bold red]")
        sys.exit(1)
    return prompt_path.read_text(encoding="utf-8")


def discover_tasks(
    processing_dir: Path,
    article_filter: Optional[str] = None,
) -> List[ChunkTask]:
    """Scan processing directory and build queue of tasks based on HIL state."""
    tasks: List[ChunkTask] = []
    if not processing_dir.exists():
        return tasks

    article_dirs = [d for d in processing_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]

    if article_filter:
        article_dirs = [d for d in article_dirs if d.name == article_filter or article_filter in d.name]

    for article_dir in sorted(article_dirs, key=lambda d: d.name):
        article_name = article_dir.name
        pdf_files = sorted(article_dir.glob("*.pdf"), key=lambda p: p.name)

        for pdf_file in pdf_files:
            filename = pdf_file.name
            stem = pdf_file.stem

            is_docling = stem.endswith("!d")
            is_force = stem.endswith("!") and not is_docling

            # Clean stem removes any trailing '!' or '!d'
            clean_stem = re.sub(r"!d?$", "", stem)
            clean_pdf_path = article_dir / f"{clean_stem}.pdf"
            output_md_path = article_dir / f"{clean_stem}.md"

            if is_force or is_docling:
                # Always reprocess forced or docling-flagged files
                tasks.append(
                    ChunkTask(
                        article_name=article_name,
                        pdf_path=pdf_file,
                        output_md_path=output_md_path,
                        is_force_retry=is_force,
                        is_docling_fallback=is_docling,
                        clean_pdf_path=clean_pdf_path,
                    )
                )
            else:
                # Normal chunk: only process if output .md does not exist
                if not output_md_path.exists():
                    tasks.append(
                        ChunkTask(
                            article_name=article_name,
                            pdf_path=pdf_file,
                            output_md_path=output_md_path,
                            is_force_retry=False,
                            is_docling_fallback=False,
                            clean_pdf_path=clean_pdf_path,
                        )
                    )

    return tasks


def process_with_docling(pdf_path: Path) -> str:
    """Fallback processor using docling for complex structural parsing."""
    try:
        from docling.document_converter import DocumentConverter

        converter = DocumentConverter()
        result = converter.convert(pdf_path)
        md_text = result.document.export_to_markdown()
        return md_text if md_text else "[ERROR: Docling produced empty markdown]"
    except Exception as e:
        console.print(f"[bold red]Docling processing error for {pdf_path.name}:[/bold red] {e}")
        return f"[ERROR: Docling conversion failed: {e}]"


def process_with_gemini(
    task: ChunkTask,
    system_prompt: str,
    key_manager: APIKeyManager,
    model_name: str = "gemini-3.5-flash-lite",
    max_retries: int = 3,
) -> Tuple[bool, str]:
    """Send PDF chunk to Gemini API with retry logic and key rotation."""
    pdf_bytes = task.pdf_path.read_bytes()
    pdf_part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")

    user_instruction = (
        "Convert the provided PDF page(s) into pristine Markdown following the system prompt rules. "
        "Output ONLY the markdown."
    )

    contents = [pdf_part, user_instruction]
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=0.1,
    )

    attempt = 0
    while attempt < max_retries:
        attempt += 1
        client, key = key_manager.get_next_client()
        key_alias = key_manager.get_key_alias(key)

        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )

            response_text = response.text or ""
            if not response_text.strip():
                response_text = "[ERROR: LLM returned empty response. Needs HIL retry]"
                console.print(
                    f"[yellow]Warning: Empty response from model for {task.pdf_path.name}.[/yellow]"
                )

            return True, response_text

        except APIError as e:
            error_code = getattr(e, "code", None)
            error_message = str(e)

            if error_code == 429 or "RESOURCE_EXHAUSTED" in error_message or "429" in error_message:
                key_manager.mark_rate_limited(key, cooldown_seconds=60.0)
                console.print(
                    f"[yellow]Quota limit on {key_alias} for {task.pdf_path.name}. Switching key...[/yellow]"
                )
                continue  # Try next key immediately
            elif error_code in (500, 503, 504) or "UNAVAILABLE" in error_message:
                backoff = 2.0 ** attempt
                console.print(
                    f"[yellow]Server error ({error_code}) on {key_alias} for {task.pdf_path.name}. Retrying in {backoff:.1f}s (Attempt {attempt}/{max_retries})...[/yellow]"
                )
                time.sleep(backoff)
            else:
                console.print(
                    f"[red]API Error on {key_alias} for {task.pdf_path.name}: {error_message}[/red]"
                )
                if attempt >= max_retries:
                    return False, f"[ERROR: Gemini API Error: {error_message}]"
                time.sleep(1.0)

        except Exception as e:
            console.print(
                f"[red]Unexpected error processing {task.pdf_path.name}: {e}[/red]"
            )
            if attempt >= max_retries:
                return False, f"[ERROR: Unexpected exception: {e}]"
            time.sleep(1.0)

    return False, "[ERROR: Max retries exceeded]"


def run_ai_processing(
    processing_dir: Path = Path("processing"),
    prompt_path: Path = Path("prompts/processing-prompt.md"),
    article_filter: Optional[str] = None,
    model_name: str = "gemini-3.5-flash-lite",
    rpm_limit: int = 15,
    max_retries: int = 3,
) -> None:
    """Run the AI extraction pipeline over all pending chunks."""
    system_prompt = read_prompt(prompt_path)
    key_manager = APIKeyManager(rpm_limit=rpm_limit)
    tasks = discover_tasks(processing_dir, article_filter=article_filter)

    if not tasks:
        console.print(
            Panel(
                "[green]No pending PDF chunks to process.[/green]\n"
                "All chunks in processing/ already have corresponding .md files, "
                "or no articles were found.",
                title="AI Processor Status",
                border_style="green",
            )
        )
        return

    console.print(
        Panel(
            f"[bold cyan]AI Markdown Extraction[/bold cyan]\n"
            f"Pending Chunks: [green]{len(tasks)}[/green]\n"
            f"Active API Keys: [green]{key_manager.key_count}[/green] (Rate limit: {rpm_limit} RPM/key)\n"
            f"Model: [green]{model_name}[/green]\n"
            f"System Prompt: [green]{prompt_path}[/green]",
            border_style="cyan",
        )
    )

    table = Table(title="Processing Execution", show_lines=True)
    table.add_column("Article", style="cyan", no_wrap=False)
    table.add_column("Chunk", justify="center", style="white")
    table.add_column("Type", justify="center")
    table.add_column("Status", justify="center", style="bold")

    success_count = 0
    error_count = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task_bar = progress.add_task("[cyan]Processing chunks...", total=len(tasks))

        for task in tasks:
            desc = f"[cyan]Processing: {task.article_name[:20]} / {task.pdf_path.name}"
            progress.update(task_bar, description=desc)

            if task.is_docling_fallback:
                type_label = "[bold magenta]Docling Fallback[/bold magenta]"
                md_content = process_with_docling(task.pdf_path)
                success = not md_content.startswith("[ERROR:")
            else:
                type_label = (
                    "[bold yellow]HIL Retry (!)[/bold yellow]"
                    if task.is_force_retry
                    else "[blue]Normal[/blue]"
                )
                success, md_content = process_with_gemini(
                    task=task,
                    system_prompt=system_prompt,
                    key_manager=key_manager,
                    model_name=model_name,
                    max_retries=max_retries,
                )

            # Write output .md
            task.output_md_path.write_text(md_content, encoding="utf-8")

            # Handle renaming if it was a forced retry or docling fallback
            if task.is_force_retry or task.is_docling_fallback:
                if task.pdf_path != task.clean_pdf_path:
                    try:
                        # Rename X-Y!.pdf -> X-Y.pdf
                        task.pdf_path.rename(task.clean_pdf_path)
                    except Exception as e:
                        console.print(
                            f"[yellow]Warning: Failed to rename {task.pdf_path.name} to {task.clean_pdf_path.name}: {e}[/yellow]"
                        )

            if success:
                success_count += 1
                status_str = "[green]✓ Success[/green]"
            else:
                error_count += 1
                status_str = "[red]✗ Error[/red]"

            table.add_row(task.article_name, task.pdf_path.name, type_label, status_str)
            progress.advance(task_bar)

    console.print(table)
    console.print(
        f"[bold green]AI Processing Completed![/bold green] "
        f"Success: [green]{success_count}[/green], Errors: [red]{error_count}[/red]\n"
    )


def main() -> None:
    """CLI entry point for AI Processor."""
    parser = argparse.ArgumentParser(
        description="Process PDF chunks in processing/ into Markdown using Gemini API with key rotation."
    )
    parser.add_argument(
        "-p",
        "--processing-dir",
        type=Path,
        default=Path("processing"),
        help="Path to processing directory (default: processing)",
    )
    parser.add_argument(
        "--prompt-path",
        type=Path,
        default=Path("prompts/processing-prompt.md"),
        help="Path to system prompt markdown file (default: prompts/processing-prompt.md)",
    )
    parser.add_argument(
        "-a",
        "--article",
        type=str,
        default=None,
        help="Specific article folder name to process (optional)",
    )
    parser.add_argument(
        "-m",
        "--model",
        type=str,
        default="gemini-3.5-flash-lite",
        help="Gemini model name (default: gemini-3.5-flash-lite)",
    )
    parser.add_argument(
        "--rpm-limit",
        type=int,
        default=15,
        help="RPM limit per API key (default: 15)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=3,
        help="Maximum retries per chunk on error (default: 3)",
    )

    args = parser.parse_args()
    run_ai_processing(
        processing_dir=args.processing_dir,
        prompt_path=args.prompt_path,
        article_filter=args.article,
        model_name=args.model,
        rpm_limit=args.rpm_limit,
        max_retries=args.max_retries,
    )


if __name__ == "__main__":
    main()
