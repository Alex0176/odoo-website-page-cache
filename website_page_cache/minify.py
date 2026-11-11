# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
"""Conservative HTML minifier for cached pages (bytes in, bytes out).

What it does:

* removes HTML comments (not conditional comments, not ``<!--!`` licence comments);
* collapses every run of white space in text to one space (never touching non-breaking spaces);
* removes white space completely only where the browser ignores it anyway: between and around
  block-level elements, table parts and ``<br>``. An element whose tag says ``inline``,
  ``display``, ``float`` or ``white-space`` is not treated as a block, so a space between two
  inline-blocks stays;
* leaves ``<pre>``, ``<textarea>``, ``<script>`` and elements with ``white-space: pre*`` untouched;
* ``<script type="application/ld+json">`` is re-serialised compactly (unchanged when it does not
  parse); other scripts are not touched at all (no renaming, no whitespace removal);
* ``<style>`` blocks get a simple, string- and ``url()``-aware CSS minification.

Anything unexpected (unclosed raw-text element, undecodable bytes, a larger result) returns the
input unchanged.
"""
import json
import re

WS = " \t\r\n\f"
RAW_NAMES = ("script", "style", "pre", "textarea", "xmp", "listing")
ATTRS = r"""(?:"[^"]*"|'[^']*'|[^>"'])*"""
SCANNER = re.compile(
    r"(?P<comment><!--.*?-->)"
    rf"|(?P<raw><(?P<rn>{'|'.join(RAW_NAMES)})(?P<ra>(?![\w:-]){ATTRS})>(?P<rb>.*?)</(?P=rn)\s*>)"
    rf"|(?P<tag><(?P<close>/?)(?P<name>[a-zA-Z][a-zA-Z0-9:-]*)(?P<attrs>{ATTRS})>)"
    r"|(?P<other><[!?][^>]*>)"
    r"|(?P<text>[^<]+|<)",
    re.DOTALL | re.IGNORECASE,
)
WS_RUN = re.compile(r"[ \t\r\n\f]+")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
        "track", "wbr"}
# whitespace around these is ignored by the layout (unless the tag says otherwise, see UNSAFE)
STRUCT = {
    "html", "head", "body", "div", "p", "section", "article", "header", "footer", "nav", "main", "aside",
    "ul", "ol", "li", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead", "tbody",
    "tfoot", "tr", "td", "th", "caption", "colgroup", "col", "form", "fieldset", "legend", "hr", "br",
    "blockquote", "figure", "figcaption", "address", "details", "summary", "option", "optgroup", "title",
}
WEAK = {"meta", "link", "base", "template", "noscript"}  # invisible; only empty gaps between boundaries go
UNSAFE = re.compile(r"inline|display|float|white-space|position", re.I)
PRESERVE = re.compile(r"white-space\s*:\s*(?:pre|break)|\b(?:text-pre|pre-wrap|white-space-pre)\b", re.I)
LD_JSON = re.compile(r"""type\s*=\s*["']?application/ld\+json""", re.I)


def minify_css(css):
    """Strings and ``url(...)`` are kept verbatim; comments go, white space shrinks."""
    tokens = re.split(r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|url\([^)'"]*\)|/\*.*?\*/)""", css, flags=re.S)
    kept, text = [], []
    for i, part in enumerate(tokens):
        if i % 2 == 0:
            text.append(part)
        elif not part.startswith("/*"):
            text.append(f"\x00{len(kept)}\x00")
            kept.append(part)
    result = WS_RUN.sub(" ", "".join(text))
    result = re.sub(r" ?([{};,>]) ?", r"\1", result)
    result = re.sub(r":\s+", ":", result).strip().replace(";}", "}")
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], result)


def _minify_ld_json(body):
    try:
        data = json.loads(body)
    except ValueError:
        return body
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    text = text.replace("</", "<\\/")
    if "<!--" in text:
        return body
    return text


def _raw(match):
    name = match.group("rn").lower()
    attrs = match.group("ra")
    body = match.group("rb")
    if name == "script" and LD_JSON.search(attrs):
        body = _minify_ld_json(body)
    elif name == "style" and not re.search(r"type\s*=\s*[\"']?(?!text/css)", attrs, re.I):
        body = minify_css(body)
    whole = match.group(0)
    start = match.start(0)
    return whole[: match.start("rb") - start] + body + whole[match.end("rb") - start:]


def minify_html(html):
    """Minify a ``str``; raises ``ValueError`` when the markup is not safe to touch."""
    tokens = []  # [kind, text, flag] kind: text | tag | raw | other
    stack = []  # open STRUCT elements: (name, safe)
    keep_name, keep_depth = None, 0
    pos = 0
    for m in SCANNER.finditer(html):
        if m.start() != pos:
            raise ValueError("unscanned markup")
        pos = m.end()
        if m.group("comment") is not None:
            c = m.group("comment")
            if c.startswith(("<!--[if", "<!--<!", "<!--!", "<!-->")) or c.endswith(("<![endif]-->",)):
                tokens.append(["other", c, "keep"])
            continue
        if m.group("raw") is not None:
            name = m.group("rn").lower()
            if name in ("script", "style"):
                tokens.append(["raw", _raw(m), "weak"])
            else:
                tokens.append(["raw", m.group(0), "struct" if name == "pre" else None])
            continue
        if m.group("tag") is not None:
            name = m.group("name").lower()
            attrs = m.group("attrs")
            closing = bool(m.group("close"))
            if name in RAW_NAMES and not closing:
                raise ValueError("unclosed raw text element")
            flag = None
            if name in STRUCT or name in WEAK:
                if closing:
                    for i in range(len(stack) - 1, -1, -1):
                        if stack[i][0] == name:
                            flag = "struct" if stack[i][1] else None
                            del stack[i:]
                            break
                else:
                    safe = not UNSAFE.search(attrs)
                    if name in STRUCT:
                        flag = "struct" if safe else None
                        if name not in VOID and not attrs.rstrip().endswith("/"):
                            stack.append((name, safe))
                    else:
                        flag = "weak"
                    if name == "br":
                        flag = "struct"
            # elements keeping their white space through CSS
            if keep_name:
                if name == keep_name and not closing and name not in VOID:
                    keep_depth += 1
                elif name == keep_name and closing:
                    keep_depth -= 1
                    if keep_depth == 0:
                        keep_name = None
                tokens.append(["tag", m.group(0), None, True])
                continue
            if not closing and name not in VOID and PRESERVE.search(attrs):
                keep_name, keep_depth = name, 1
                tokens.append(["tag", m.group(0), None, True])
                continue
            tokens.append(["tag", m.group(0), flag, False])
            continue
        if m.group("other") is not None:
            tokens.append(["other", m.group(0), "struct" if m.group(0)[:3].lower() == "<!d" else None])
            continue
        tokens.append(["text", m.group("text"), None, bool(keep_name)])
    if pos != len(html):
        raise ValueError("unscanned markup")

    # merge texts that were separated by removed comments
    merged = []
    for tok in tokens:
        if tok[0] == "text" and merged and merged[-1][0] == "text" and merged[-1][3] == tok[3]:
            merged[-1][1] += tok[1]
        else:
            merged.append(tok)

    def boundary(tok, strong):
        if tok is None:
            return True
        if tok[0] == "other" and tok[2] == "keep":
            return False
        return tok[2] == "struct" or (not strong and tok[2] == "weak")

    out = []
    for i, tok in enumerate(merged):
        if tok[0] != "text":
            out.append(tok[1])
            continue
        if tok[3]:  # inside an element that keeps its white space
            out.append(tok[1])
            continue
        text = WS_RUN.sub(" ", tok[1])
        prev = merged[i - 1] if i else None
        nxt = merged[i + 1] if i + 1 < len(merged) else None
        if text == " ":
            if boundary(prev, False) and boundary(nxt, False) or (
                boundary(prev, True) or boundary(nxt, True)
            ):
                text = ""
        else:
            if text.startswith(" ") and boundary(prev, True):
                text = text[1:]
            if text.endswith(" ") and boundary(nxt, True):
                text = text[:-1]
        out.append(text)
    return "".join(out)


def minify(data):
    """bytes -> bytes; the input itself when anything goes wrong or nothing is gained."""
    try:
        html = data.decode("utf-8")
        result = minify_html(html).encode("utf-8")
    except (ValueError, UnicodeError, RecursionError):
        return data
    return result if 0 < len(result) < len(data) else data
