"""tools/devtools/refstext.py refuses a pdftotext that is not poppler's."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.devtools import refstext


def _run(stdout="", stderr=""):
    return lambda *a, **k: SimpleNamespace(stdout=stdout, stderr=stderr)


def test_xpdf_is_refused():
    xpdf = "pdftotext version 4.00\nCopyright 1996-2017 Glyph & Cog, LLC\n"
    with pytest.raises(SystemExit, match="not poppler"):
        refstext.require_poppler(_run(stderr=xpdf))


def test_poppler_is_accepted():
    poppler = (
        "pdftotext version 24.02.0\n"
        "Copyright 2005-2024 The Poppler Developers - http://poppler.freedesktop.org\n"
    )
    refstext.require_poppler(_run(stderr=poppler))
