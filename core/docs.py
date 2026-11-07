"""PDF documents: reports, plans, research write-ups, itineraries. Written as Markdown by the model, rendered to a
clean A4 PDF with PyMuPDF (already installed for reading PDFs), and sent as a file.

Used two ways: the create_pdf tool (when a document is the right output, or the user asks), and automatically when an
answer is too long to read comfortably in chat (AUTO_PDF_CHARS): the chat gets a short summary, the PDF the full answer.
"""
import os
import re
import tempfile
from datetime import datetime

import fitz
import markdown

AUTO_PDF_CHARS = int(os.getenv("AUTO_PDF_CHARS", "3500"))

CSS = """
body { font-family: sans-serif; font-size: 10.5pt; line-height: 1.45; color: #1d1d24; }
h1 { font-size: 20pt; margin: 0 0 4pt 0; color: #111; }
h2 { font-size: 14pt; margin: 14pt 0 4pt 0; color: #222; border-bottom: 1px solid #ddd; }
h3 { font-size: 12pt; margin: 10pt 0 3pt 0; }
p { margin: 0 0 6pt 0; }
ul, ol { margin: 0 0 6pt 14pt; }
li { margin: 0 0 2pt 0; }
table { border-collapse: collapse; margin: 6pt 0; }
th, td { border: 1px solid #ccc; padding: 3pt 5pt; font-size: 9.5pt; }
th { background: #f0f0f4; }
code { font-family: monospace; font-size: 9pt; }
.meta { color: #777; font-size: 9pt; margin-bottom: 10pt; }
"""


def make_pdf(title: str, body_md: str) -> str:
    """Path to the PDF in a fresh temp folder (the sender deletes it)."""
    title = (title or "Document").strip()[:120]
    html_body = markdown.markdown(body_md or "", extensions=["tables", "sane_lists"])
    import html as _h
    html = (f"<h1>{_h.escape(title)}</h1><div class='meta'>Prepared {datetime.now():%d %B %Y}</div>{html_body}")
    folder = tempfile.mkdtemp(prefix="agentdoc_")
    name = re.sub(r"[^A-Za-z0-9 _-]+", "", title).strip().replace(" ", "_")[:60] or "document"
    path = os.path.join(folder, f"{name}.pdf")
    story = fitz.Story(html=html, user_css=CSS)
    writer = fitz.DocumentWriter(path)
    page, where = fitz.paper_rect("a4"), fitz.paper_rect("a4") + (50, 50, -50, -50)
    more = True
    while more:
        dev = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    return path


def tool_create_pdf(title: str, content: str) -> str:
    from core import context
    path = make_pdf(title, content)
    context.get().outbox.append({"kind": "document", "path": path, "caption": title[:200]})
    return (f"PDF '{title}' created and will be sent with your reply. In the chat, give a 2-4 line summary of what's in it; "
            "don't repeat the document.")


def auto_pdf(question: str, answer_md: str, ctx) -> str:
    """If the answer is long, send it as a PDF and return a short chat version; otherwise return it unchanged."""
    if len(answer_md) < AUTO_PDF_CHARS or any(i.get("kind") == "document" for i in ctx.outbox):
        return answer_md
    first = re.sub(r"\s+", " ", re.sub(r"[#*_>|`-]", " ", answer_md)).strip()
    title = re.sub(r"\s+", " ", question).strip()[:80].rstrip("?.!") or "Report"
    try:
        path = make_pdf(title[:1].upper() + title[1:], answer_md)
    except Exception as e:
        print(f"Auto PDF failed: {e}")
        return answer_md
    ctx.outbox.append({"kind": "document", "path": path, "caption": "Full answer"})
    cut = first[:700].rsplit(". ", 1)[0] + "."
    try:      # a real summary, not the first lines
        from core import reason
        r = reason.client.messages.create(_uid="owner", model=reason.MODEL, max_tokens=300, messages=[{"role": "user", "content":
            "Summarise this answer in 3-4 short plain-text lines: the key conclusion first, then the most important "
            f"facts. No preamble, no markdown.\n\n{answer_md[:12000]}"}])
        cut = "".join(getattr(b, "text", "") for b in r.content).strip() or cut
    except Exception:
        pass
    return f"{cut}\n\nFull answer ({len(answer_md.split())} words) in the PDF below."
