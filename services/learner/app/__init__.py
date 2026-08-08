"""Learner service — convention extractor and Qdrant vector store.

This service:
- Listens for merged PR events from the Redis stream
- Fetches the merged PR diff from GitHub
- Uses an LLM to extract coding conventions
- Stores conventions in Qdrant for use by the Style agent
"""

__version__ = "0.1.0"
