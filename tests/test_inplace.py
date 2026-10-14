"""dt2.inplace: edit a section by re-encoding only the tokens the edit touches (needs the 1.16 firmware)."""

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SYX = ROOT / "Digitakt_II_OS1.16.syx"
pytestmark = pytest.mark.skipif(
    not SYX.exists(), reason="needs Digitakt_II_OS1.16.syx in the repo root"
)

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import patched_image  # noqa: E402
from dt2 import container, inplace  # noqa: E402


def test_no_edit_reproduces_the_stock_file_byte_for_byte():
    data, stats = inplace.patch_section(SYX, 3, {})
    assert data == SYX.read_bytes()
    assert stats["kept"] == stats["tokens"]


def test_string_edit_keeps_the_container_size_and_the_other_sections(tmp_path):
    out = tmp_path / "patched.syx"
    report = patched_image.make(SYX, "UNTITLED", "PATCHKIT", out)
    assert report["check_authenticity"]
    assert (
        report["sections_changed"] == [3] and report["section_3_changed_only_at_patch"]
    )
    assert report["section_3_bytes_changed"] == 7
    assert 0 <= report["container_bytes"] - report["stock_container_bytes"] <= 4096
    # The other sections keep their stored bytes.
    c0, s0 = container.sections(SYX)
    c1, s1 = container.sections(out)
    for (sid, o0, n0, _d), (_sid, o1, n1, _e) in zip(s0, s1, strict=True):
        if sid != 3:
            assert c0[o0 : o0 + n0] == c1[o1 : o1 + n1]


def test_edits_that_do_not_fit_are_refused():
    with pytest.raises(ValueError, match="keep the string.s length"):
        patched_image.make(SYX, "UNTITLED", "SHORT", pathlib.Path("/nonexistent/x.syx"))
    with pytest.raises(ValueError, match="not in section 3"):
        patched_image.locate(
            patched_image.sections_of(SYX)[3], "NOT A STRING IN MAIN OS"
        )
