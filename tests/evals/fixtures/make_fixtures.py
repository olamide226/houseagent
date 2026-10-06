"""Draws the synthetic photo fixtures for the eval suite. Nothing here is a real photo or
real personal data. Pillow is not a project dependency; run it only to regenerate:

    uv run --with pillow python tests/evals/fixtures/make_fixtures.py
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
INK, PAPER = (25, 25, 25), (250, 249, 244)


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def receipt(name: str, shop: str, lines: list[tuple[str, str]], footer: list[tuple[str, str]]) -> None:
    height = 190 + 34 * (len(lines) + len(footer))
    image = Image.new("RGB", (460, height), PAPER)
    draw = ImageDraw.Draw(image)
    draw.text((230, 28), shop, font=font(28), fill=INK, anchor="mm")
    draw.text((230, 62), "Example Road, Sampletown", font=font(15), fill=INK, anchor="mm")
    draw.text((230, 84), "06/10/2026  11:42", font=font(15), fill=INK, anchor="mm")
    y = 120
    for label, price in [*lines, ("", ""), *footer]:
        draw.text((28, y), label, font=font(20), fill=INK)
        draw.text((432, y), price, font=font(20), fill=INK, anchor="ra")
        y += 34
    draw.text((230, height - 26), "THANK YOU FOR SHOPPING WITH US", font=font(14), fill=INK, anchor="mm")
    image.save(HERE / name, optimize=True)


def shelves(name: str, title: str, rows: list[list[tuple[str, tuple[int, int, int]]]]) -> None:
    """An open fridge or freezer seen from the front: each shelf holds labelled packs."""
    image = Image.new("RGB", (520, 150 + 150 * len(rows)), (228, 236, 241))
    draw = ImageDraw.Draw(image)
    draw.rectangle((14, 14, 506, image.height - 14), outline=(120, 130, 140), width=6)
    draw.text((260, 44), title, font=font(18), fill=(90, 100, 110), anchor="mm")
    for index, row in enumerate(rows):
        floor = 210 + 150 * index
        draw.line((20, floor, 500, floor), fill=(150, 160, 170), width=5)
        slot = 460 // len(row)
        for position, (label, colour) in enumerate(row):
            left = 34 + slot * position
            draw.rounded_rectangle((left, floor - 104, left + slot - 18, floor - 3), radius=8, fill=colour,
                                   outline=INK, width=2)
            draw.multiline_text((left + (slot - 18) / 2, floor - 54), label, font=font(17), fill=INK,
                                anchor="mm", align="center")
    image.save(HERE / name, optimize=True)


if __name__ == "__main__":
    receipt("receipt_supermarket.png", "FRESHWAY SUPERMARKET", [
        ("SEMI SKIMMED MILK 2 PINTS", "1.65"), ("FREE RANGE EGGS X12", "2.95"), ("WHOLEMEAL BREAD", "1.20"),
    ], [("TOTAL", "5.80"), ("CARD", "5.80")])
    receipt("receipt_market.png", "RYE LANE FOOD MARKET", [
        ("PLANTAIN X4", "2.00"), ("YAM", "3.50"), ("PALM OIL 1L", "4.99"), ("SCOTCH BONNET", "1.00"),
    ], [("TOTAL", "11.49"), ("CASH", "20.00"), ("CHANGE", "8.51")])
    receipt("receipt_big_shop.png", "FRESHWAY SUPERMARKET", [
        ("BASMATI RICE 5KG", "9.50"), ("CHICKEN THIGHS 1KG", "4.75"), ("BUTTER 250G", "1.99"),
        ("TOMATOES X6", "0.95"), ("ONIONS 1KG", "1.10"), ("BLEACH 750ML", "0.85"), ("TOILET ROLL 9PK", "4.25"),
        ("CARRIER BAG", "0.30"),
    ], [("LOYALTY SAVING", "-1.00"), ("TOTAL", "22.69"), ("CARD", "22.69")])
    shelves("fridge.png", "FRIDGE", [
        [("MILK", (245, 245, 250)), ("MILK", (245, 245, 250)), ("ORANGE\nJUICE", (255, 190, 90))],
        [("EGGS\n6", (235, 215, 180)), ("BUTTER", (250, 235, 150)), ("CHEDDAR\nCHEESE", (245, 200, 110))],
    ])
    shelves("freezer.png", "FREEZER", [
        [("CHICKEN\nTHIGHS", (240, 200, 195)), ("GARDEN\nPEAS", (170, 215, 160))],
        [("FISH\nFINGERS", (235, 205, 150)), ("VANILLA\nICE CREAM", (250, 245, 225))],
    ])
