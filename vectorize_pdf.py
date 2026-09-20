"""!
@file vectorize_pdf.py
@brief Standalone CLI that pre-builds LLM_Rag.RagIndex's own embedding cache for one or more
    PDF sourcebooks, without booting the rest of the app. Reuses RagIndex's exact chunking/
    embedding/caching pipeline (no logic duplicated here), so the cache this produces is
    byte-for-byte what LLMCore's real boot would build and load once LLMCore.set_setting points
    it at that same directory's own ".rag_cache/" -- just run ahead of time, synchronously, with
    progress printed to the console instead of running silently on a background thread during
    the app's first narration request (which is otherwise where the multi-minute first-time
    extract/embed cost gets paid, unnoticed until a query comes back empty).

Usage:
    python vectorize_pdf.py                          # vectorize every Settings/<setting>/, one cache each
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
from paths import PROJECT_ROOT


def _make_logging_event_bus(prefix=None):
    """!
    @brief A bare EventBus with print-based subscribers for all three log levels RagIndex
        publishes. Distinct from Logger.py (which only wires log_info/log_error) since this
        script wants log_warning surfaced too (ex: "no PDFs found", "cache unreadable,
        rebuilding") rather than silently dropped, useful for a diagnostic CLI tool.
    @param prefix Optional label (ex: a setting's own directory name) prepended to every line
        this bus prints -- main()'s own "every setting" path runs several RagIndex builds
        concurrently on their own background threads (see _start_index), so without a label
        their log lines would interleave with no way to tell which setting a given line
        belongs to.
    @return A ready-to-use EventBus.
    """
    event_bus = EventBus()
    tag = f"[{prefix}] " if prefix else ""
    for level in ("log_info", "log_warning", "log_error"):
        label = level.split("_", 1)[1].upper()
        # flush=True -- these fire from RagIndex's own background build thread (see
        # __init__), so without it a log line can visibly land after this script's own
        # main-thread prints that logically came later (ex: "Failed to build" printing
        # before the warning that explains why), purely from separate stdout buffers.
        event_bus.subscribe(level, lambda message, label=label: print(f"{tag}[{label}] {message}", flush=True))
    return event_bus


def _setting_dirs():
    """!
    @brief Every immediate subdirectory of Settings/ -- one per setting's own staged
        sourcebook pack, whatever happens to exist today (ex: "Fantasy", "Pathfinder",
        "Dark Sun"). Deliberately not cross-checked against Rules/<setting>/ -- the two aren't
        required to be paired 1:1 (a setting's sourcebooks can be staged here ahead of any
        matching Rules/ data, the same "Dark Sun" is today), and RagIndex/set_setting only ever
        care about Settings/<setting>/ existing, never about Rules/ alongside it.
    @return Sorted list of absolute directory paths, or [] if Settings/ itself doesn't exist.
    """
    settings_root = os.path.join(PROJECT_ROOT, "Settings")
    if not os.path.isdir(settings_root):
        return []
    return sorted(
        os.path.join(settings_root, name)
        for name in os.listdir(settings_root)
        if os.path.isdir(os.path.join(settings_root, name)) and not name.startswith(".")
    )


def _start_index(source_dir, cache_dir=None, top_k=3, confidence_threshold=0.3, label=None):
    """!
    @brief Constructs a RagIndex and returns immediately, without waiting on it -- RagIndex's
        own __init__ already kicks extraction/chunking/embedding off on a background thread
        (see LLM_Rag.py), so simply not calling wait_until_ready() here yet is what lets
        main()'s own "every setting" loop start several independent builds before blocking on
        any of them, overlapping their CPU-bound embedding work instead of running strictly
        one after another. Pairs with _finish_index, which is what actually blocks.
    @param label Forwarded to _make_logging_event_bus so this build's log lines stay
        attributable once several are interleaving concurrently.
    @return (RagIndex, start_time) -- start_time is time.monotonic() at construction, for
        _finish_index to compute elapsed time from.
    """
    event_bus = _make_logging_event_bus(prefix=label)
    start = time.monotonic()
    index = RagIndex(
        event_bus, source_dir=source_dir, cache_dir=cache_dir,
        top_k=top_k, confidence_threshold=confidence_threshold,
    )
    return index, start


def _finish_index(index, start, exit_on_failure=True):
    """!
    @brief Blocks until index's own background build (started by _start_index) finishes, then
        prints the same indexed/failed summary every caller has always gotten.
    @param exit_on_failure Whether an index that never became ready (ex: no PDFs in
        source_dir) should abort the whole script. True for a single explicitly-named target
        (vectorize()'s own default); main()'s "every setting" loop passes False, since one
        setting's sourcebooks not being staged yet is routine, not a reason to abandon every
        other setting's build.
    @return index, unchanged -- returned for the caller's own convenience (ex: running --query
        against it next).
    """
    index.wait_until_ready()
    elapsed = time.monotonic() - start

    if index.ready:
        print(f"Indexed {len(index.chunks)} chunk(s) from {index.source_dir} in {elapsed:.1f}s.")
        print(f"Cache written to {index.cache_dir}")
    elif exit_on_failure:
        print("Failed to build the index -- see log output above.", file=sys.stderr)
        sys.exit(1)
    else:
        print(f"Skipped {index.source_dir} -- no index built (see log output above).")

    return index


def vectorize(pdf_or_dir=None, cache_dir=None, top_k=3, confidence_threshold=0.3, exit_on_failure=True):
    """!
    @brief Builds (or loads, if an up-to-date cache already exists) the embedding index for
        pdf_or_dir, blocking until done. A thin _start_index + _finish_index wrapper for the
        single-target case -- main()'s own "every setting" path calls those two directly
        instead, so it can start every setting's build before blocking on any of them.
    @param pdf_or_dir Path to a single .pdf file, or a directory of them. A single file
        resolves to its own parent directory (RagIndex indexes every *.pdf in a directory,
        not one file in isolation -- see LLM_Rag.py's module docstring), so the cache this
        produces matches exactly what RagIndex would build for real if later pointed at that
        same directory. None defaults to RagIndex's own default (Settings/Fantasy/).
    @param cache_dir Overrides the default ".rag_cache/" subdirectory of the source directory.
    @param top_k / confidence_threshold Forwarded to RagIndex, only relevant if --query is
        also used to test the freshly built index.
    @param exit_on_failure See _finish_index.
    @return The built RagIndex (ready if the build succeeded, not ready if it failed/skipped).
    """
    if pdf_or_dir and os.path.isfile(pdf_or_dir):
        if not pdf_or_dir.lower().endswith(".pdf"):
            print(f"error: {pdf_or_dir} is not a .pdf file", file=sys.stderr)
            sys.exit(1)
        source_dir = os.path.dirname(os.path.abspath(pdf_or_dir)) or "."
    else:
        source_dir = pdf_or_dir

    index, start = _start_index(source_dir, cache_dir=cache_dir, top_k=top_k, confidence_threshold=confidence_threshold)
    return _finish_index(index, start, exit_on_failure=exit_on_failure)


def main():
    parser = argparse.ArgumentParser(
        description="Pre-build LLM_Rag.RagIndex's embedding cache for one or more PDFs, for RAG grounding.",
    )
    parser.add_argument(
        "pdf_or_dir", nargs="?", default=None,
        help="A single .pdf file, or a directory of them. Omit to vectorize every "
             "Settings/<setting>/ directory in turn, one cache per setting.",
    )
    parser.add_argument(
        "--cache-dir", default=None,
        help="Override the default .rag_cache/ location. Only valid alongside an explicit "
             "pdf_or_dir -- meaningless when vectorizing every setting at once, since each "
             "keeps its own cache next to its own sourcebooks.",
    )
    parser.add_argument("--top-k", type=int, default=3, help="Chunks returned per --query (default: 3).")
    parser.add_argument(
        "--threshold", type=float, default=0.3,
        help="Minimum cosine similarity a chunk must clear for --query to return it (default: 0.3).",
    )
    parser.add_argument(
        "--query", default=None,
        help="After building, run this text as a test query and print the top matches -- "
             "against every setting's own index in turn, when vectorizing all of them.",
    )
    args = parser.parse_args()

    if args.pdf_or_dir is None:
        if args.cache_dir is not None:
            parser.error("--cache-dir requires an explicit pdf_or_dir")

        setting_dirs = _setting_dirs()
        if not setting_dirs:
            print("No Settings/<setting>/ directories found.", file=sys.stderr)
            sys.exit(1)

        # Start every setting's build before waiting on any of them -- RagIndex.__init__
        # already runs extraction/chunking/embedding on its own background thread (see
        # LLM_Rag.py), so constructing all of them up front lets independent settings'
        # CPU-bound embedding work overlap instead of running strictly one after another.
        started = []
        for setting_dir in setting_dirs:
            label = os.path.basename(setting_dir)
            print(f"=== starting {label} ===")
            index, start = _start_index(setting_dir, top_k=args.top_k, confidence_threshold=args.threshold, label=label)
            started.append((setting_dir, index, start))

        indexes = []
        for setting_dir, index, start in started:
            print(f"\n=== {os.path.basename(setting_dir)} ===")
            _finish_index(index, start, exit_on_failure=False)
            indexes.append((setting_dir, index))

        if args.query:
            for setting_dir, index in indexes:
                label = os.path.basename(setting_dir)
                if not index.ready:
                    continue
                matches = index.query(args.query)
                if not matches:
                    print(f"\n[{label}] No chunks cleared the confidence threshold "
                          f"({args.threshold}) for: {args.query!r}")
                    continue
                print(f"\n[{label}] Top {len(matches)} match(es) for: {args.query!r}")
                for chunk, score in matches:
                    print(f"\n  [{score:.3f}] {chunk['source']} p.{chunk['page']}")
                    print(f"  {chunk['text']}")
        return

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
