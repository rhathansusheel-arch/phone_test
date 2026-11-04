"""A pretend KDP website for testing the uploader without touching the real KDP.

It uses the same visible labels as KDP and records every action, so tests can check
what the agent typed and clicked (and that it never clicked Publish without approval).
"""

from __future__ import annotations

import html
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class FakeKdp:
    def __init__(self, port: int = 8799):
        self.port = port
        self.books: dict[str, dict] = {}      # id -> {"title", "formats": {fmt: {...fields, status}}}
        self.clicks: list[str] = []
        self.mode = "normal"                   # normal / login / captcha
        self._server = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/en_US"

    def start(self):
        kdp = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body: str, status: int = 200, location: str | None = None):
                data = body.encode()
                self.send_response(status)
                if location:
                    self.send_header("Location", location)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                kdp._get(self)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                form = {k: v if len(v) > 1 else v[0] for k, v in parse_qs(self.rfile.read(length).decode()).items()}
                kdp._post(self, form)

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()

    # ------------------------------------------------------------ pages

    def _page(self, title: str, body: str) -> str:
        return f"""<!doctype html><html><head><title>{title}</title><style>
        body{{font-family:sans-serif;margin:20px}} .modal{{display:none;border:2px solid #333;padding:10px}}
        section{{border:1px solid #ccc;margin:8px 0;padding:8px}}</style></head><body>
        <h1>KDP - {title}</h1>{body}</body></html>"""

    def _guard_mode(self, h) -> bool:
        if self.mode == "login":
            h._send(self._page("Sign-In", '<form><label for="e">Email</label><input id="e">'
                                          '<label for="p">Password</label><input id="p" type="password"></form>'))
            return True
        if self.mode == "captcha":
            h._send(self._page("Robot check", "<p>Enter the characters you see below</p><img alt='captcha'>"))
            return True
        return False

    def _get(self, h):
        url = urlparse(h.path)
        parts = url.path.strip("/").split("/")
        if self._guard_mode(h):
            return
        if url.path.endswith("/bookshelf"):
            return h._send(self._bookshelf())
        # /en_US/title-setup/<fmt>/<id>/<step>
        if len(parts) >= 5 and parts[1] == "title-setup":
            fmt, book_id, step = parts[2], parts[3], parts[4]
            if step == "details":
                return h._send(self._details(fmt, book_id))
            if step == "content":
                return h._send(self._content(fmt, book_id))
            if step == "previewer":
                return h._send(self._page("Previewer", f"""<p>Previewer: 0 issues found. Looks good.</p>
                  <form method="post" action="/en_US/title-setup/{fmt}/{book_id}/previewer">
                  <button type="submit">Approve</button></form>"""))
            if step == "pricing":
                return h._send(self._pricing(fmt, book_id))
        h._send(self._page("Not found", "<p>Not found</p>"), 404)

    def _post(self, h, form: dict):
        parts = urlparse(h.path).path.strip("/").split("/")
        fmt, book_id, step = parts[2], parts[3], parts[4]
        action = form.pop("_action", "save")
        self.clicks.append(f"{fmt}:{step}:{action}")
        if step == "details":
            if book_id == "new":
                book_id = form.pop("_link", "") or f"b{len(self.books) + 1}"
                self.books.setdefault(book_id, {"formats": {}})
            book = self.books[book_id]
            book["title"] = form.get("title", book.get("title"))
            book["formats"].setdefault(fmt, {"status": "DRAFT"}).update(form)
            return h._send("", 303, f"/en_US/title-setup/{fmt}/{book_id}/content")
        rec = self.books[book_id]["formats"][fmt]
        if step == "previewer":
            rec["previewer_approved"] = True
            return h._send("", 303, f"/en_US/title-setup/{fmt}/{book_id}/content")
        rec.update(form)
        if step == "content" and action == "preview":
            return h._send("", 303, f"/en_US/title-setup/{fmt}/{book_id}/previewer")
        if step == "content":
            return h._send("", 303, f"/en_US/title-setup/{fmt}/{book_id}/pricing")
        if step == "pricing":
            if action == "publish":
                rec["status"] = "IN REVIEW"
                return h._send(self._page("Submitted", "<p>Your book is in review. It can take up to 72 hours.</p>"))
            rec["status"] = "DRAFT"
            return h._send("", 303, "/en_US/bookshelf")

    def _bookshelf(self) -> str:
        rows = []
        for book_id, b in self.books.items():
            cells = []
            for fmt, rec in b["formats"].items():
                link = f'<a href="https://www.amazon.com/dp/FAKE{book_id}">View on Amazon</a>' if rec["status"] == "LIVE" else ""
                cells.append(f'<div>{fmt.upper()} {rec["status"]} '
                             f'<a href="/en_US/title-setup/{fmt}/{book_id}/details">Edit {fmt} details</a> '
                             f'<a href="/en_US/title-setup/{fmt}/{book_id}/pricing">Edit {fmt} rights &amp; pricing</a> {link}</div>')
            if "hardcover" not in b["formats"]:
                cells.append(f'<a href="/en_US/title-setup/hardcover/new/details?link={book_id}">Create hardcover</a>')
            rows.append(f'<tr><td>{html.escape(b.get("title", ""))}</td><td>{"".join(cells)}</td></tr>')
        return self._page("Bookshelf", f'<a href="/en_US/title-setup/paperback/new/details">Create</a>'
                                       f'<table>{"".join(rows)}</table>')

    def _details(self, fmt: str, book_id: str) -> str:
        rec = self.books.get(book_id, {}).get("formats", {}).get(fmt, {})
        b = self.books.get(book_id, {})
        val = lambda k: html.escape(str(rec.get(k, b.get(k, "") if k == "title" else "")))
        kw = "".join(f'<input type="text" name="kw{i}" value="{val(f"kw{i}")}">' for i in range(7))
        return self._page(f"{fmt.title()} Details", f"""
        <form method="post" id="f">
          <input type="hidden" name="_link" id="link">
          <script>document.getElementById('link').value=new URLSearchParams(location.search).get('link')||''</script>
          <label for="lang">Language</label><select id="lang" name="language"><option>English</option><option>German</option></select>
          <label for="t">Book Title</label><input id="t" name="title" value="{val('title')}">
          <label for="st">Subtitle (Optional)</label><input id="st" name="subtitle" value="{val('subtitle')}">
          <section>Primary Author or Contributor
            <input name="first" placeholder="First name" value="{val('first')}">
            <input name="last" placeholder="Last name" value="{val('last')}"></section>
          <section><label>Description</label>
            <div contenteditable="true" id="desc" style="border:1px solid #999;min-height:40px">{val('description')}</div>
            <input type="hidden" name="description" id="desch"></section>
          <section>Publishing Rights
            <label><input type="radio" name="rights" value="own">I own the copyright and I hold the necessary publishing rights.</label>
            <label><input type="radio" name="rights" value="pd">This is a public domain work.</label></section>
          <section>Primary Audience: Sexually Explicit Images or Title
            <label><input type="radio" name="explicit" value="yes">Yes</label>
            <label><input type="radio" name="explicit" value="no">No</label>
            Reading age <label for="mn">Minimum</label><select id="mn" name="age_min"><option></option>{''.join(f'<option>{i}</option>' for i in range(1, 19))}</select>
            <label for="mx">Maximum</label><select id="mx" name="age_max"><option></option>{''.join(f'<option>{i}</option>' for i in range(1, 19))}</select></section>
          <label for="pm">Primary marketplace</label><select id="pm" name="marketplace"><option>Amazon.com</option><option>Amazon.co.uk</option></select>
          <section>Categories <button type="button" onclick="document.getElementById('cat').style.display='block'">Choose categories</button>
            <div class="modal" id="cat">
              <span onclick="this.dataset.on=1">Juvenile Nonfiction</span> <span>Activity Books</span> <span>Animals</span> <span>Concepts</span> <span>Transportation</span>
              <label><input type="checkbox" name="cat" value="Coloring">Coloring</label>
              <label><input type="checkbox" name="cat" value="Marine Life">Marine Life</label>
              <label><input type="checkbox" name="cat" value="Fishes">Fishes</label>
              <label><input type="checkbox" name="cat" value="Shapes">Shapes</label>
              <label><input type="checkbox" name="cat" value="Cars & Trucks">Cars &amp; Trucks</label>
              <button type="button" onclick="document.getElementById('cat').style.display='none'">Save categories</button>
            </div></section>
          <section><h3>Keywords</h3>{kw}</section>
          <button type="submit" name="_action" value="save" onclick="document.getElementById('desch').value=document.getElementById('desc').innerText">Save and Continue</button>
        </form>""")

    def _content(self, fmt: str, book_id: str) -> str:
        rec = self.books[book_id]["formats"][fmt]
        isbn = rec.get("isbn", "")
        def c(name, value):
            return "checked" if rec.get(name) == value else ""
        isbn_html = f"<p>ISBN: {isbn}</p>" if isbn else """<button type="button" onclick="document.getElementById('im').style.display='block'">Assign me a free KDP ISBN</button>
           <div class="modal" id="im"><button type="button" onclick="document.getElementById('isbn').value='979-8-000000-00-0';this.parentNode.innerHTML='ISBN: 979-8-000000-00-0'">Assign ISBN</button></div>"""
        sizes = ["6 x 9 in", "8 x 10 in", "8.25 x 11 in", "8.5 x 8.5 in", "8.5 x 11 in"]
        size_btns = "".join(f"""<button type="button" onclick="document.getElementById('trim').value='{s}';document.getElementById('trimshow').innerText='{s}';document.getElementById('tm').style.display='none'">{s}</button>""" for s in sizes)
        def ai_select(kind):
            opts = ["None", "Some sections, with minimal or no editing", "Some sections, with extensive editing",
                    "Entire work, with minimal or no editing", "Entire work, with extensive editing"]
            return (f'<label for="ai{kind}">{kind}</label><select id="ai{kind}" name="ai_{kind.lower()}">'
                    + "".join(f"<option {'selected' if rec.get('ai_' + kind.lower()) == o else ''}>{o}</option>" for o in opts)
                    + f'</select><input type="text" name="ai_{kind.lower()}_tool" value="{html.escape(rec.get("ai_" + kind.lower() + "_tool", ""))}">')
        return self._page(f"{fmt.title()} Content", f"""
        <form method="post">
          <input type="hidden" name="isbn" id="isbn" value="{isbn}">
          <section>ISBN {isbn_html}</section>
          <section>Print Options
            <label><input type="radio" name="ink" value="bw_white" {c("ink", "bw_white")}>Black &amp; white interior with white paper</label>
            <label><input type="radio" name="ink" value="bw_cream" {c("ink", "bw_cream")}>Black &amp; white interior with cream paper</label>
            Trim size: <span id="trimshow">{rec.get('trim', '6 x 9 in')}</span><input type="hidden" name="trim" id="trim" value="{rec.get('trim', '6 x 9 in')}">
            <button type="button" onclick="document.getElementById('tm').style.display='block'">Select a different size</button>
            <div class="modal" id="tm">{size_btns}</div>
            <label><input type="radio" name="bleed" value="no" {c("bleed", "no")}>No Bleed</label>
            <label><input type="radio" name="bleed" value="yes" {c("bleed", "yes")}>Bleed (PDF only)</label>
            <label><input type="radio" name="finish" value="matte" {c("finish", "matte")}>Matte</label>
            <label><input type="radio" name="finish" value="glossy" {c("finish", "glossy")}>Glossy</label></section>
          <section>Manuscript
            <input type="file" id="mf" style="display:none" onchange="document.getElementById('mn').value=this.files[0].name;document.getElementById('ms').innerText='Manuscript uploaded successfully'">
            <input type="hidden" name="manuscript" id="mn" value="{rec.get('manuscript', '')}">
            <button type="button" onclick="document.getElementById('mf').click()">Upload {fmt} manuscript</button><span id="ms"></span></section>
          <section>Book Cover
            <label><input type="radio" name="cover_mode" value="upload" {c("cover_mode", "upload")}>Upload a cover you already have (print-ready PDF only)</label>
            <input type="file" id="cf" style="display:none" onchange="document.getElementById('cn').value=this.files[0].name;document.getElementById('cs').innerText='Cover uploaded successfully'">
            <input type="hidden" name="cover" id="cn" value="{rec.get('cover', '')}">
            <button type="button" onclick="document.getElementById('cf').click()">Upload your cover file</button><span id="cs"></span></section>
          <section>AI-Generated Content
            <p>Did you use AI tools in creating texts, images, and/or translations in your book?</p>
            <label><input type="radio" name="ai" value="yes" {c("ai", "yes")}>Yes</label>
            <label><input type="radio" name="ai" value="no" {c("ai", "no")}>No</label>
            {ai_select('Texts')} {ai_select('Images')} {ai_select('Translations')}</section>
          <section>Book Preview <button type="submit" name="_action" value="preview">Launch Previewer</button>
            {'<p>Previewer approved</p>' if rec.get('previewer_approved') else ''}</section>
          <section>Summary: Printing cost $2.84</section>
          <button type="submit" name="_action" value="save">Save and Continue</button>
        </form>""")

    def _pricing(self, fmt: str, book_id: str) -> str:
        return self._page(f"{fmt.title()} Rights & Pricing", f"""
        <form method="post">
          <section>Territories
            <label><input type="radio" name="territories" value="all">All territories (worldwide rights)</label>
            <label><input type="radio" name="territories" value="some">Individual territories</label></section>
          <label for="pm">Primary marketplace</label><select id="pm" name="marketplace"><option>Amazon.com</option></select>
          <table><tr><td>Amazon.com</td><td>$<input name="price_us" id="pu" oninput="var p=parseFloat(this.value)||0;var r=(p>=9.99?0.6:0.5)*p-2.84;document.getElementById('roy').innerText='$'+r.toFixed(2)"></td>
          <td>Royalty <span id="roy"></span></td></tr></table>
          <button type="submit" name="_action" value="draft">Save as Draft</button>
          <button type="submit" name="_action" value="publish">Publish Your {fmt.title()} Book</button>
        </form>""")
