"""Reviewer service — GitHub comment publisher.

This service:
- Receives completed review results from the worker via Celery
- Formats findings as rich GitHub markdown comments
- Posts a Pull Request Review with inline comments via the GitHub API
"""

__version__ = "0.1.0"
