/* Shared photo capture: shrink on the phone, read the text, ask the server what it means, fill the form. */
(function () {
  const $ = (s, r) => (r || document).querySelector(s);

  async function shrink(file) {
    try {
      const bmp = await createImageBitmap(file, { imageOrientation: 'from-image' });
      const s = Math.min(1, 1600 / Math.max(bmp.width, bmp.height));
      const c = document.createElement('canvas');
      c.width = Math.round(bmp.width * s); c.height = Math.round(bmp.height * s);
      c.getContext('2d').drawImage(bmp, 0, 0, c.width, c.height);
      const blob = await new Promise(r => c.toBlob(r, 'image/jpeg', 0.82));
      if (!blob || (s === 1 && blob.size >= file.size)) return file;
      return new File([blob], 'photo.jpg', { type: 'image/jpeg' });
    } catch (e) { return file; }
  }

  let tess;
  function loadTess() {
    if (window.Tesseract) return Promise.resolve();
    if (!tess) tess = new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = 'https://cdnjs.cloudflare.com/ajax/libs/tesseract.js/5.1.0/tesseract.min.js';
      s.onload = res; s.onerror = rej; document.head.appendChild(s);
    });
    return tess;
  }

  async function recognise(blob, onProgress) {
    await loadTess();
    const { data } = await Tesseract.recognize(blob, 'eng', {
      logger: m => { if (m.status === 'recognizing text') onProgress(Math.round(m.progress * 100)); }
    });
    return data.text || '';
  }

  async function suggest(kind, text) {
    const r = await fetch('/capture/suggest', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind, text }) });
    return r.json();
  }

  // set a field only when it is empty (or force), and mark it as filled from the photo
  function setv(el, v, force) {
    if (!el || v === null || v === undefined || v === '') return false;
    if (!force && el.value && el.value !== '0') return false;
    el.value = v;
    el.classList.add('autofill');
    return true;
  }

  document.addEventListener('input', e => { if (e.isTrusted && e.target.classList) e.target.classList.remove('autofill'); });

  window.GSCapture = function (o) {
    const input = $(o.input), status = $(o.status), prev = o.preview && $(o.preview), hid = o.ocr && $(o.ocr);
    if (!input) return;
    input.addEventListener('change', async () => {
      const f = input.files[0];
      if (!f) return;
      if (hid) hid.value = '';
      if (!/^image\//.test(f.type)) { status.textContent = 'File attached. Fill in the details by hand.'; return; }
      status.textContent = 'Preparing photo…';
      let blob = f;
      if (!input.multiple) {
        const s = await shrink(f);
        if (s !== f) { const dt = new DataTransfer(); dt.items.add(s); input.files = dt.files; }
        blob = s;
      }
      if (prev) { prev.src = URL.createObjectURL(blob); prev.hidden = false; }
      if (!o.kind) { status.textContent = 'Photo ready.'; return; }
      try {
        status.textContent = 'Reading the photo… you can keep filling in the details meanwhile.';
        const text = await recognise(blob, p => { status.textContent = 'Reading the photo… ' + p + '%'; });
        if (hid) hid.value = text;
        const s = await suggest(o.kind, text);
        const got = await o.apply(s, { setv, $, text, suggest });
        status.textContent = got.length
          ? 'Read from the photo: ' + got.join(', ') + '. Please check each one before saving.'
          : 'Nothing could be read reliably from the photo (handwriting is often hard to read). Please fill in the details by hand.';
      } catch (e) {
        status.textContent = 'Could not read the photo automatically. Please fill in the details by hand.';
      }
    });
  };
})();
