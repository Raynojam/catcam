#!/usr/bin/env python3
"""Stage a Pawtrait Studio upgrade in a COPY of an existing lounge release.

Requires the original guestbook frontend. Does not touch settings, Caddy,
systemd, authentication, or the database. Review before deploying.
"""
import argparse
from pathlib import Path

MARKER = "// Catpuccino Studio: signed keepsakes"
HELPER = r'''// Catpuccino Studio: signed keepsakes
  async function saveKeepsake(item, button) {
    button.disabled = true;
    const label = button.textContent;
    button.textContent = 'Making your Polaroid…';
    try {
      const image = new Image();
      image.src = api + '/photo/' + item.id;
      await image.decode();
      const width = 1000, pad = 56, inner = width - pad * 2;
      const canvas = document.createElement('canvas');
      const ctx = canvas.getContext('2d');
      if (!ctx || !image.naturalWidth || !image.naturalHeight) {
        throw Error('Photo unavailable');
      }
      const colors = {cream:'#f5ebd9', lavender:'#dfd0ef',
                      matcha:'#d6e6c8', rose:'#f1ced8'};
      const height = Math.round(inner * image.naturalHeight / image.naturalWidth);
      ctx.font = 'italic 34px Georgia, serif';
      function wrap(text) {
        const lines = [];
        for (const paragraph of String(text).split(/\r?\n/)) {
          let line = '';
          for (const char of Array.from(paragraph)) {
            if (line && ctx.measureText(line + char).width > inner) {
              lines.push(line);
              line = '';
            }
            line += char;
          }
          lines.push(line);
        }
        return lines;
      }
      const lines = wrap(item.caption || '♡');
      const signatures = wrap('— ' + item.signature + ' ' + item.stamp);
      canvas.width = width;
      canvas.height = pad + height + 40 + (lines.length + signatures.length) * 46 + 100;
      ctx.fillStyle = colors[item.style] || colors.cream;
      ctx.fillRect(0, 0, width, canvas.height);
      ctx.drawImage(image, pad, pad, inner, height);
      ctx.fillStyle = '#493c46';
      ctx.font = 'italic 34px Georgia, serif';
      ctx.textBaseline = 'top';
      let y = pad + height + 30;
      for (const line of lines) {
        ctx.fillText(line, pad, y, inner);
        y += 46;
      }
      for (const line of signatures) {
        ctx.fillText(line, pad, y, inner);
        y += 46;
      }
      ctx.font = '22px system-ui, sans-serif';
      ctx.fillStyle = '#695963';
      ctx.fillText('CATPUCCINO  •  ' +
        new Date(item.published * 1000).toLocaleDateString(), pad, y + 22, inner);
      const blob = await new Promise((resolve, reject) =>
        canvas.toBlob(value => value ? resolve(value) : reject(Error('Export failed')), 'image/png'));
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'Catpuccino-Pawtrait-' + item.id.slice(0, 8) + '.png';
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60000);
      button.textContent = '♡ Saved';
      setTimeout(() => { button.textContent = label; }, 1600);
    } catch (error) {
      button.textContent = label;
      el('pawStatus').textContent = error.message || 'Could not save your Pawtrait';
    } finally {
      button.disabled = false;
    }
  }

  let albumFingerprint = null;
'''

def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError("Unexpected frontend version: " + old[:70])
    return text.replace(old, new, 1)

def stage(root):
    js = root / "pawtraits.js"
    css = root / "lounge.css"
    html = root / "lounge.html"
    s = js.read_text(encoding="utf-8")
    if MARKER in s:
        raise ValueError("Studio already applied")
    s = replace_once(s, "  async function album() {", HELPER + "\n  async function album() {")
    s = replace_once(s, "      const grid = el('pawGrid');",
        """      const fingerprint = JSON.stringify(data.items);
      if (fingerprint === albumFingerprint) {
        el('pawStatus').textContent = '';
        return;
      }
      const grid = el('pawGrid');""")
    s = replace_once(s, """        const download = element('a', 'paw-download', 'Save photo');
        download.href = photo.src;
        download.download = 'Catpuccino-' + item.id + '.jpg';""",
        """        const download = element('button', 'paw-download', 'Save signed Pawtrait');
        download.type = 'button';
        download.addEventListener('click', () => saveKeepsake(item, download));""")
    s = replace_once(s, "        grid.append(card);\n      }\n\n      el('pawStatus').textContent = '';",
        "        grid.append(card);\n      }\n      albumFingerprint = fingerprint;\n\n      el('pawStatus').textContent = '';")
    h = html.read_text(encoding="utf-8").replace("Faff", "Faf")
    h = h.replace('tabindex="0" aria-label="Live camera. Tap to zoom. When zoomed, drag or use arrow keys to look around." aria-describedby="viewHelp"', 'aria-label="Stationary live camera"')
    c = css.read_text(encoding="utf-8") + """
/* Catpuccino Studio */
.view-tools, #viewMode, #viewHelp { display: none !important; }
#stage > #picture { transform: none !important; transition: none !important; object-fit: contain !important; }
.paw-download {
  border: 1px solid #493c4660; border-radius: 8px;
  background: transparent; padding: 8px 12px; cursor: pointer;
  font: 600 .78rem system-ui; color: #493c46;
}
.paw-download:hover { background: #493c4610; }
.paw-download:disabled { opacity: .6; cursor: wait; }
"""
    # Complete all checks before writing any file.
    js.write_text(s, encoding="utf-8")
    css.write_text(c, encoding="utf-8")
    html.write_text(h, encoding="utf-8")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("staged_release", type=Path)
    args = parser.parse_args()
    stage(args.staged_release)
    print("Studio frontend staged; validate JavaScript before deployment.")
