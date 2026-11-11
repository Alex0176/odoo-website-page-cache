# Copyright 2026 Biricon IT Services GmbH
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl).
import gzip
import json
import os
import shutil
import tempfile
import time
from unittest.mock import patch

from odoo.tests import BaseCase, tagged

from .. import compress, minify, store

PAGE = b"""<!DOCTYPE html>
<html>
  <head>
    <!-- remove me -->
    <meta charset="utf-8"/>
    <style>
      a  { color : red ; }
      /* gone */ b:hover{ content: "a  b" ; background: url( 'x y.png' ) }
    </style>
    <script type="application/ld+json">
      { "@type": "Article", "name": "caf\xc3\xa9  </script"
      }
    </script>
    <script>
      var a = 1;   // keep this exactly
      var b = "x  y";
    </script>
  </head>
  <body>
    <!--[if IE]><p>ie</p><![endif]-->
    <div class="x">
      <p>Hello   <b>big</b> <i>world</i>
        and more</p>
      <pre>  keep
     me  </pre>
      <textarea>
  a   b</textarea>
      <span>one</span> <span>two</span>
      <a href="/x">link</a>
      <a href="/y">link</a>
      <button class="d-inline-block">a</button>
      <div class="d-inline-block">x</div>
      <div class="d-inline-block">y</div>
      <input type="hidden" name="csrf_token" value="@@website_page_cache:csrf@@"/>
    </div>
  </body>
</html>
"""


@tagged("post_install", "-at_install")
class TestMinify(BaseCase):
    def setUp(self):
        super().setUp()
        self.out = minify.minify(PAGE).decode()

    def test_smaller_and_comments(self):
        self.assertLess(len(self.out), len(PAGE))
        self.assertNotIn("remove me", self.out)
        self.assertNotIn("gone", self.out)
        self.assertIn("<!--[if IE]><p>ie</p><![endif]-->", self.out)

    def test_raw_elements_untouched(self):
        self.assertIn("<pre>  keep\n     me  </pre>", self.out)
        self.assertIn("<textarea>\n  a   b</textarea>", self.out)
        self.assertIn('var a = 1;   // keep this exactly\n      var b = "x  y";', self.out)

    def test_inline_spacing_stays(self):
        self.assertIn("<span>one</span> <span>two</span>", self.out)
        self.assertIn('<a href="/x">link</a> <a href="/y">link</a>', self.out)
        self.assertIn("<p>Hello <b>big</b> <i>world</i> and more</p>", self.out)
        # inline-blocks: the gap between them is visible, so it must stay
        self.assertIn('>x</div> <div class="d-inline-block">y</div>', self.out)

    def test_block_gaps_go(self):
        self.assertIn("<head><meta", self.out)
        self.assertIn("</p><pre>", self.out)

    def test_css(self):
        self.assertIn("<style>a{color :red}b:hover{content:\"a  b\";background:url( 'x y.png' )}</style>", self.out)
        self.assertEqual(minify.minify_css("a { b : c ; }\n/* x */ d{e:f;}"), "a{b :c}d{e:f}")  # a space before a colon may be a selector, it stays

    def test_ld_json(self):
        ok = b'<script type="application/ld+json">{ "a": [1, 2],\n "b": "\xc3\xa9 </script>x" }</script>'
        # a script end inside a string ends the element for browsers as well: input stays as it is
        self.assertEqual(minify.minify(ok), ok)
        good = b'<p> </p><script type="application/ld+json">\n{ "a": [1, 2],\n "b": "\xc3\xa9<\\/b>" }\n</script>'
        out = minify.minify(good)
        body = out[out.index(b'ld+json">') + 9:out.rindex(b"</script>")]
        self.assertEqual(body, '{"a":[1,2],"b":"\xe9<\\/b>"}'.encode())
        self.assertEqual(json.loads(body), {"a": [1, 2], "b": "\xe9</b>"})
        broken = b'<script type="application/ld+json">{ "a": [1, </script>\n<p> x </p>'
        self.assertIn(b'{ "a": [1, </script>', minify.minify(broken))

    def test_placeholder_and_attributes_survive(self):
        self.assertIn('value="@@website_page_cache:csrf@@"', self.out)
        self.assertIn('<div class="x">', self.out)

    def test_nbsp_and_entities_untouched(self):
        data = b"<p>a&nbsp;&nbsp; b\xc2\xa0 \xc2\xa0c</p>  <p>  d  </p>"
        self.assertEqual(minify.minify(data), b"<p>a&nbsp;&nbsp; b\xc2\xa0 \xc2\xa0c</p><p>d</p>")

    def test_white_space_pre_elements_kept(self):
        data = b'<div style="white-space: pre-wrap">  a\n   b  <b> c </b></div>\n\n<p>  x  </p>'
        self.assertEqual(
            minify.minify(data), b'<div style="white-space: pre-wrap">  a\n   b  <b> c </b></div><p>x</p>')

    def test_unsafe_input_returned_unchanged(self):
        for data in (b"<pre>  never closed  \n\n\n", b"\xff\xfe<p>  </p>   \n  ", b"", b"plain text"):
            self.assertEqual(minify.minify(data), data)

    def test_idempotent(self):
        once = minify.minify(PAGE)
        self.assertEqual(minify.minify(once), once)


@tagged("post_install", "-at_install")
class TestCompress(BaseCase):
    def test_accept_encoding(self):
        self.assertTrue(compress.accepts_gzip("gzip, deflate, br"))
        self.assertTrue(compress.accepts_gzip("deflate, GZIP;q=0.5"))
        self.assertTrue(compress.accepts_gzip("x-gzip"))
        self.assertFalse(compress.accepts_gzip("gzip;q=0"))
        self.assertFalse(compress.accepts_gzip("br, deflate"))
        self.assertFalse(compress.accepts_gzip(None))
        self.assertFalse(compress.accepts_gzip(""))

    def test_roundtrip_and_vary(self):
        data = PAGE * 20
        packed = compress.gzip_bytes(data)
        self.assertLess(len(packed), len(data))
        self.assertEqual(gzip.decompress(packed), data)
        self.assertEqual(compress.gzip_bytes(data), packed)  # no timestamp: reproducible
        from werkzeug.datastructures import Headers
        headers = Headers()
        compress.add_vary(headers)
        compress.add_vary(headers)
        self.assertEqual(headers["Vary"], "Accept-Encoding")
        headers["Vary"] = "Cookie"
        compress.add_vary(headers)
        self.assertEqual(headers["Vary"], "Cookie, Accept-Encoding")

    def test_asset_store_and_trim(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with patch.object(store, "root", lambda backend, dbname=None: f"{tmp}/{dbname or ''}"):
            data = compress.gzip_bytes(b"body{color:red}" * 100)
            self.assertIsNone(store.asset_load("db", "/web/assets/abc/web.css?"))
            self.assertTrue(store.asset_save("db", "/web/assets/abc/web.css?", {"content_type": "text/css"}, data))
            meta, loaded = store.asset_load("db", "/web/assets/abc/web.css?")
            self.assertEqual((meta["content_type"], loaded), ("text/css", data))
            self.assertIsNone(store.asset_load("db", "/web/assets/def/web.css?"))
            self.assertEqual(store.asset_trim("db"), 0)
            path = next(iter(store.asset_entries("db")))[0]
            old = time.time() - 40 * 86400
            os.utime(path, (old, old))
            self.assertEqual(store.asset_trim("db"), 1)
            self.assertIsNone(store.asset_load("db", "/web/assets/abc/web.css?"))
            store.asset_save("db", "k", {}, data)
            store.clear_all("db")
            self.assertIsNone(store.asset_load("db", "k"))
