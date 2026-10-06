import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import dsp_frames  # noqa: E402


def write(path, lines):
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def test_align_differing_first(tmp_path):
    a = dsp_frames.read_rows(
        write(tmp_path / "a.csv", ["frame,x,y", "0,1,1", "1,2,2", "2,3,3"])
    )
    b = dsp_frames.read_rows(
        write(tmp_path / "b.csv", ["frame,x,y", "5,0,0", "6,1,1", "7,2,9", "8,3,3"])
    )
    al = dsp_frames.align(a, b, 1, 7, before=1, after=5)
    assert [o for o, _, _ in al] == [-1, 0, 1]
    assert dsp_frames.differing(al[0][1], al[0][2]) == []
    assert dsp_frames.differing(al[1][1], al[1][2]) == [("y", "2", "9")]
    assert dsp_frames.differing(al[1][1], al[1][2], re.compile("^x")) == []
    assert dsp_frames.first_difference(al) == (0, [("y", "2", "9")])


def test_summary_and_cli(tmp_path, capsys):
    p = write(tmp_path / "a.csv", ["frame,x,v", "0,1,", "1,0,0:2"])
    s = dsp_frames.summarize(dsp_frames.read_rows(p))
    assert s == {"x": (1.0, 1), "v": (0.0, 1)}
    assert dsp_frames.main(["summary", p]) == 0
    assert "x\ttotal=1\tnonzero=1" in capsys.readouterr().out
    assert dsp_frames.main(["first", p, p, "--align", "0", "0"]) == 1
