"""!
@file vectorize_pdf.py
@brief Standalone CLI that pre-builds LLM_Rag.RagIndex's own embedding cache for one or more
    PDF sourcebooks, without booting the rest of the app. Reuses RagIndex's exact chunking/
    embedding/caching pipeline (no logic duplicated here), so the cache this produces is
    byte-for-byte what LLMCore's real boot would build and load from Settings/Fantasy/.toml's
    ".rag_cache/" -- just run ahead of time, synchronously, with progress printed to the
    console instead of running silently on a background thread during the app's first
    narration request (which is otherwise where the multi-minute first-time extract/embed
    cost gets paid, unnoticed until a query comes back empty).

Usage:
    python vectorize_pdf.py                          # vectorize Settings/Fantasy/ (default)
    python vectorize_pdf.py path/to/sourcebook.pdf    # vectorize just that PDF's directory
    python vectorize_pdf.py path/to/pdf_dir/          # vectorize every *.pdf in that directory
    python vectorize_pdf.py some.pdf --query "who rules Brevoy?"  # build, then test a query
"""

import argparse
import os
import sys
import time

from Event_Bus import EventBus
from llm.LLM_Rag import RagIndex


def _make_logging_event_bus():
    """!
    @brief A bare EventBus with print-based subscribers for all three log levels RagIndex
        publishes. Distinct from Logger.py (which only wires log_info/log_error) since this
        script wants log_warning surfaced too (ex: "no PDFs found", "cache unreadable,
        rebuilding") rather than silently dropped, useful for a diagnostic CLI tool.
    @return A ready-to-use EventBus.
    """
    event_bus = EventBus()
    for level in ("log_info", "log_warning", "log_error"):
        label = level.split("_", 1)[1].upper()
        # flush=True -- these fire from RagIndex's own background build thread (see
        # __init__), so without it a log line can visibly land after this script's own
        # main-thread prints that logically came later (ex: "Failed to build" printing
        # before the warning that explains why), purely from separate stdout buffers.
        event_bus.subscribe(level, lambda message, label=label: print(f"[{label}] {message}", flush=True))
    return event_bus


def vectorize(pdf_or_dir=None, cache_dir=None, top_k=3, confidence_threshold=0.3):
    """!
    @brief Builds (or loads, if an up-to-date cache already exists) the embedding index for
        pdf_or_dir, blocking until done.
    @param pdf_or_dir Path to a single .pdf file, or a directory of them. A single file
        resolves to its own parent directory (RagIndex indexes every *.pdf in a directory,
        not one file in isolation -- see LLM_Rag.py's module docstring), so the cache this
        produces matches exactly what RagIndex would build for real if later pointed at that
        same directory. None defaults to RagIndex's own default (Settings/Fantasy/).
    @param cache_dir Overrides the default ".rag_cache/" subdirectory of the source directory.
    @param top_k / confidence_threshold Forwarded to RagIndex, only relevant if --query is
        also used to test the freshly built index.
    @return The built RagIndex (ready if the build succeeded, not ready if it failed).
    """
    if pdf_or_dir and os.path.isfile(pdf_or_dir):
        if not pdf_or_dir.lower().endswith(".pdf"):
            print(f"error: {pdf_or_dir} is not a .pdf file", file=sys.stderr)
            sys.exit(1)
        source_dir = os.path.dirname(os.path.abspath(pdf_or_dir)) or "."
    else:
        source_dir = pdf_or_dir

    event_bus = _make_logging_event_bus()
    start = time.monotonic()
    index = RagIndex(
        event_bus, source_dir=source_dir, cache_dir=cache_dir,
        top_k=top_k, confidence_threshold=confidence_threshold,
    )
    index.wait_until_ready()
    elapsed = time.monotonic() - start

    if index.ready:
        print(f"Indexed {len(index.chunks)} chunk(s) from {index.source_dir} in {elapsed:.1f}s.")
        print(f"Cache written to {index.cache_dir}")
    else:
        print("Failed to build the index -- see log output above.", file=sys.stderr)
        sys.exit(1)

    return index


def main():
    parser = argparse.ArgumentParser(
        description="Pre-build LLM_Rag.RagIndex's embedding cache for one or more PDFs, for RAG grounding.",
    )
    parser.add_argument(
        "pdf_or_dir", nargs="?", default=None,
        help="A single .pdf file, or a directory of them. Defaults to Settings/Fantasy/.",
    )
    parser.add_argument("--cache-dir", default=None, help="Override the default .rag_cache/ location.")
    parser.add_argument("--top-k", type=int, default=3, help="Chunks returned per --query (default: 3).")
    parser.add_argument(
        "--threshold", type=float, default=0.3,
        help="Minimum cosine similarity a chunk must clear for --query to return it (default: 0.3).",
    )
    parser.add_argument(
        "--query", default=None,
        help="After building, run this text as a test query and print the top matches.",
    )
    args = parser.parse_args()

    index = vectorize(args.pdf_or_dir, cache_dir=args.cache_dir, top_k=args.top_k, confidence_threshold=args.threshold)

    if args.query:
        matches = index.query(args.query)
        if not matches:
            print(f"\nNo chunks cleared the confidence threshold ({args.threshold}) for: {args.query!r}")
            return
        print(f"\nTop {len(matches)} match(es) for: {args.query!r}")
        for chunk, score in matches:
            print(f"\n  [{score:.3f}] {chunk['source']} p.{chunk['page']}")
            print(f"  {chunk['text']}")


if __name__ == "__main__":
    main()
