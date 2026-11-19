pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

// A pinch zooms the visual viewport, which magnifies the canvases as bitmaps: a page is re-rendered at the pinch's
// scale once it settles. A whole page at that scale would pass iOS's canvas budget, so each page keeps a base canvas of
// at most BASE_CANVAS_PIXELS and, where that is coarser than the screen, a detail canvas over its part on screen at the
// screen's resolution.
const BASE_CANVAS_PIXELS = 5e6;
const SETTLE_MS = 150;
const RETRY_MS = 1000;
const FLASH_MS = 2000;
const EVENTS_PATH = '/.synctex/events';
const pdfUrl = location.pathname + '?raw';
const scrollerEl = document.getElementById('scroller');
const pagesEl = document.getElementById('pages');
const statusEl = document.getElementById('status');
const zoomEl = document.getElementById('zoom');
document.title = decodeURIComponent(location.pathname.split('/').pop());
// ?debug: what the pinch re-render did, under the bar — the pinch, the canvases of the page on screen, the visual
// viewport and the page's box, and the last render error.
const debugEl = new URLSearchParams(location.search).has('debug') ? document.createElement('div') : null;
let lastError = '';
if (debugEl) {
  debugEl.style.cssText = 'padding: 0.5em 0.83em; background: rgba(0, 0, 0, 0.75); '
                        + 'font: 0.86em monospace; white-space: pre-wrap;';
  document.getElementById('hud').append(debugEl);
}

function showDebug() {
  if (!debugEl) return;
  const vv = window.visualViewport;
  const page = pages.find(p => p.visible && onScreen(p));
  const size = canvas => canvas ? canvas.width + 'x' + canvas.height : 'none';
  const rect = page ? page.el.getBoundingClientRect() : null;
  debugEl.textContent = [
    'pinch ' + pinchScale().toFixed(2) + ' dpr ' + devicePixelRatio,
    'vv ' + (vv ? [vv.offsetLeft, vv.offsetTop, vv.width, vv.height].map(v => v.toFixed(0)).join(',') : 'none')
      + ' scroll ' + scrollerEl.scrollLeft.toFixed(0) + ',' + scrollerEl.scrollTop.toFixed(0),
    page ? 'page ' + pages.indexOf(page) + ' box ' + [rect.left, rect.top, rect.width].map(v => v.toFixed(0)).join(',')
           + ' base ' + size(page.baseCanvas) + ' detail ' + size(page.detailCanvas)
           + (page.detailCanvas ? ' at ' + page.detailCanvas.style.left + ',' + page.detailCanvas.style.top : '')
         : 'no page on screen',
    lastError,
  ].join('\n');
}

let doc = null;
let docVersion = 0;
let shownStamp = null;
let wantedStamp = null;
let loading = false;
let pendingJump = null;
let updatedText = '';
let zoom = 1;
const pages = [];

const observer = new IntersectionObserver(entries => {
  for (const entry of entries) {
    const page = pages.find(p => p.el === entry.target);
    if (!page) continue;
    page.visible = entry.isIntersecting;
    if (page.visible) {
      render(page);
    } else {
      drop(page.baseCanvas);
      drop(page.detailCanvas);
      page.baseCanvas = page.detailCanvas = null;
      page.baseKey = page.detailKey = null;
    }
  }
}, { root: scrollerEl, rootMargin: '100% 0px' });

function pageWidth() {
  return Math.min(scrollerEl.clientWidth - 16, 900) * zoom;
}

function resizePages() {
  const width = pageWidth();
  for (const page of pages) {
    const viewport = page.proxy.getViewport({ scale: 1 });
    page.el.style.width = width + 'px';
    page.el.style.height = width * viewport.height / viewport.width + 'px';
  }
  zoomEl.textContent = Math.round(zoom * 100) + '%';
}

function readAnchor() {
  for (const [pageIdx, page] of pages.entries()) {
    const rect = page.el.getBoundingClientRect();
    if (rect.bottom > 0) return { pageIdx, fraction: -rect.top / rect.height, left: scrollerEl.scrollLeft / scrollerEl.scrollWidth };
  }
  return null;
}

function restoreAnchor(anchor) {
  if (!anchor || !pages[anchor.pageIdx]) return;
  const el = pages[anchor.pageIdx].el;
  scrollerEl.scrollTo(anchor.left * scrollerEl.scrollWidth, el.offsetTop + anchor.fraction * el.offsetHeight);
}

function relayout() {
  const anchor = readAnchor();
  resizePages();
  restoreAnchor(anchor);
  pages.filter(p => p.visible).forEach(render);
}

function pinchScale() {
  return window.visualViewport ? visualViewport.scale : 1;
}

// The part of a page on screen, in the page's CSS pixels, or null: the page's box clipped to the visual viewport, which
// a pinch shrinks and moves within the layout viewport the box is measured in.
function onScreen(page) {
  const rect = page.el.getBoundingClientRect();
  const vv = window.visualViewport;
  const screen = vv ? { left: vv.offsetLeft, top: vv.offsetTop, right: vv.offsetLeft + vv.width,
                        bottom: vv.offsetTop + vv.height }
                    : { left: 0, top: 0, right: innerWidth, bottom: innerHeight };
  const left = Math.max(rect.left, screen.left);
  const top = Math.max(rect.top, screen.top);
  const right = Math.min(rect.right, screen.right);
  const bottom = Math.min(rect.bottom, screen.bottom);
  if (right <= left || bottom <= top) return null;
  return { x: left - rect.left, y: top - rect.top, width: right - left, height: bottom - top };
}

// A canvas taken off the page, its pixels released at once: iOS counts them against its budget until it is collected.
function drop(canvas) {
  if (!canvas) return;
  canvas.width = canvas.height = 0;
  canvas.remove();
}

async function render(page) {
  if (page.busy) {
    page.again = true;
    return;
  }
  page.busy = true;
  try {
    do {
      page.again = false;
      if (!page.visible) break;
      const proxy = page.proxy;
      const base = proxy.getViewport({ scale: 1 });
      // PDF units to device pixels at the layout's width and the pinch.
      const deviceScale = devicePixelRatio * pinchScale();
      const fineScale = page.el.clientWidth / base.width * deviceScale;
      const baseScale = Math.min(fineScale, Math.sqrt(BASE_CANVAS_PIXELS / (base.width * base.height)));

      const baseKey = docVersion + ':' + page.el.clientWidth + ':' + baseScale;
      if (page.baseKey !== baseKey || !page.baseCanvas) {
        const viewport = proxy.getViewport({ scale: baseScale });
        const canvas = document.createElement('canvas');
        canvas.width = Math.ceil(viewport.width);
        canvas.height = Math.ceil(viewport.height);
        await proxy.render({ canvasContext: canvas.getContext('2d'), viewport }).promise;
        if (!page.visible || page.proxy !== proxy) {
          drop(canvas);
          continue;
        }
        drop(page.baseCanvas);
        page.el.prepend(canvas);
        page.baseCanvas = canvas;
        page.baseKey = baseKey;
      }

      const part = fineScale > baseScale * 1.05 ? onScreen(page) : null;
      if (part === null) {
        drop(page.detailCanvas);
        page.detailCanvas = page.detailKey = null;
        continue;
      }
      const detailKey = baseKey + ':' + fineScale + ':' + [part.x, part.y, part.width, part.height].map(Math.round);
      if (page.detailKey === detailKey) continue;
      const canvas = document.createElement('canvas');
      canvas.className = 'detail';
      canvas.width = Math.ceil(part.width * deviceScale);
      canvas.height = Math.ceil(part.height * deviceScale);
      Object.assign(canvas.style, { left: part.x + 'px', top: part.y + 'px',
                                     width: part.width + 'px', height: part.height + 'px' });
      await proxy.render({ canvasContext: canvas.getContext('2d'), viewport: proxy.getViewport({ scale: fineScale }),
                           transform: [1, 0, 0, 1, -part.x * deviceScale, -part.y * deviceScale] }).promise;
      if (!page.visible || page.proxy !== proxy) {
        drop(canvas);
        continue;
      }
      drop(page.detailCanvas);
      page.el.append(canvas);
      page.detailCanvas = canvas;
      page.detailKey = detailKey;
    } while (page.again);
  } catch (error) {
    console.warn(error);
    lastError = 'error: ' + error;
  } finally {
    page.busy = false;
    showDebug();
  }
}

async function load(stamp) {
  statusEl.textContent = 'loading…';
  let newDoc;
  let proxies;
  try {
    newDoc = await pdfjsLib.getDocument({ url: pdfUrl + '&t=' + Date.now(), disableRange: true, disableStream: true }).promise;
    proxies = await Promise.all(Array.from({ length: newDoc.numPages }, (_, i) => newDoc.getPage(i + 1)));
  } catch (error) {
    statusEl.textContent = 'unreadable, retrying';
    return false;
  }
  const anchor = readAnchor();
  const oldDoc = doc;
  doc = newDoc;
  docVersion += 1;
  shownStamp = stamp;
  while (pages.length < proxies.length) {
    const el = document.createElement('div');
    el.className = 'page';
    pagesEl.append(el);
    pages.push({ el, visible: false });
    observer.observe(el);
  }
  while (pages.length > proxies.length) {
    const page = pages.pop();
    observer.unobserve(page.el);
    page.el.remove();
  }
  proxies.forEach((proxy, pageIdx) => { pages[pageIdx].proxy = proxy; });
  resizePages();
  restoreAnchor(anchor);
  await Promise.all(pages.filter(p => p.visible).map(render));
  if (oldDoc) oldDoc.destroy();
  updatedText = 'updated ' + new Date().toLocaleTimeString();
  statusEl.textContent = updatedText;
  if (pendingJump) jump(pendingJump);
  return true;
}

// Loads one at a time, always the latest stamp the server announced; an unreadable file is tried again until it
// reads or a newer one is announced.
async function requestLoad(stamp) {
  wantedStamp = stamp;
  if (loading) return;
  loading = true;
  while (wantedStamp !== shownStamp) {
    if (!await load(wantedStamp)) await new Promise(resolve => setTimeout(resolve, RETRY_MS));
  }
  loading = false;
}

// A forward search: the boxes on their page, scrolled to a third of the screen's height and flashed. A jump to a page
// the shown document does not have yet waits for the next load.
function jump(target) {
  const page = pages[target.page - 1];
  if (!page || !page.proxy) {
    pendingJump = target;
    return;
  }
  pendingJump = null;
  const base = page.proxy.getViewport({ scale: 1 });
  const ratio = page.el.offsetWidth / base.width;
  const top = Math.min(...target.boxes.map(box => box.top)) * ratio;
  const left = Math.min(...target.boxes.map(box => box.left)) * ratio;
  const right = Math.max(...target.boxes.map(box => box.left + box.width)) * ratio;
  scrollerEl.scrollTo(page.el.offsetLeft + (left + right) / 2 - scrollerEl.clientWidth / 2,
                      page.el.offsetTop + top - scrollerEl.clientHeight / 3);
  for (const box of target.boxes) {
    const el = document.createElement('div');
    el.className = 'synctex-box';
    Object.assign(el.style, { left: box.left / base.width * 100 + '%', top: box.top / base.height * 100 + '%',
                               width: box.width / base.width * 100 + '%', height: box.height / base.height * 100 + '%' });
    page.el.append(el);
    setTimeout(() => el.remove(), FLASH_MS);
  }
}

// The server announces the PDF once it has settled after each change, and forward searches; the browser reconnects
// on its own after a drop, e.g. when iOS suspends the tab.
function listen() {
  const events = new EventSource(EVENTS_PATH + '?pdf=' + encodeURIComponent(location.pathname));
  events.addEventListener('pdf', event => requestLoad(JSON.parse(event.data).stamp));
  events.addEventListener('jump', event => jump(JSON.parse(event.data)));
  events.onopen = () => { statusEl.textContent = shownStamp === null ? 'waiting for the PDF' : updatedText; };
  events.onerror = () => {
    statusEl.textContent = events.readyState === EventSource.CLOSED ? 'server unreachable' : 'reconnecting…';
  };
}

document.getElementById('zoom_in').onclick = () => { zoom = clampZoom(zoom * 1.25); relayout(); };
document.getElementById('zoom_out').onclick = () => { zoom = clampZoom(zoom / 1.25); relayout(); };

// A two-finger pinch is the viewer's zoom, not the browser's: the browser's magnifies the canvases as bitmaps, which
// iOS shows jagged, while the viewer's lays the pages out at the new width and renders them again, as the buttons do.
// While the fingers move the pages follow them as a scaled preview; when they lift, the point that was under them
// at the start is laid out under where they are now. The browser's pinch is held off by touch-action and, for iOS,
// which ignores user-scalable=no, by refusing its gesture events.
const MIN_ZOOM = 0.5;
const MAX_ZOOM = 8;
let pinch = null;

function clampZoom(value) {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, value));
}

function fingers(touches) {
  const [a, b] = touches;
  return { x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2,
            distance: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) };
}

// The pinch's ratio, held to what keeps the zoom within its bounds.
function pinchRatio() {
  return clampZoom(zoom * pinch.now.distance / pinch.start.distance) / zoom;
}

// Where a point of the screen is on the pages: the page nearest it vertically, and the point as fractions of its box.
function pageAnchor(x, y) {
  let best = null;
  for (const [pageIdx, page] of pages.entries()) {
    const rect = page.el.getBoundingClientRect();
    const gap = y < rect.top ? rect.top - y : y > rect.bottom ? y - rect.bottom : 0;
    if (best === null || gap < best.gap) {
      best = { gap, pageIdx, fx: (x - rect.left) / rect.width, fy: (y - rect.top) / rect.height };
    }
  }
  return best;
}

addEventListener('touchstart', event => {
  if (event.touches.length !== 2 || pages.length === 0) return;
  const start = fingers(event.touches);
  const rect = pagesEl.getBoundingClientRect();
  pinch = { start, now: start, anchor: pageAnchor(start.x, start.y) };
  pagesEl.style.transformOrigin = (start.x - rect.left) + 'px ' + (start.y - rect.top) + 'px';
}, { passive: true });

addEventListener('touchmove', event => {
  if (pinch === null || event.touches.length !== 2) return;
  event.preventDefault();
  pinch.now = fingers(event.touches);
  pagesEl.style.transform = 'translate(' + (pinch.now.x - pinch.start.x) + 'px, ' + (pinch.now.y - pinch.start.y) + 'px) '
                          + 'scale(' + pinchRatio() + ')';
}, { passive: false });

function endPinch(event) {
  if (pinch === null || event.touches.length >= 2) return;
  const { now, anchor } = pinch;
  zoom = clampZoom(zoom * pinchRatio());
  pinch = null;
  pagesEl.style.transform = '';
  resizePages();
  const el = pages[anchor.pageIdx].el;
  scrollerEl.scrollTo(el.offsetLeft + anchor.fx * el.offsetWidth - now.x, el.offsetTop + anchor.fy * el.offsetHeight - now.y);
  pages.filter(p => p.visible).forEach(render);
}
addEventListener('touchend', endPinch);
addEventListener('touchcancel', endPinch);
for (const type of ['gesturestart', 'gesturechange', 'gestureend']) {
  addEventListener(type, event => event.preventDefault());
}
let resizeTimer = null;
addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(relayout, 150);
});
let pinchTimer = null;
const rerenderVisible = () => {
  clearTimeout(pinchTimer);
  pinchTimer = setTimeout(() => {
    pages.filter(p => p.visible).forEach(render);
    showDebug();
  }, SETTLE_MS);
};
// The bar in the screen's top right corner at its own size whatever the zoom: a fixed box sits in the layout viewport,
// which a pinch magnifies and the visual viewport moves within, so it is moved to the visual viewport's corner and its
// font, which every size in it follows, divided by the pinch — laid out again, not a scaled bitmap, so it stays sharp.
const hudEl = document.getElementById('hud');
function placeHud() {
  const vv = window.visualViewport;
  if (!vv) return;
  hudEl.style.fontSize = 14 / vv.scale + 'px';
  hudEl.style.transform = 'translate(' + (vv.offsetLeft + vv.width) + 'px, ' + vv.offsetTop + 'px) translateX(-100%)';
}
if (window.visualViewport) {
  for (const event of ['resize', 'scroll']) {
    visualViewport.addEventListener(event, rerenderVisible);
    visualViewport.addEventListener(event, placeHud);
  }
}
scrollerEl.addEventListener('scroll', rerenderVisible);
placeHud();
listen();
