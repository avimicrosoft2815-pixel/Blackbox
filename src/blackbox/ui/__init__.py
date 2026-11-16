"""The viewer: a capsule rendered as one offline HTML file (`blackbox view`), and the model it renders (P4-10).

The package exists so that "the viewer" is a module with tests rather than a folder of static files nobody can
regress: `model.py` turns a capsule into a document, `viewer.py` inlines that document into the template, and
`static/` holds the three files a person reads when they want to know what the page does.
"""
from __future__ import annotations

from blackbox.ui import model, viewer

__all__ = ["model", "viewer"]
