#!/usr/bin/env python3
"""Inline the prototype into one self-contained HTML file (dist/attention-prototype.html)."""
import pathlib, re
here = pathlib.Path(__file__).resolve().parent
index = (here / 'index.html').read_text(encoding='utf-8')
body = index.split('<!-- artifact:start -->')[1].split('<!-- artifact:end -->')[0]
fonts = re.search(r'<link rel="stylesheet" href="(https://fonts\.googleapis\.com[^"]+)">', index).group(1)
css = (here / 'style.css').read_text(encoding='utf-8')
scripts = ''.join(f"<script>\n{(here / f).read_text(encoding='utf-8')}\n</script>\n" for f in ('backends.js', 'engine.js', 'simulation.js', 'ui.js'))
out = (f'<title>Retinue Attention Prototype</title>\n<link rel="stylesheet" href="{fonts}">\n<style>\n{css}</style>\n{body}\n'
       f'{scripts}<script>AttentionApp.boot(document.getElementById("app"));</script>\n')
dist = here / 'dist'; dist.mkdir(exist_ok=True)
(dist / 'attention-prototype.html').write_text(out, encoding='utf-8')
print(f'wrote {dist / "attention-prototype.html"} ({len(out) // 1024} KB)')
