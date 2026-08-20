"""Render a vendor invoice as a document, the way one actually arrives.

The verification step is the heart of the product: did the vendor really stop
billing? Checking that against a tidy JSON record would be checking our own
homework. Vendors send documents, so the demo world sends documents too, and
the agent has to read one.
"""

import random
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import DATA_DIR

INVOICES = DATA_DIR / "invoices"


def _font(size: int, bold: bool = False):
    names = ("arialbd.ttf", "segoeuib.ttf") if bold else ("arial.ttf", "segoeui.ttf")
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def render_invoice(vendor: str, amount: float, memo: str, invoice_date: str,
                   currency: str = "USD", number: str | None = None) -> Path:
    """Draw a plausible vendor invoice and save it as a JPEG."""
    W, H = 1240, 1754
    img = Image.new("RGB", (W, H), (255, 255, 255))
    d = ImageDraw.Draw(img)
    ink, grey, rule = (26, 26, 24), (110, 107, 99), (222, 220, 214)
    number = number or f"INV-{invoice_date.replace('-', '')}-{random.randint(100, 999)}"

    d.rectangle([0, 0, W, 14], fill=(31, 58, 95))
    d.text((80, 92), vendor, font=_font(40, True), fill=ink)
    d.text((80, 146), "billing@" + vendor.replace(" ", "").lower() + ".test",
           font=_font(20), fill=grey)
    d.text((W - 80, 92), "INVOICE", font=_font(40, True), fill=(31, 58, 95), anchor="ra")
    d.text((W - 80, 148), number, font=_font(20), fill=grey, anchor="ra")

    d.line([80, 210, W - 80, 210], fill=rule, width=2)
    d.text((80, 246), "BILL TO", font=_font(15, True), fill=grey)
    d.text((80, 274), "Meridian Retail LLP", font=_font(24), fill=ink)
    d.text((80, 308), "Finance Department", font=_font(19), fill=grey)
    d.text((W - 80, 246), "INVOICE DATE", font=_font(15, True), fill=grey, anchor="ra")
    d.text((W - 80, 274), invoice_date, font=_font(24), fill=ink, anchor="ra")
    d.text((W - 80, 312), "DUE ON RECEIPT", font=_font(15, True), fill=grey, anchor="ra")

    y = 400
    d.rectangle([80, y, W - 80, y + 44], fill=(244, 245, 247))
    d.text((100, y + 13), "DESCRIPTION", font=_font(15, True), fill=grey)
    d.text((W - 100, y + 13), "AMOUNT", font=_font(15, True), fill=grey, anchor="ra")
    y += 60
    sym = {"USD": "$", "INR": "INR ", "GBP": "£", "EUR": "€"}.get(currency, currency + " ")

    d.text((100, y), memo, font=_font(21), fill=ink)
    d.text((W - 100, y), f"{sym}{amount:,.2f}", font=_font(21), fill=ink, anchor="ra")
    y += 52
    d.line([80, y, W - 80, y], fill=rule, width=1)

    y += 40
    d.text((W - 300, y), "Subtotal", font=_font(20), fill=grey)
    d.text((W - 100, y), f"{sym}{amount:,.2f}", font=_font(20), fill=ink, anchor="ra")
    y += 38
    d.text((W - 300, y), "Tax", font=_font(20), fill=grey)
    d.text((W - 100, y), f"{sym}0.00", font=_font(20), fill=ink, anchor="ra")
    y += 46
    d.line([W - 320, y, W - 80, y], fill=ink, width=2)
    y += 18
    d.text((W - 320, y), "TOTAL DUE", font=_font(22, True), fill=ink)
    d.text((W - 100, y), f"{sym}{amount:,.2f}", font=_font(26, True),
           fill=(31, 58, 95), anchor="ra")

    d.text((80, H - 210), "Payment terms: net 0. Late payments accrue interest at 1.5% per month.",
           font=_font(17), fill=grey)
    d.text((80, H - 176), "Questions about this invoice? billing@" +
           vendor.replace(" ", "").lower() + ".test", font=_font(17), fill=grey)
    d.line([80, H - 130, W - 80, H - 130], fill=rule, width=1)
    d.text((W // 2, H - 100), f"{vendor} — generated for the Reaper demonstration environment",
           font=_font(15), fill=(178, 176, 170), anchor="ma")

    # A light scan pass: paper is never perfectly flat or perfectly clean.
    img = img.rotate(random.uniform(-0.5, 0.5), expand=False, resample=Image.BICUBIC,
                     fillcolor=(255, 255, 255))
    px = img.load()
    for _ in range(24000):
        x, yy = random.randrange(W), random.randrange(H)
        r, g, b = px[x, yy]
        n = random.randint(-7, 3)
        px[x, yy] = (max(0, r + n), max(0, g + n), max(0, b + n))
    img = img.filter(ImageFilter.GaussianBlur(0.4))

    INVOICES.mkdir(parents=True, exist_ok=True)
    path = INVOICES / f"invoice-{vendor.replace(' ', '_')}.jpg"
    img.save(path, quality=88)
    return path
