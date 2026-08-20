"""Render a paper contract as if photographed on a desk.

Used to exercise (and demo) the multimodal intake path: no text layer exists in
a photograph, so Gemini vision has to read the clause off the pixels.
"""
import random
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "contracts" / "northwind-facilities-photo.jpg"

BODY = [
    ("FACILITIES SERVICES AGREEMENT", "title"),
    ("", ""),
    ("This Facilities Services Agreement is entered into between Northwind", ""),
    ("Facilities Ltd. (\"Contractor\") and Meridian Retail LLP (\"Client\").", ""),
    ("", ""),
    ("1. SERVICES.  Contractor shall provide scheduled maintenance, janitorial", ""),
    ("and grounds services at the Client premises, billed at INR 96,000 per", ""),
    ("month in advance.", ""),
    ("", ""),
    ("2. TERM.  The initial term commences on 1 April 2026 and expires on", ""),
    ("31 March 2027.", ""),
    ("", ""),
    ("3. RENEWAL.  This Agreement shall renew automatically for successive", ""),
    ("twelve (12) month terms unless either party delivers written notice of", ""),
    ("non-renewal not less than forty-five (45) days prior to the end of the", ""),
    ("then-current term, addressed to contracts@northwindfacilities.test.", ""),
    ("", ""),
    ("4. PRICE ADJUSTMENT.  Fees may be revised once per renewal term upon", ""),
    ("sixty days written notice.", ""),
    ("", ""),
    ("5. GOVERNING LAW.  This Agreement is governed by the laws of India.", ""),
]

W, H = 1500, 2000
page = Image.new("RGB", (W, H), (252, 250, 244))
d = ImageDraw.Draw(page)

def font(sz, bold=False):
    for name in (("georgiab.ttf", "timesbd.ttf") if bold else ("georgia.ttf", "times.ttf")):
        try:
            return ImageFont.truetype(name, sz)
        except OSError:
            continue
    return ImageFont.load_default(sz)

y = 190
for text, kind in BODY:
    if kind == "title":
        f = font(52, bold=True)
        d.text(((W - d.textlength(text, font=f)) / 2, y), text, font=f, fill=(28, 26, 24))
        y += 96
    else:
        f = font(34)
        d.text((150, y), text, font=f, fill=(34, 32, 30))
        y += 52

# paper grain and a soft fold shadow down the middle
px = page.load()
for _ in range(90000):
    x, yy = random.randrange(W), random.randrange(H)
    r, g, b = px[x, yy]
    n = random.randint(-9, 5)
    px[x, yy] = (max(0, r + n), max(0, g + n), max(0, b + n))
shade = Image.new("L", (W, H), 0)
ImageDraw.Draw(shade).rectangle([W // 2 - 60, 0, W // 2 + 60, H], fill=42)
page = Image.composite(Image.new("RGB", (W, H), (196, 190, 178)), page,
                       shade.filter(ImageFilter.GaussianBlur(70)))

# desk, perspective tilt, camera softness and a warm light gradient
page = page.rotate(-2.4, expand=True, resample=Image.BICUBIC, fillcolor=(120, 96, 70))
desk = Image.new("RGB", (page.width + 220, page.height + 220), (122, 96, 68))
grain = Image.effect_noise((desk.width, desk.height), 16).convert("L")
desk = Image.composite(Image.new("RGB", desk.size, (98, 76, 54)), desk, grain)
desk.paste(page, (110, 110))
light = Image.linear_gradient("L").resize(desk.size).rotate(28, resample=Image.BICUBIC)
desk = Image.composite(desk, Image.new("RGB", desk.size, (255, 246, 226)),
                       light.point(lambda v: 210 - v // 3))
desk = desk.filter(ImageFilter.GaussianBlur(0.9)).resize((1200, int(1200 * desk.height / desk.width)))
OUT.parent.mkdir(parents=True, exist_ok=True)
desk.save(OUT, quality=86)
print("wrote", OUT, desk.size, f"{OUT.stat().st_size // 1024} KB")
