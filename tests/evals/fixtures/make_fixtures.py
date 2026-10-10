"""Draws the synthetic photo fixtures for the eval suite, and writes its one PDF. Nothing here is
a real photo, a real letter or real personal data: the school, the people and the addresses are
made up. Pillow is not a project dependency; run it only to regenerate:

    uv run --with pillow python tests/evals/fixtures/make_fixtures.py
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parents[2]))

from tests.helpers import text_pdf  # noqa: E402

INK, PAPER = (25, 25, 25), (250, 249, 244)


def font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.load_default(size=size)


def receipt(name: str, shop: str, lines: list[tuple[str, str]], footer: list[tuple[str, str]],
            printed: str = "06/10/2026  11:42") -> None:
    height = 190 + 34 * (len(lines) + len(footer))
    image = Image.new("RGB", (460, height), PAPER)
    draw = ImageDraw.Draw(image)
    draw.text((230, 28), shop, font=font(28), fill=INK, anchor="mm")
    draw.text((230, 62), "Example Road, Sampletown", font=font(15), fill=INK, anchor="mm")
    draw.text((230, 84), printed, font=font(15), fill=INK, anchor="mm")
    y = 120
    for label, price in [*lines, ("", ""), *footer]:
        draw.text((28, y), label, font=font(20), fill=INK)
        draw.text((432, y), price, font=font(20), fill=INK, anchor="ra")
        y += 34
    draw.text((230, height - 26), "THANK YOU FOR SHOPPING WITH US", font=font(14), fill=INK, anchor="mm")
    image.save(HERE / name, optimize=True)


def torn_receipt(name: str, shop: str, printed: str, lines: list[tuple[str, str]], smudged: str) -> None:
    """A receipt torn off below its last line, so it shows no total, with one price under a stain."""
    height = 150 + 34 * len(lines)
    image = Image.new("RGB", (460, height), (60, 60, 60))
    draw = ImageDraw.Draw(image)
    edge = [(0, 0), (460, 0), (460, height - 22)]
    edge += [(460 - 23 * step, height - (8 if step % 2 else 26)) for step in range(1, 20)] + [(0, height - 18)]
    draw.polygon(edge, fill=PAPER)
    draw.text((230, 28), shop, font=font(28), fill=INK, anchor="mm")
    draw.text((230, 62), "Example Road, Sampletown", font=font(15), fill=INK, anchor="mm")
    draw.text((230, 84), printed, font=font(15), fill=INK, anchor="mm")
    y = 120
    for label, price in lines:
        draw.text((28, y), label, font=font(20), fill=INK)
        if label == smudged:
            draw.ellipse((352, y - 8, 448, y + 32), fill=(96, 66, 40))
        else:
            draw.text((432, y), price, font=font(20), fill=INK, anchor="ra")
        y += 34
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


def page(name: str, heading: list[str], body: list[str], *, width: int = 760) -> None:
    """A letter or an invite on one sheet: a centred heading, then lines; an empty line is a gap."""
    height = 70 + 40 * len(heading) + sum(34 if line else 18 for line in body) + 40
    image = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(image)
    y = 44
    for n, line in enumerate(heading):
        draw.text((width / 2, y), line, font=font(30 if n == 0 else 18), fill=INK, anchor="mm")
        y += 40
    y += 16
    for line in body:
        if line:
            draw.text((44, y), line, font=font(21), fill=INK)
        y += 34 if line else 18
    image.save(HERE / name, optimize=True)


AUTUMN_DATES = [
    "Sunnyfield Primary School, Example Lane, Sampletown",
    "Autumn term dates for parents and carers",
    "",
    "Tuesday 13 October, 9:15am: Harvest assembly in the school hall. Parents welcome.",
    "Friday 16 October: Non-uniform day. Please bring 1 pound for the school fund.",
    "Monday 26 October to Friday 30 October: Half term. School is closed.",
    "Wednesday 12 November, 4pm to 6pm: Parents' evening in the classrooms.",   # the 12th is a Thursday
    "",
    "Mrs A. Example, Headteacher",
]


if __name__ == "__main__":
    page("letter_trip.png", ["SUNNYFIELD PRIMARY SCHOOL", "Example Lane, Sampletown"], [
        "5 October 2026", "",
        "Dear parents and carers of Year 3,", "",
        "Year 3 will visit the Science Museum on Thursday 15 October.",
        "The coach leaves school at 8:45am and we will be back by 3:30pm.",
        "Children need a packed lunch and a waterproof coat.", "",
        "Please return the permission slip with 12 pounds by Friday 9 October.", "",
        "Friday 23 October is an INSET day: school is closed to pupils.", "",
        "Mrs A. Example, Class Teacher",
    ])
    page("invite_party.png", ["YOU'RE INVITED!"], [
        "Dear Tobi,", "",
        "Please come to Zara's 7th birthday party", "",
        "Saturday 17 October, 2pm to 4pm",
        "Jungle Jim's Soft Play, 12 Example Road, Sampletown", "",
        "RSVP to Zara's mum by Monday 12 October",
    ], width=640)
    (HERE / "autumn_dates.pdf").write_bytes(text_pdf(AUTUMN_DATES))
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
    receipt("receipt_corner_shop.png", "HILLTOP GROCERS", [
        ("ORGANIC UNSALTED BUTTER 250G", "2.40"), ("LONG GRAIN RICE 2KG", "3.15"), ("LARGE WHITE ONIONS 1KG", "1.05"),
        ("COCONUT MILK 400ML", "1.10"), ("KITCHEN FOIL 10M", "1.75"),
    ], [("MEMBER DISCOUNT", "-0.50"), ("TOTAL", "8.95"), ("CONTACTLESS", "8.95")])
    torn_receipt("receipt_torn.png", "HILLTOP GROCERS", "28/09/2026  16:05", [
        ("OLIVE OIL 500ML", "4.50"), ("TINNED TOMATOES X4", "2.20"), ("WASHING UP LIQUID", "1.35"),
        ("PORRIDGE OATS 1KG", "1.80"),
    ], smudged="WASHING UP LIQUID")
    shelves("fridge.png", "FRIDGE", [
        [("MILK", (245, 245, 250)), ("MILK", (245, 245, 250)), ("ORANGE\nJUICE", (255, 190, 90))],
        [("EGGS\n6", (235, 215, 180)), ("BUTTER", (250, 235, 150)), ("CHEDDAR\nCHEESE", (245, 200, 110))],
    ])
    shelves("freezer.png", "FREEZER", [
        [("CHICKEN\nTHIGHS", (240, 200, 195)), ("GARDEN\nPEAS", (170, 215, 160))],
        [("FISH\nFINGERS", (235, 205, 150)), ("VANILLA\nICE CREAM", (250, 245, 225))],
    ])
