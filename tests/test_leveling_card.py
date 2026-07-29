"""Unit tests for cogs.leveling.fit_card_background - the rank card background resize step.

Run with: py -3 -m unittest tests.test_leveling_card -v
(stdlib unittest, no pytest dependency needed - this repo doesn't have one installed.)
"""
import unittest

from PIL import Image

from cogs.leveling import fit_card_background

TARGET_SIZE = (920, 240)  # matches base_w, base_h in cogs/leveling.py's /level command


class FitCardBackgroundSizeTests(unittest.TestCase):
    """Whatever the source aspect ratio, the output must always be exactly 920x240."""

    def test_various_aspect_ratios_all_produce_exact_target_size(self):
        source_sizes = [
            (920, 240),   # already the target ratio (~3.83:1) - should pass through untouched
            (100, 400),   # tall portrait (1:4)
            (2000, 100),  # very wide landscape (20:1)
            (500, 500),   # square (1:1)
            (240, 920),   # target size rotated 90 degrees (portrait 1:3.83)
            (1, 1),       # degenerate 1x1 source
        ]
        for size in source_sizes:
            with self.subTest(source_size=size):
                src = Image.new("RGBA", size, color=(200, 50, 50, 255))
                result = fit_card_background(src, TARGET_SIZE)
                self.assertEqual(result.size, TARGET_SIZE)


class FitCardBackgroundCropNotStretchTests(unittest.TestCase):
    """Confirms the source is center-cropped (content discarded), not squeezed to fit -
    a marker placed near an edge of the source must NOT survive into the output, because
    a plain stretch-resize would keep it (thinned/distorted) while a cover-crop discards it."""

    def test_tall_source_crops_top_and_bottom_instead_of_squeezing(self):
        # 100 wide x 400 tall, top 10 rows pure red, rest pure blue.
        src = Image.new("RGBA", (100, 400), color=(0, 0, 255, 255))
        for y in range(10):
            for x in range(100):
                src.putpixel((x, y), (255, 0, 0, 255))

        result = fit_card_background(src, TARGET_SIZE)
        self.assertEqual(result.size, TARGET_SIZE)

        # A stretch-resize would still show red somewhere in the top rows of the output
        # (compressed but present). A cover-crop centers the crop window vertically and,
        # for this source/target ratio pair, crops the height down to ~26px around the
        # vertical center (row 200 of 400) - the top red marker rows never survive.
        top_row_colors = {result.getpixel((x, 0)) for x in range(0, TARGET_SIZE[0], 50)}
        self.assertTrue(
            all(color[0] < 255 or color[2] > 0 for color in top_row_colors),
            f"expected the red top-edge marker to be cropped away, but found it in the output: {top_row_colors}",
        )

    def test_wide_source_crops_left_and_right_instead_of_squeezing(self):
        # 2000 wide x 100 tall, left 10 columns pure red, rest pure blue.
        src = Image.new("RGBA", (2000, 100), color=(0, 0, 255, 255))
        for x in range(10):
            for y in range(100):
                src.putpixel((x, y), (255, 0, 0, 255))

        result = fit_card_background(src, TARGET_SIZE)
        self.assertEqual(result.size, TARGET_SIZE)

        # Cover-crop centers the crop window horizontally and crops the width down from
        # 2000 to ~383px around the horizontal center (column 1000 of 2000) - the left
        # red marker columns never survive into the output.
        left_col_colors = {result.getpixel((0, y)) for y in range(0, TARGET_SIZE[1], 40)}
        self.assertTrue(
            all(color[0] < 255 or color[2] > 0 for color in left_col_colors),
            f"expected the red left-edge marker to be cropped away, but found it in the output: {left_col_colors}",
        )


if __name__ == "__main__":
    unittest.main()
