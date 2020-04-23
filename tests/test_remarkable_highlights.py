import os

import fitz
import pytest

from remarkable_highlights import WordSelectionMethod, extract_highlights


@pytest.fixture
def rootdir():
    return os.path.dirname(os.path.abspath(__file__))


def test_unix_pdf(rootdir):
    with fitz.open(os.path.join(rootdir, "./pdfs/unix.pdf")) as doc:
        page_text_highlights, page_clips = extract_highlights(
            doc, 3, "CENTROID", 1000, 3, False
        )

        print(page_text_highlights)

        assert page_text_highlights == {
            1: [
                "The UNIX Time- Sharing System",
                "Perhaps the most important achievement of UNIX is to demonstrate that a powerful operating system for interactive use need not be expensive either in equipment or in human effort: UNIX can run on hardware costing as little as $40,000,",
                "Communications July 1974 of Volume 17 the ACM Number 7",
            ],
            8: [
                'Given this framework, the implementation of back- ground processes is trivial; whenever a command line contains "&", the Shell merely refrains from waiting'
            ],
        }

        assert {n: len(clips) for n, clips in page_clips.items()} == {11: 1}


def test_fortran_pdf(rootdir):
    with fitz.open(os.path.join(rootdir, "./pdfs/fortran.pdf")) as doc:
        page_text_highlights, page_clips = extract_highlights(
            doc, 3, "CENTROID", 1000, 3, False
        )

        assert page_text_highlights == {
            2: [
                "The F O R T R A N Automatic Coding System",
                "HE FORTRAN project was begun in the sum- mer of 1954.",
            ],
            6: [
                "Basically, it is simple, and most of the complexities which it does possess arise from the effort to cause it to produce object programs which can compete in efficiency with hand-written programs."
            ],
            7: ["A(2* I + 1,4* J + 3 , 6 * K + 5),"],
            10: [
                "The preceding sections of this paper have described the language and the translator program of the FOR- TRAN system. Following are some comments on the system aqd its application."
            ],
            11: [
                "I t is considerably easier to teach people untrained in the use of computers how to write programs in FORTRAN language than it is to teach them machine language."
            ],
            12: [
                "The generality and complexity of some of the tech- niques employed to achieve efficient output programs may often be superfluous in many common applications."
            ],
        }

        assert {n: len(clips) for n, clips in page_clips.items()} == {6: 1, 7: 1, 8: 1}
