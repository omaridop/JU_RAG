"""University of Jordan — Bilingual RAG Assistant.

A retrieval-augmented chatbot for University of Jordan students, in Arabic and
English. Documents (PDF/DOCX) are semantically chunked by DeepSeek V4 Flash,
embedded with Gemini Embedding 2, and stored in a Chroma vector database. The
chatbot answers from that knowledge base with recency-aware retrieval.

Everything (chat + embeddings) is served through a single OpenRouter API key.
"""

__version__ = "1.0.0"
