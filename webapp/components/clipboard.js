// Files carried by a paste, for any composer that takes attachments.
//
// A screenshot copied to the clipboard, an image copied out of a web page or
// a document, a file copied in a file manager: the browser hands all of them
// to a `paste` event as files. The two composers that stage attachments (the
// conversation composer, any file; the chat page, images) read them through
// this module so a paste stages exactly what the paperclip would have — the
// same limits, the same chips or previews, the same errors.
//
// What the caller decides is only whether to let the browser's own paste run:
// `pastedText` says whether the clipboard also carries plain text. A copied
// image alone (types like `Files` and `text/html`, no `text/plain`) must be
// swallowed, or an empty paste lands in the field; a copied passage with an
// image inside it carries its words as `text/plain`, and those still belong
// in the box beside the staged file.
//
// Pure functions over the event's `clipboardData` — nothing here touches the
// DOM — so the behaviour is pinned under Node (tests/test_webapp_paste.py).

// Names the clipboard gives a pasted image that say nothing about it: every
// screenshot is `image.png` in Chrome, Safari and Firefox alike.
const GENERIC_NAMES = new Set(['image', 'image.png', 'image.jpg', 'image.jpeg', 'image.gif',
  'image.webp', 'image.bmp', 'blob', '']);

const EXT_BY_TYPE = {
  'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp',
  'image/bmp': 'bmp', 'image/svg+xml': 'svg', 'image/tiff': 'tiff', 'image/heic': 'heic',
};

// The files in a paste, in clipboard order, each with a name worth showing.
// `data` is a `DataTransfer` (the event's `clipboardData`); anything without
// files gives `[]`, so a plain text paste costs one check and no work.
export function pastedFiles(data, now = new Date()) {
  if (!data) return [];
  const out = [];
  // `items` is the richer view (it is what carries a copied image on every
  // engine); `files` is the fallback where `items` is missing or empty.
  for (const item of Array.from(data.items || [])) {
    if (item.kind !== 'file') continue;
    const file = typeof item.getAsFile === 'function' ? item.getAsFile() : null;
    if (file) out.push(file);
  }
  if (!out.length) out.push(...Array.from(data.files || []));
  return out.map((f) => nameFile(f, now));
}

// Whether the browser's default paste would put words into the field. Only
// plain text counts: a copied image from a page comes with `text/html`
// markup around it that a textarea would never show anyway.
export function pastedText(data) {
  if (!data) return false;
  const types = Array.from(data.types || []);
  if (!types.includes('text/plain')) return false;
  try {
    return String(data.getData('text/plain') || '').trim() !== '';
  } catch (_err) {
    return false;
  }
}

// A pasted image arrives as `image.png` from every engine; the name shown on
// the chip (and stored beside the thread) says when it was pasted instead.
// A file with a real name — copied from a file manager — keeps it.
export function nameFile(file, now = new Date()) {
  const name = String(file.name || '');
  if (!GENERIC_NAMES.has(name.toLowerCase())) return file;
  const ext = EXT_BY_TYPE[String(file.type || '').toLowerCase()]
    || (name.includes('.') ? name.slice(name.lastIndexOf('.') + 1) : 'bin');
  const fresh = `pasted-${stamp(now)}.${ext}`;
  try {
    return new File([file], fresh, { type: file.type, lastModified: file.lastModified || Date.now() });
  } catch (_err) {
    // No File constructor (an old engine): the original, with the name
    // shadowed where the platform allows it.
    try { Object.defineProperty(file, 'name', { value: fresh }); } catch (_e) { /* keep as is */ }
    return file;
  }
}

function stamp(d) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-` +
    `${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}
