"""Makes pretend 'Claude Design' PDFs with typical problems, for testing the checker."""
import asyncio
from pathlib import Path

from pypdf import PdfReader, PdfWriter

from kdp_agent.interior import html_to_pdf

FISH = '<path d="M150 400 Q400 150 650 400 Q400 650 150 400 Z M650 400 L780 300 L780 500 Z" fill="none" stroke="#000" stroke-width="{w}"/><circle cx="280" cy="370" r="30" fill="none" stroke="#000" stroke-width="{w}"/>'
STAR = '<polygon points="400,60 490,300 740,300 540,450 610,700 400,560 190,700 260,450 60,300 310,300" fill="{fill}" stroke="#000" stroke-width="{w}"/>'
CAR = '<rect x="120" y="380" width="560" height="160" rx="30" fill="none" stroke="#000" stroke-width="{w}"/><rect x="240" y="260" width="300" height="130" rx="20" fill="none" stroke="#000" stroke-width="{w}"/><circle cx="250" cy="560" r="60" fill="#fff" stroke="#000" stroke-width="{w}"/><circle cx="550" cy="560" r="60" fill="#fff" stroke="#000" stroke-width="{w}"/>'


def picture(svg_inner, full_bleed=False):
    size = "100%" if full_bleed else "80%"
    return f'<section class="p"><svg viewBox="0 0 800 800" style="width:{size};height:{size}">{svg_inner}</svg></section>'


async def make(path: Path, w=8.5, h=11, problems=True, copies=1):
    pages = ['<section class="p"><h1>Ocean Friends</h1><p>A colouring book</p></section>']
    pages.append(picture(FISH.format(w=8)))
    pages.append(picture(STAR.format(w=8, fill="none")))
    pages.append(picture(CAR.format(w=8)))
    pages.append(picture(STAR.format(w=8, fill="#999" if problems else "none")))   # grey fill
    pages.append(picture(FISH.format(w=0.6 if problems else 8)))                     # thin lines
    pages.append(picture(CAR.format(w=8), full_bleed=problems).replace("<svg", "<div style=\"position:absolute;inset:0.1in;border:3pt solid #000\"></div><svg") if problems else picture(CAR.format(w=8)))  # into margins
    pages = pages[:1] + pages[1:] * copies
    html = f"""<html><head><style>@page{{size:{w}in {h}in;margin:0}} body{{margin:0}}
      .p{{position:relative;width:{w}in;height:{h}in;display:flex;flex-direction:column;align-items:center;justify-content:center;page-break-after:always;overflow:hidden}}
      h1{{font-size:48pt;font-family:sans-serif}}</style></head><body>{''.join(pages)}</body></html>"""
    raw = path.with_suffix(".raw.pdf")
    await html_to_pdf(html, raw, w, h)
    reader, writer = PdfReader(str(raw)), PdfWriter()
    for p in reader.pages:
        writer.add_page(p)
    if problems:
        writer.add_outline_item("Start", 0)
        writer.add_metadata({"/Author": "Someone", "/Title": "Draft"})
    with open(path, "wb") as f:
        writer.write(f)
    raw.unlink()
    return path


if __name__ == "__main__":
    import sys
    asyncio.run(make(Path(sys.argv[1])))
