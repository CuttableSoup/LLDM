"""!
@file LLM_Rag.py
@brief Builds and queries a local retrieval index over PDF sourcebooks under Settings/Fantasy/
    (ex: "Inner Sea World Guide.pdf"), so LLMCore.perform_rag can ground narration in actual
    campaign-setting text instead of letting the LLM invent lore wholesale. RagIndex is a
    self-contained helper LLMCore owns by composition, not a mixin -- unlike DMCore's mixins
    (DM_Combat.py etc.), which all share DMCore's own entities/rules/skills state, RagIndex
    needs nothing from LLMCore beyond the event bus for logging, so plain ownership
    (self.rag_index = RagIndex(event_bus) in LLMCore.__init__) is the simpler fit.

    Deliberately data-driven the same way Rules/Fantasy/*.toml is: indexes every *.pdf found
    under Settings/Fantasy/ generically, so dropping in a second sourcebook needs no code
    change. Mirrors NLPCore's own embed-and-cosine-match pattern (SentenceTransformer +
    numpy) rather than pulling in a dedicated vector-store dependency, since the corpus here
    (a few thousand chunks at most) never needs an index structure fancier than a flat matrix.
"""

import glob
import hashlib
import json
import os
import re
import threading

import numpy as np
from pypdf import PdfReader
from sentence_transformers import SentenceTransformer

# A sentence-transformers phrase embedding silently truncates past ~256 tokens (roughly
# 180-200 words) -- chunks are capped well under that so no chunk ever loses its tail
# to truncation. MIN_CHUNK_WORDS drops the sub-fragments a map/table page's scattered
# labels produce (see _chunk_page_text), which would otherwise pollute the index with
# noise that can't meaningfully match any real query.
MAX_CHUNK_WORDS = 180
MIN_CHUNK_WORDS = 40


class RagIndex:
    """!
    @brief Extracts, chunks, embeds, and caches every PDF under source_dir, then answers
        nearest-neighbor lore queries against the result.
    """

    def __init__(self, event_bus, source_dir=None, cache_dir=None, top_k=3, confidence_threshold=0.3):
        """!
        @brief Kicks off index construction on a background thread -- building the index the
            first time (extracting + embedding an entire book) can take minutes, and must
            never block LLMCore's own boot or the app's first narration. query() returns
            no matches until self.ready is True; there's no blocking wait anywhere.
        @param event_bus The central event bus instance, used only for log_info/log_warning/
            log_error -- RagIndex never publishes or subscribes to gameplay events.
        @param source_dir Directory to scan for *.pdf files. Defaults to Settings/Fantasy/,
            next to Rules/Fantasy/ -- the only setting actually wired into any scenario today.
        @param cache_dir Directory for the cached chunks/embeddings. Defaults to a
            .rag_cache/ subdirectory of source_dir.
        @param top_k Maximum number of chunks a single query() call returns.
        @param confidence_threshold Minimum cosine similarity a chunk must clear to be
            returned at all -- below this, an unrelated query returns no lore rather than
            the closest-of-a-bad-lot chunk, the same principle NLPCore's own
            confidence_threshold applies to skill matching.
        """
        self.event_bus = event_bus
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.source_dir = source_dir or os.path.join(base_dir, "Settings", "Fantasy")
        self.cache_dir = cache_dir or os.path.join(self.source_dir, ".rag_cache")
        self.top_k = top_k
        self.confidence_threshold = confidence_threshold

        self.model = None
        self.chunks = []
        # Rows are unit-normalized once at build/load time (see _build), so query() only
        # ever needs a plain dot product, not a norm computed fresh on every call.
        self.chunk_embeddings = None
        self.ready = False

        threading.Thread(target=self._build, daemon=True).start()

    def _build(self):
        """!
        @brief Loads a cached index if one matches the current source PDFs, otherwise
            extracts/chunks/embeds them fresh and writes the cache for next time. Runs
            entirely on the background thread started by __init__; any failure is logged
            and leaves self.ready False rather than crashing LLMCore.
        """
        try:
            pdf_paths = sorted(glob.glob(os.path.join(self.source_dir, "*.pdf")))
            if not pdf_paths:
                self.event_bus.publish("log_warning", f"RagIndex: no PDFs found in {self.source_dir}.")
                return

            self.model = SentenceTransformer("all-MiniLM-L6-v2")

            cache_key = self._cache_key(pdf_paths)
            chunks, embeddings = self._load_cache(cache_key)
            if chunks is None:
                self.event_bus.publish(
                    "log_info", f"RagIndex: no cache for {len(pdf_paths)} source(s), extracting/embedding now."
                )
                chunks = self._extract_chunks(pdf_paths)
                if not chunks:
                    self.event_bus.publish("log_warning", "RagIndex: no extractable text found in any source PDF.")
                    return
                embeddings = self.model.encode(
                    [chunk["text"] for chunk in chunks], convert_to_numpy=True, show_progress_bar=False, batch_size=64
                )
                self._save_cache(cache_key, chunks, embeddings)

            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            self.chunk_embeddings = embeddings / np.maximum(norms, 1e-10)
            self.chunks = chunks
            self.ready = True
            self.event_bus.publish(
                "log_info", f"RagIndex: ready with {len(chunks)} chunks from {len(pdf_paths)} source(s)."
            )
        except Exception as e:
            self.event_bus.publish("log_error", f"RagIndex: failed to build index: {e}")

    def _cache_key(self, pdf_paths):
        """!
        @brief Fingerprints the current source PDFs (path, size, mtime) so an edited or
            replaced PDF invalidates the cache automatically rather than silently serving a
            stale index -- the same "trust disk state, not what's in memory" convention
            DMCore.load_game uses when re-instancing from fresh TOML.
        @param pdf_paths Sorted list of source PDF paths.
        @return A hex digest identifying this exact set of source files.
        """
        digest = hashlib.sha1()
        for path in pdf_paths:
            stat = os.stat(path)
            digest.update(f"{path}:{stat.st_size}:{stat.st_mtime}".encode("utf-8"))
        return digest.hexdigest()

    def _cache_paths(self, cache_key):
        return (
            os.path.join(self.cache_dir, f"{cache_key}.chunks.json"),
            os.path.join(self.cache_dir, f"{cache_key}.embeddings.npy"),
        )

    def _load_cache(self, cache_key):
        """!
        @brief Reads a previously-built index back from disk, if present.
        @param cache_key This exact source-file-set's fingerprint (see _cache_key).
        @return (chunks, embeddings), or (None, None) if there's no matching cache, or if a
                cache file exists but fails to load (corrupt/truncated write) -- treated the
                same as no cache at all, rebuilt fresh, rather than raising.
        """
        chunks_path, embeddings_path = self._cache_paths(cache_key)
        if not (os.path.exists(chunks_path) and os.path.exists(embeddings_path)):
            return None, None
        try:
            with open(chunks_path, "r", encoding="utf-8") as f:
                chunks = json.load(f)
            embeddings = np.load(embeddings_path)
            return chunks, embeddings
        except Exception as e:
            self.event_bus.publish("log_warning", f"RagIndex: cache at {chunks_path} unreadable ({e}), rebuilding.")
            return None, None

    def _save_cache(self, cache_key, chunks, embeddings):
        os.makedirs(self.cache_dir, exist_ok=True)
        chunks_path, embeddings_path = self._cache_paths(cache_key)
        with open(chunks_path, "w", encoding="utf-8") as f:
            json.dump(chunks, f)
        np.save(embeddings_path, np.asarray(embeddings))

    def _extract_chunks(self, pdf_paths):
        """!
        @brief Extracts every page of every source PDF into word-bounded chunks.
        @param pdf_paths Sorted list of source PDF paths.
        @return A list of {source, page, text} dicts, one per chunk, in source/page order.
        """
        chunks = []
        for path in pdf_paths:
            source_name = os.path.splitext(os.path.basename(path))[0]
            reader = PdfReader(path)
            for page_number, page in enumerate(reader.pages, start=1):
                page_text = page.extract_text() or ""
                for chunk_text in self._chunk_page_text(page_text):
                    chunks.append({"source": source_name, "page": page_number, "text": chunk_text})
        return chunks

    def _chunk_page_text(self, text):
        """!
        @brief Splits one page's raw extracted text into MAX_CHUNK_WORDS-bounded chunks.
            Sentence-bounded (not paragraph-bounded): pypdf's extract_text() reflects the
            PDF's own line-wrap layout, not real paragraph breaks, so splitting on blank
            lines is unreliable across this corpus (dense body-text pages and sparse
            map/table pages both). Splitting on sentence punctuation instead works
            regardless of that layout, at the cost of an occasional chunk boundary landing
            mid-paragraph -- acceptable for lore grounding, not a legal document.
        @param text One page's raw extract_text() output.
        @return A list of chunk strings, each MIN_CHUNK_WORDS..MAX_CHUNK_WORDS words long.
                A page with too little real text (ex: a map page reduced to a handful of
                scattered place-name labels) contributes no chunks at all.
        """
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized:
            return []

        sentences = re.split(r"(?<=[.!?])\s+", normalized)
        chunks = []
        current_words = []
        for sentence in sentences:
            words = sentence.split()
            if current_words and len(current_words) + len(words) > MAX_CHUNK_WORDS:
                chunks.append(" ".join(current_words))
                current_words = []
            current_words.extend(words)
        if current_words:
            chunks.append(" ".join(current_words))

        return [chunk for chunk in chunks if len(chunk.split()) >= MIN_CHUNK_WORDS]

    def query(self, text, top_k=None, confidence_threshold=None):
        """!
        @brief Finds the chunks most semantically similar to text.
        @param text The query string (ex: a narration prompt or the player's raw input).
        @param top_k Overrides self.top_k for this call.
        @param confidence_threshold Overrides self.confidence_threshold for this call.
        @return A list of (chunk, score) pairs, best match first, score descending -- empty
                if the index isn't ready yet, has no chunks, or nothing clears the
                confidence threshold.
        """
        if not self.ready or self.chunk_embeddings is None or not len(self.chunks):
            return []

        top_k = self.top_k if top_k is None else top_k
        threshold = self.confidence_threshold if confidence_threshold is None else confidence_threshold

        query_embedding = self.model.encode(text, convert_to_numpy=True)
        query_embedding = query_embedding / max(np.linalg.norm(query_embedding), 1e-10)
        scores = self.chunk_embeddings @ query_embedding

        top_indices = np.argsort(-scores)[:top_k]
        return [(self.chunks[i], float(scores[i])) for i in top_indices if scores[i] >= threshold]
