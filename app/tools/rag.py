"""RAG (Retrieval-Augmented Generation) tools.

rag_ingest — embed local files into a ChromaDB vector store (with mtime-based cache invalidation).
rag_search — semantic search over indexed files.

ChromaDB persists to {WORKSPACE_DIR}/.chromadb/
Collection: "raion_rag"
Embedding model: text-embedding-3-small (OpenAI)
"""
from __future__ import annotations

import hashlib
import os
from typing import Annotated

from langchain_core.tools import tool

import app.config as app_config

# ---------------------------------------------------------------------------
# Supported plain-text extensions
# ---------------------------------------------------------------------------

_TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".html", ".css",
    ".json", ".yaml", ".yml", ".toml", ".csv",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_collection():
    """Return (or create) the ChromaDB persistent collection."""
    import chromadb

    chroma_dir = app_config.WORKSPACE_DIR / ".chromadb"
    chroma_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(chroma_dir))
    return client.get_or_create_collection(name="raion_rag")


def _extract_text(file_path: str) -> str | None:
    """Extract plain text from a file. Returns None for unsupported types."""
    ext = os.path.splitext(file_path)[1].lower()

    if ext in _TEXT_EXTENSIONS:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()

    if ext == ".pdf":
        try:
            from pypdf import PdfReader
            reader = PdfReader(file_path)
            parts = []
            for page in reader.pages:
                text = page.extract_text()
                if text:
                    parts.append(text)
            return "\n".join(parts)
        except Exception as exc:
            raise RuntimeError(f"PDF read error: {exc}") from exc

    if ext == ".docx":
        try:
            from docx import Document
            doc = Document(file_path)
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception as exc:
            raise RuntimeError(f"DOCX read error: {exc}") from exc

    return None  # unsupported


def _chunk_text(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Split text into overlapping chunks, preferring sentence boundaries."""
    # Split on sentence boundary candidates first
    import re
    # Split on ". " or "\n" to get natural segments
    raw_segments = re.split(r"(?<=\. )|(?<=\n)", text)

    chunks: list[str] = []
    current = ""

    for seg in raw_segments:
        if len(current) + len(seg) <= chunk_size:
            current += seg
        else:
            if current.strip():
                chunks.append(current.strip())
            # Overlap: carry tail of previous chunk
            if overlap > 0 and len(current) > overlap:
                current = current[-overlap:] + seg
            else:
                current = seg

    if current.strip():
        chunks.append(current.strip())

    # Safety net: if a single segment exceeded chunk_size, hard-split it
    result: list[str] = []
    for chunk in chunks:
        while len(chunk) > chunk_size:
            result.append(chunk[:chunk_size])
            chunk = chunk[chunk_size - overlap:]
        if chunk.strip():
            result.append(chunk)

    return result


def _file_id_prefix(file_path: str) -> str:
    """Return an MD5 hash of the file path, used as an ID prefix for ChromaDB."""
    return hashlib.md5(file_path.encode()).hexdigest()


def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of text strings using OpenAI text-embedding-3-small."""
    from openai import OpenAI

    client = OpenAI(api_key=app_config.OPENAI_API_KEY)
    response = client.embeddings.create(
        input=texts,
        model="text-embedding-3-small",
    )
    return [item.embedding for item in response.data]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def rag_ingest(
    paths: Annotated[list[str], "List of absolute file paths to ingest into the vector store"],
) -> str:
    """Ingest local files into the RAG vector store (ChromaDB).

    Skips files that are already indexed and unmodified (mtime-based cache).
    Re-embeds files that have been modified since last ingestion.
    Supported: .txt .md .py .js .ts .html .css .json .yaml .yml .toml .csv .pdf .docx
    """
    try:
        collection = _get_collection()
    except Exception as exc:
        return f"Error initialising ChromaDB: {exc}"

    ingested: list[str] = []
    cached: list[str] = []
    skipped: list[str] = []

    for file_path in paths:
        file_path = str(file_path)
        basename = os.path.basename(file_path)

        # --- existence check ---
        if not os.path.isfile(file_path):
            skipped.append(f"{basename} (file not found)")
            continue

        # --- unsupported type check ---
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in _TEXT_EXTENSIONS and ext not in (".pdf", ".docx"):
            skipped.append(f"{basename} (unsupported type)")
            continue

        current_mtime = os.path.getmtime(file_path)
        fp_prefix = _file_id_prefix(file_path)

        # --- cache check: query existing chunks ---
        try:
            existing = collection.get(
                where={"file_path": file_path},
                include=["metadatas"],
            )
        except Exception:
            existing = {"ids": [], "metadatas": []}

        if existing["ids"]:
            stored_mtime = existing["metadatas"][0].get("mtime", 0.0)
            if float(stored_mtime) >= current_mtime:
                cached.append(f"{basename} (cached)")
                continue
            # Modified — delete old chunks
            try:
                collection.delete(where={"file_path": file_path})
            except Exception:
                pass

        # --- extract text ---
        try:
            text = _extract_text(file_path)
        except Exception as exc:
            skipped.append(f"{basename} (read error: {exc})")
            continue

        if text is None:
            skipped.append(f"{basename} (unsupported type)")
            continue

        if not text.strip():
            skipped.append(f"{basename} (empty content)")
            continue

        # --- chunk ---
        chunks = _chunk_text(text)
        if not chunks:
            skipped.append(f"{basename} (no chunks produced)")
            continue

        # --- embed ---
        try:
            embeddings = _embed_texts(chunks)
        except Exception as exc:
            skipped.append(f"{basename} (embedding error: {exc})")
            continue

        # --- store ---
        ids = [f"{fp_prefix}::chunk_{i}" for i in range(len(chunks))]
        metadatas = [
            {"file_path": file_path, "mtime": current_mtime, "chunk_index": i}
            for i in range(len(chunks))
        ]

        try:
            collection.add(
                ids=ids,
                embeddings=embeddings,
                documents=chunks,
                metadatas=metadatas,
            )
        except Exception as exc:
            skipped.append(f"{basename} (store error: {exc})")
            continue

        ingested.append(f"{basename} ({len(chunks)} chunks)")

    # --- build summary ---
    parts: list[str] = []
    all_done = ingested + cached
    if all_done:
        parts.append("Ingested " + str(len(ingested + cached)) + " file(s): " + ", ".join(all_done))
    if skipped:
        parts.append("Skipped: " + ", ".join(skipped))

    return ". ".join(parts) if parts else "No files processed."


@tool
def rag_search(
    query: Annotated[str, "Search query to find relevant content"],
    paths: Annotated[list[str] | None, "Optional list of file paths to restrict search to"] = None,
) -> str:
    """Search the RAG vector store for content relevant to a query.

    Embeds the query with OpenAI text-embedding-3-small and returns the top-5
    most similar chunks from ChromaDB.  If `paths` is given, only chunks from
    those files are searched.
    """
    try:
        collection = _get_collection()
    except Exception as exc:
        return f"Error initialising ChromaDB: {exc}"

    # Check collection is not empty
    try:
        count = collection.count()
    except Exception:
        count = 0

    if count == 0:
        return f"No relevant content found for: {query}\n(Vector store is empty — run rag_ingest first)"

    # --- embed query ---
    try:
        query_embedding = _embed_texts([query])[0]
    except Exception as exc:
        return f"Error embedding query: {exc}"

    # --- build where filter ---
    where: dict | None = None
    if paths:
        path_strs = [str(p) for p in paths]
        if len(path_strs) == 1:
            where = {"file_path": path_strs[0]}
        else:
            where = {"file_path": {"$in": path_strs}}

    # --- query ---
    try:
        kwargs: dict = {
            "query_embeddings": [query_embedding],
            "n_results": min(5, count),
            "include": ["documents", "metadatas", "distances"],
        }
        if where:
            kwargs["where"] = where

        results = collection.query(**kwargs)
    except Exception as exc:
        return f"Error querying vector store: {exc}"

    ids = results.get("ids", [[]])[0]
    if not ids:
        return f"No relevant content found for: {query}"

    documents = results.get("documents", [[]])[0]
    metadatas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]

    lines: list[str] = [f"Top results for: {query}\n"]
    for i, (doc, meta, dist) in enumerate(zip(documents, metadatas, distances), 1):
        file_path = meta.get("file_path", "unknown")
        chunk_idx = meta.get("chunk_index", "?")
        basename = os.path.basename(file_path)
        # ChromaDB returns L2 distance; convert to a rough similarity score
        similarity = round(1 / (1 + dist), 3)
        lines.append(
            f"{i}. [{basename}] (chunk {chunk_idx}, similarity={similarity})\n"
            f"   {doc[:400]}"
        )

    return "\n\n".join(lines)
