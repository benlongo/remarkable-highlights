from enum import Enum
from functools import partial
from itertools import chain, groupby
from operator import itemgetter

import click
import fitz
from shapely import ops
from shapely.geometry import MultiPolygon, Polygon, box

from .parsing import content_contains_highlight, highlighter_lines
import os
import textwrap
from collections import defaultdict

# TODO: Warn if highlights appear 'sloppy' mean diff from 100% area coverage on words?

WordSelectionMethod = Enum("WordSelectionMethod", "CENTROID AREA_RATIO")
DEFAULT_AREA_RATIO = 0.5  # TODO: Allow CLI control of this


def word_is_highlighted(method, highlight_poly, word_box):
    """Check if the word box is highlighted by the given highlight polygon with the provided method.

    The centroid method is more robust against vertically drifting lines, especially as the text
    boxes tend to overlap and that can lead to more area that it would appear being selected.
    """
    if method == WordSelectionMethod.CENTROID:
        return highlight_poly.contains(word_box.centroid)
    if method == WordSelectionMethod.AREA_RATIO:
        overlap = highlight_poly.intersection(word_box)
        overlap_ratio = overlap.area / word_box.area
        return overlap_ratio > DEFAULT_AREA_RATIO
    raise ValueError(f"Unknown word selection method: {method}")


HighlightType = Enum("HighlightType", "TEXTUAL SELECTION")


def classify_highlight(highlight_poly, clipping_area_threshold):
    """Determine if the highlight polygon is a textual selection, or a clipping selection.

    If the polygon has precisely one interior ring, and is of sufficient area we will consider it
    to be a clipping selection, otherwise it will be classified as textual.
    """
    if len(highlight_poly.interiors) == 1:
        # TODO: The CropBox area can vary significantly.
        #       Maybe use a ratio of the interior to the crop box area?
        if Polygon(highlight_poly.interiors[0]).area > clipping_area_threshold:
            return HighlightType.SELECTION
    return HighlightType.TEXTUAL


def classify_highlights(polys, clipping_area_threshold):
    """Group the highlight polygons based on their classification."""
    text_highlights = []
    selection_highlights = []
    for poly in polys:
        highlight_type = classify_highlight(poly, clipping_area_threshold)
        if highlight_type == HighlightType.TEXTUAL:
            text_highlights.append(poly)
        elif highlight_type == HighlightType.SELECTION:
            selection_highlights.append(poly)
    return text_highlights, selection_highlights


def coordinate_transformer(page):
    """Provide a transformer that maps PDF graphics coordinates to Fitz coordinates (and back).

    PyMuPDF uses y=0 for the top of the page, but the PDF format (and our debug plotting code) use
    y=0 for the bottom of the page. We choose to make the PDFs into fitz coordinates because
    shapely makes such transformations very simple to express.
    """

    def to_fitz_coord(x, y):
        # PyMuPDF uses y=0 for the top, but pdf uses y=0 for the bottom
        return x, page.CropBox[-1] - y

    return partial(ops.transform, to_fitz_coord)


def extract_highlight_lines(doc, page):
    """Extract the highlighter line geometries from the page.

    All the real work is getting done in the parsing code, but it is important that we apply the
    coordinate transform immediately after the lines are extracted so we never have to deal with
    different coordinate spaces.
    """
    polys = []
    for xref in page._getContents():  # pylint: disable=protected-access
        if not doc.isStream(xref):
            continue
        content = doc.xrefStream(xref)
        if content_contains_highlight(content):
            polys.extend(highlighter_lines(content))

    to_fitz_coords = coordinate_transformer(page)
    return [to_fitz_coords(poly) for poly in polys]


def normalize_polys(geometry):
    """Turn MultiPolygons into their constituent geometries, and Polygons into singleton lists.

    When we take the union of all the highlight lines, we may get 1 or n distinct polygons. This
    is used to treat both cases identically.
    """
    if geometry.geom_type == "MultiPolygon":
        return geometry.geoms
    if geometry.geom_type == "Polygon":
        return [geometry]
    raise ValueError(f"Unexpected geometry type: {geometry.geom_type}")


def merge_highlight_lines(lines):
    """Take the union of the line geometries and break them into distinct polygons."""
    union = ops.unary_union(lines)
    return normalize_polys(union)


def extract_clips(selection_polys, page, clip_zoom):
    """Get Pixmaps for each of the given selection polygons."""
    for selection_poly in selection_polys:
        # TODO: Enable masking by the interior of the poly
        selection_rect = fitz.Rect(*selection_poly.exterior.bounds)
        # TODO: Enable rendering without the yellow lines
        yield page.getPixmap(
            clip=selection_rect.round(), matrix=fitz.Matrix(clip_zoom, clip_zoom)
        )


def extract_text_highlights(text_highlights, page, word_selection_method, max_skip_len):
    """Extract the highlighted text with the provided method, allowing up to max_skip_len skips.

    If a highlight path wanders for a couple of words but then returns, it is annoying to have two
    distinct textual selections and also not know what the intermediate words are. Thus, we allow
    for the case where a couple words are skipped. It is unlikely that two intentionally distinct
    highlights are only seperated by a couple of words, and if they are it will cause much less
    harm to include more words than to exclude them.

    The algorithm to compute the selections is greatly complicated by this requirement however.
    It is certainly possible to refactor this into a single imperative loop, but after messing up
    the implementation with that approach several times I resorted to a somewhat ugly, but more
    easy to reason about implementation based on groupby.
    """
    text_highlight = MultiPolygon(text_highlights)
    # NOTE: If the skip logic proves to be finnicky with adjacent, yet distinct polys, it is
    # possible to instead look at each polygon by itself instead of merging them all into one
    # mega MultiPolygon.
    words = page.getText("words")
    word_boxes = [
        (word, box(x0, y0, x1, y1)) for x0, y0, x1, y1, word, _, _, _ in words
    ]

    is_highlighted = partial(word_is_highlighted, word_selection_method, text_highlight)

    runs = [
        (is_selected, list(map(itemgetter(0), words)))
        for is_selected, words in groupby(
            word_boxes, key=lambda t: is_highlighted(t[1])
        )
    ]

    marked_skips = [
        (is_selected or len(run) <= max_skip_len, run) for is_selected, run in runs
    ]

    merged_runs = [
        list(chain.from_iterable(map(itemgetter(1), runs_to_merge)))
        for is_selected, runs_to_merge in groupby(marked_skips, key=itemgetter(0))
        if is_selected
    ]

    return [" ".join(run) for run in merged_runs]


def extract_highlights(
    doc,
    max_skip_len,
    word_selection_method,
    clip_area_threshold,
    clip_zoom,
    debug_render,
):
    page_text_highlights = defaultdict(list)
    page_clips = defaultdict(list)
    for page in doc:
        highlight_lines = extract_highlight_lines(doc, page)
        if not highlight_lines:
            continue

        highlight_polys = merge_highlight_lines(highlight_lines)

        text_highlight_polys, selection_highlight_polys = classify_highlights(
            highlight_polys, clip_area_threshold
        )

        if debug_render:
            # pylint: disable=import-outside-toplevel
            import matplotlib.pyplot as plt

            debug_page(page, text_highlight_polys, selection_highlight_polys)
            plt.show()

        if selection_highlight_polys:
            selection_highlights = extract_clips(
                selection_highlight_polys, page, clip_zoom
            )
            page_clips[page.number + 1].extend(selection_highlights)

        if text_highlight_polys:
            text_highlights = extract_text_highlights(
                text_highlight_polys,
                page,
                WordSelectionMethod[word_selection_method],
                max_skip_len,
            )
            page_text_highlights[page.number + 1].extend(text_highlights)

    return dict(page_text_highlights), dict(page_clips)


def debug_page(page, text_highlights, selection_highlights):
    """Plot the word boxes and highlights for a particular page.

    After calling this, you must still use plt.show() to see the result! We don't call it here to
    avoid blocking on each page, although that is still possible to do if you want.
    """
    # pylint: disable=import-outside-toplevel,too-many-locals
    import matplotlib.pyplot as plt
    import descartes

    _, _, page_width, page_height = page.CropBox
    fig = plt.figure()
    ax = fig.gca()
    ax.set_xlim((0, page_width))
    ax.set_ylim((0, page_height))
    ax.set_aspect("equal")

    flip = coordinate_transformer(page)

    words = page.getText("words")
    word_boxes = [box(x0, y0, x1, y1) for x0, y0, x1, y1, word, _, _, _ in words]
    for word_box in word_boxes:
        word_patch = descartes.PolygonPatch(flip(word_box))
        ax.add_patch(word_patch)

    for text_highlight in text_highlights:
        highlight_patch = descartes.PolygonPatch(
            flip(text_highlight), fc="yellow", alpha=0.5
        )
        ax.add_patch(highlight_patch)

    for selection_highlight in selection_highlights:
        selection_patch = descartes.PolygonPatch(
            flip(selection_highlight), fc="orange", alpha=0.7
        )
        ax.add_patch(selection_patch)


@click.command()
@click.argument("filename", type=click.Path(exists=True, dir_okay=False, readable=True))
@click.option(
    "--out",
    type=click.Path(),
    default=None,
    help="Directory for output. By default the filname without the PDF extension is used.",
)
@click.option(
    "--max-skip-len",
    default=3,
    help="Number of words that can be skipped without being excluded from a highlight.",
)
@click.option(
    "--word-selection-method",
    type=click.Choice([m.name for m in WordSelectionMethod], case_sensitive=False),
    default=WordSelectionMethod.CENTROID.name,
    help="How to determine if a particular word has been highlighted.",
)
@click.option(
    "--clip-area-threshold",
    default=1000,
    help="How much area a clipping must enclose to be considered a clipping.",
)
@click.option(
    "--clip-zoom",
    default=3,
    help="How much to zoom in for the rendering of a clip. Increase for better quality.",
)
@click.option(
    "--yes",
    default=False,
    is_flag=True,
    help="WARNING: Potentially destructive! Answer yes to any interactive prompts.",
)
@click.option(
    "--debug-render",
    is_flag=True,
    default=False,
    help="Render the highlights and text boxes for each page.",
)
def main(
    filename,
    out,
    max_skip_len,
    word_selection_method,
    clip_area_threshold,
    clip_zoom,
    yes,
    debug_render,
):
    """Extract textual highlights and clippings from FILENAME."""

    file_basename, ext = os.path.splitext(filename)
    if ext == "" and out is None:
        click.echo(
            "If the pdf has no extension, the output directory must be manually specified.",
            err=True,
        )
        raise click.Abort()
    if ext != ".pdf" and not yes:
        click.confirm(
            f"Unexpected extension: {ext} - do you want to continue?", abort=True
        )
    if out is None:
        out = file_basename + "-highlights"
    if os.path.isdir(out):
        if not yes:
            click.confirm(
                f"Output directory {out} already exists, overwrite any contents?",
                abort=True,
            )
    else:
        os.mkdir(out)

    highlights_p = os.path.join(out, "highlights.txt")

    with fitz.open(filename) as doc, open(highlights_p, "w") as highlights_f:
        page_text_highlights, page_clips = extract_highlights(
            doc,
            max_skip_len,
            word_selection_method,
            clip_area_threshold,
            clip_zoom,
            debug_render,
        )

        for page_num, text_highlights in page_text_highlights.items():
            highlights_f.write(f"page {page_num}:\n")
            for highlight in text_highlights:
                block = textwrap.indent(textwrap.fill(highlight) + "\n\n", " " * 4)
                highlights_f.write(block)

        for page_num, clips in page_clips.items():
            for i, clip in enumerate(clips):
                png_path = os.path.join(out, f"p{page_num}-c{i}.png")
                clip.writePNG(png_path)


if __name__ == "__main__":
    main()  # pylint: disable=no-value-for-parameter
