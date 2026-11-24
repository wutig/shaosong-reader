const byId = (name) => document.getElementById(name);
const home = byId('home-view');
const viewport = byId('pdf-viewport');
const pages = byId('pdf-pages');
const pageInput = byId('pdf-page');
const zoomSelect = byId('pdf-zoom');
const passwordDialog = byId('pdf-password-dialog');
const passwordInput = byId('pdf-password');
const fileInput = byId('pdf-file');
const settings = matchMedia('(prefers-reduced-motion: reduce)');
const pdfAssets = new URL('../vendor/pdfjs/', import.meta.url);
const canvasPixelLimit = 8_000_000;
const zoomMinimum = 0.25;
const zoomMaximum = 4;

let libraryPromise;
let session;
let pending;
let passwordRequest;
let dragDepth = 0;
let layoutFrame = 0;
let scrollFrame = 0;
let immersive = false;

const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
const readerVisible = () => document.body.dataset.view === 'reader';

function pdfLibrary() {
  if (!libraryPromise) {
    libraryPromise = import(new URL('pdf.min.mjs', pdfAssets)).then((library) => {
      library.GlobalWorkerOptions.workerSrc = new URL('pdf.worker.min.mjs', pdfAssets).href;
      return library;
    }).catch((error) => {
      libraryPromise = undefined;
      throw error;
    });
  }
  return libraryPromise;
}

function showError(message = '') {
  const error = byId('pdf-error');
  error.querySelector('p').textContent = message;
  error.hidden = !message;
}

function destroyTask(task) {
  if (task) void task.destroy().catch(() => {});
}

function cancelPassword() {
  passwordRequest = undefined;
  passwordInput.value = '';
  byId('pdf-password-error').hidden = true;
  if (passwordDialog.open) passwordDialog.close();
}

function cancelOpening() {
  if (!pending) return;
  const previous = pending;
  pending = undefined;
  cancelPassword();
  destroyTask(previous.task);
  byId('pdf-loading').hidden = true;
}

function requestPassword(attempt, library, updatePassword, reason) {
  if (pending !== attempt) return;
  byId('pdf-loading').hidden = true;
  passwordRequest = { attempt, updatePassword };
  byId('pdf-password-error').hidden = reason !== library.PasswordResponses.INCORRECT_PASSWORD;
  passwordInput.value = '';
  if (!passwordDialog.open) passwordDialog.showModal();
  passwordInput.focus();
}

function readErrorMessage(error) {
  switch (error?.name) {
    case 'InvalidPDFException': return '无法打开此文件。请确认它是完整、有效的 PDF。';
    case 'PasswordException': return 'PDF 密码不正确。请重新打开文件后再试。';
    case 'NotReadableError': return '无法读取文件。请将它下载到本机后再试。';
    default: return '暂时无法打开此 PDF。请尝试重新选择文件。';
  }
}

async function openFile(file) {
  cancelOpening();
  if (!file) return;
  showError();
  if (file.size === 0) {
    showError('这个文件是空的。请选择一个有效的 PDF。');
    return;
  }
  const attempt = {};
  pending = attempt;
  byId('pdf-loading').hidden = false;
  try {
    const [library, data] = await Promise.all([pdfLibrary(), file.arrayBuffer()]);
    if (pending !== attempt) return;
    const task = library.getDocument({
      data: new Uint8Array(data),
      cMapUrl: new URL('cmaps/', pdfAssets).href,
      cMapPacked: true,
      standardFontDataUrl: new URL('standard_fonts/', pdfAssets).href,
      iccUrl: new URL('iccs/', pdfAssets).href,
      wasmUrl: new URL('wasm/', pdfAssets).href,
      enableXfa: false,
    });
    attempt.task = task;
    task.onPassword = (updatePassword, reason) => requestPassword(attempt, library, updatePassword, reason);
    const documentHandle = await task.promise;
    const firstPage = await documentHandle.getPage(1);
    if (pending !== attempt) return;
    installDocument({ library, task, documentHandle, firstPage, name: file.name });
    pending = undefined;
  } catch (error) {
    if (pending === attempt) {
      pending = undefined;
      destroyTask(attempt.task);
      showError(readErrorMessage(error));
    }
  } finally {
    if (!pending) {
      cancelPassword();
      byId('pdf-loading').hidden = true;
    }
  }
}

function cancelRender(view) {
  view.job?.renderTask?.cancel();
  view.job?.textLayer?.cancel();
}

function releaseCanvas(view) {
  if (view.canvas) {
    view.canvas.width = 0;
    view.canvas.height = 0;
    view.canvas = undefined;
  }
  view.renderedScale = undefined;
  view.element.replaceChildren();
}

function destroyDocument() {
  if (!session) return;
  const old = session;
  session = undefined;
  old.observer.disconnect();
  for (const view of old.views) {
    cancelRender(view);
    releaseCanvas(view);
  }
  destroyTask(old.task);
  pages.replaceChildren();
}

function paintError(view) {
  releaseCanvas(view);
  const fallback = document.createElement('p');
  fallback.className = 'pdf-page-error';
  fallback.textContent = `第 ${view.number} 页暂时无法显示。`;
  view.element.append(fallback);
}

function installDocument(incoming) {
  destroyDocument();
  const initialSize = incoming.firstPage.getViewport({ scale: 1 });
  const state = {
    ...incoming,
    initialSize,
    current: 1,
    zoom: 'width',
    views: [],
    near: new Set(),
    activeJobs: 0,
  };
  session = state;
  const fragment = document.createDocumentFragment();
  for (let number = 1; number <= state.documentHandle.numPages; number += 1) {
    const element = document.createElement('div');
    element.className = 'pdf-sheet';
    element.dataset.page = String(number);
    element.setAttribute('role', 'group');
    element.setAttribute('aria-label', `第 ${number} 页`);
    const view = { number, element, size: initialSize };
    if (number === 1) view.pdfPage = incoming.firstPage;
    state.views.push(view);
    fragment.append(element);
  }
  pages.replaceChildren(fragment);
  byId('pdf-title').textContent = state.name;
  byId('pdf-title').title = state.name;
  byId('pdf-page-count').textContent = String(state.views.length);
  pageInput.max = String(state.views.length);
  setMode('reader');
  document.title = `${state.name} · 山河阅卷`;
  updateLayout(state);
  viewport.scrollTo({ top: 0, left: 0, behavior: 'instant' });
  updateTools(state);
  observePages(state);
  viewport.focus({ preventScroll: true });
}

function fitScale(state, size) {
  if (typeof state.zoom === 'number') return state.zoom;
  const margin = viewport.clientWidth <= 560 ? 28 : 60;
  const widthScale = Math.max(1, viewport.clientWidth - margin) / size.width;
  if (state.zoom !== 'page') return Math.min(widthScale, 2);
  return Math.min(widthScale, Math.max(1, viewport.clientHeight - 44) / size.height);
}

function viewAtMarker(state) {
  if (state.navigation && Math.abs(viewport.scrollTop - state.navigation.top) < 2) {
    return state.views[state.navigation.page - 1];
  }
  state.navigation = undefined;
  const marker = viewport.scrollTop + Math.min(110, viewport.clientHeight * 0.18);
  let first = 0;
  let last = state.views.length - 1;
  while (first < last) {
    const mid = (first + last) >> 1;
    const element = state.views[mid].element;
    if (element.offsetTop + element.offsetHeight < marker) first = mid + 1;
    else last = mid;
  }
  return state.views[first];
}

function captureReadingPosition(state) {
  const view = viewAtMarker(state);
  const element = view.element;
  const center = viewport.scrollLeft + viewport.clientWidth / 2;
  return {
    element,
    navigationPage: state.navigation?.page,
    vertical: (viewport.scrollTop - element.offsetTop) / Math.max(1, element.offsetHeight),
    horizontal: (center - element.offsetLeft) / Math.max(1, element.offsetWidth),
  };
}

function restoreReadingPosition(state, { element, vertical, horizontal, navigationPage }) {
  viewport.scrollTop = element.offsetTop + vertical * element.offsetHeight;
  viewport.scrollLeft = element.offsetLeft + horizontal * element.offsetWidth - viewport.clientWidth / 2;
  if (navigationPage) state.navigation = { page: navigationPage, top: viewport.scrollTop };
}

function setPageDimensions(state, view, actualSize) {
  const scale = fitScale(state, actualSize);
  view.size = actualSize;
  if (scale !== view.scale && view.job?.scale) cancelRender(view);
  view.scale = scale;
  view.element.style.width = `${Math.ceil(actualSize.width * scale)}px`;
  view.element.style.height = `${Math.ceil(actualSize.height * scale)}px`;
}

function updateLayout(state, position) {
  for (const view of state.views) setPageDimensions(state, view, view.size);
  if (position) restoreReadingPosition(state, position);
  updateTools(state);
  scheduleRendering(state);
}

function observePages(state) {
  state.observer = new IntersectionObserver((entries) => {
    if (session !== state) return;
    for (const entry of entries) {
      const view = state.views[Number(entry.target.dataset.page) - 1];
      if (entry.isIntersecting) {
        state.near.add(view);
      } else {
        state.near.delete(view);
        cancelRender(view);
        releaseCanvas(view);
        if (!view.job) view.pdfPage?.cleanup();
      }
    }
    scheduleRendering(state);
  }, { root: viewport, rootMargin: '500px 0px' });
  for (const view of state.views) state.observer.observe(view.element);
}

function scheduleRendering(state) {
  if (session !== state || state.activeJobs >= 2) return;
  const candidates = [...state.near].filter((view) => !view.job && !view.failed && view.renderedScale !== view.scale);
  candidates.sort((a, b) => Math.abs(a.number - state.current) - Math.abs(b.number - state.current));
  for (const view of candidates) {
    if (state.activeJobs >= 2) break;
    void renderPage(state, view);
  }
}

function stillNeeded(state, view, job) {
  return session === state && state.near.has(view) && (!job.scale || job.scale === view.scale);
}

async function renderPage(state, view) {
  const job = {};
  let canvas;
  state.activeJobs += 1;
  view.job = job;
  try {
    view.pdfPage ??= await state.documentHandle.getPage(view.number);
    if (!stillNeeded(state, view, job)) return;
    const actualSize = view.pdfPage.getViewport({ scale: 1 });
    if (actualSize.width !== view.size.width || actualSize.height !== view.size.height) {
      const anchor = captureReadingPosition(state);
      setPageDimensions(state, view, actualSize);
      restoreReadingPosition(state, anchor);
      updateTools(state);
    }
    job.scale = view.scale;
    const pageViewport = view.pdfPage.getViewport({ scale: job.scale });
    const density = Math.min(window.devicePixelRatio || 1, 2, Math.sqrt(canvasPixelLimit / (pageViewport.width * pageViewport.height)));
    canvas = document.createElement('canvas');
    canvas.setAttribute('aria-hidden', 'true');
    canvas.width = Math.max(1, Math.floor(pageViewport.width * density));
    canvas.height = Math.max(1, Math.floor(pageViewport.height * density));
    job.renderTask = view.pdfPage.render({
      canvas,
      viewport: pageViewport,
      transform: density === 1 ? undefined : [density, 0, 0, density, 0, 0],
    });
    await job.renderTask.promise;
    if (!stillNeeded(state, view, job)) return;
    releaseCanvas(view);
    view.canvas = canvas;
    view.renderedScale = job.scale;
    view.element.append(canvas);
    const text = document.createElement('div');
    text.className = 'pdf-text-layer';
    text.style.setProperty('--total-scale-factor', String(pageViewport.scale * pageViewport.userUnit));
    view.element.append(text);
    job.textLayer = new state.library.TextLayer({
      textContentSource: view.pdfPage.streamTextContent({ includeMarkedContent: true }),
      container: text,
      viewport: pageViewport,
    });
    try {
      await job.textLayer.render();
    } catch (_) {
      // The page image remains readable if the PDF has no usable text layer.
      text.remove();
    }
  } catch (error) {
    if (stillNeeded(state, view, job) && error.name !== 'RenderingCancelledException') {
      view.failed = true;
      paintError(view);
    }
  } finally {
    if (canvas && view.canvas !== canvas) {
      canvas.width = 0;
      canvas.height = 0;
    }
    view.job = undefined;
    state.activeJobs -= 1;
    if (!state.near.has(view)) view.pdfPage?.cleanup();
    scheduleRendering(state);
  }
}

function updateTools(state) {
  if (document.activeElement !== pageInput) pageInput.value = String(state.current);
  byId('pdf-prev').disabled = state.current === 1;
  byId('pdf-next').disabled = state.current === state.views.length;
  const scale = state.views[state.current - 1].scale;
  byId('pdf-zoom-out').disabled = scale <= zoomMinimum;
  byId('pdf-zoom-in').disabled = scale >= zoomMaximum;
  const optionValue = String(state.zoom);
  if ([...zoomSelect.options].some(({ value }) => value === optionValue)) {
    zoomSelect.value = optionValue;
  } else {
    const custom = zoomSelect.querySelector('[value="custom"]');
    custom.textContent = `${Math.round(scale * 100)}%`;
    zoomSelect.value = 'custom';
  }
}

function goToPage(number) {
  if (!session) return;
  const state = session;
  state.current = Number.isFinite(number) ? clamp(Math.trunc(number), 1, state.views.length) : state.current;
  const view = state.views[state.current - 1];
  viewport.scrollTop = state.current === 1 ? 0 : Math.max(0, view.element.offsetTop - 14);
  state.navigation = { page: state.current, top: viewport.scrollTop };
  pageInput.value = String(state.current);
  updateTools(state);
}

function setZoom(mode) {
  if (!session) return;
  const state = session;
  const anchor = captureReadingPosition(state);
  state.zoom = mode;
  updateLayout(state, anchor);
}

function changeZoom(multiplier) {
  if (!session) return;
  const scale = session.views[session.current - 1].scale;
  setZoom(clamp(Math.round(scale * multiplier * 100) / 100, zoomMinimum, zoomMaximum));
}

function setMode(mode) {
  const reading = mode === 'reader';
  document.body.dataset.view = mode;
  home.hidden = reading;
  byId('pdf-reader').hidden = !reading;
  byId('pdf-open-toolbar').hidden = !reading;
  byId('home-fullscreen-button').hidden = reading;
  updatePoetryMotion();
}

function closeReader() {
  cancelOpening();
  destroyDocument();
  if (document.fullscreenElement || immersive) void toggleFullscreen();
  setMode('home');
  showError();
  document.title = '山河阅卷 · PDF 阅读器';
  byId('pdf-title').textContent = '';
  byId('pdf-title').title = '';
  byId('pdf-open').focus({ preventScroll: true });
}

function syncFullscreen() {
  const active = Boolean(document.fullscreenElement) || immersive;
  document.body.classList.toggle('is-immersive', immersive);
  document.body.classList.toggle('is-fullscreen', Boolean(document.fullscreenElement));
  for (const [button, defaultLabel] of [[byId('home-fullscreen-button'), '首页全屏'], [byId('pdf-fullscreen'), '全屏阅读']]) {
    button.setAttribute('aria-pressed', String(active));
    button.textContent = active ? '退出全屏' : defaultLabel;
  }
}

async function toggleFullscreen() {
  if (document.fullscreenElement) {
    try { await document.exitFullscreen(); } catch (_) { /* Escape remains available. */ }
  } else if (immersive) {
    immersive = false;
  } else if (document.fullscreenEnabled && document.documentElement.requestFullscreen) {
    try { await document.documentElement.requestFullscreen(); } catch (_) { immersive = true; }
  } else {
    immersive = true;
  }
  syncFullscreen();
}

// Same cover motion and pause behavior as the study edition.
let poetryPaused = false;
try { poetryPaused = localStorage.getItem('shaosong-reader:poetry-paused') === 'true'; } catch (_) {}

function updatePoetryMotion() {
  const stopped = settings.matches || poetryPaused;
  const button = byId('hero-motion-toggle');
  home.dataset.poetryMoving = String(!stopped && !readerVisible() && !document.hidden && !passwordDialog.open);
  button.disabled = settings.matches;
  button.setAttribute('aria-pressed', String(stopped));
  button.textContent = settings.matches ? '诗词静止展示' : poetryPaused ? '播放诗词' : '暂停诗词';
}

function drawCoverPoetry(entries) {
  const verses = entries.filter((entry) => typeof entry?.text === 'string' && entry.text.trim());
  for (let last = verses.length - 1; last > 0; last -= 1) {
    const swap = Math.floor(Math.random() * (last + 1));
    [verses[last], verses[swap]] = [verses[swap], verses[last]];
  }
  const group = document.createElement('div');
  group.className = 'hero-marquee-group';
  for (const { text } of verses.slice(0, 10)) {
    const verse = document.createElement('span');
    verse.className = 'hero-marquee-verse';
    verse.textContent = text;
    const dot = document.createElement('span');
    dot.className = 'hero-marquee-dot';
    dot.textContent = '·';
    group.append(verse, dot);
  }
  byId('hero-marquee').replaceChildren(group, group.cloneNode(true));
}

drawCoverPoetry([
  { text: '千里稻花应秀色' }, { text: '人间有味是清欢' }, { text: '山重水复疑无路' },
]);
fetch('assets/poems.json', { cache: 'force-cache' }).then((response) => {
  if (!response.ok) throw new Error('Poetry asset unavailable');
  return response.json();
}).then((entries) => { if (Array.isArray(entries) && entries.length) drawCoverPoetry(entries); }).catch(() => {});

for (const id of ['pdf-open', 'pdf-open-toolbar']) byId(id).addEventListener('click', () => fileInput.click());
fileInput.addEventListener('change', () => {
  const chosen = fileInput.files?.[0];
  fileInput.value = '';
  void openFile(chosen);
});
byId('pdf-close').addEventListener('click', closeReader);
byId('pdf-brand').addEventListener('click', (event) => {
  event.preventDefault();
  if (readerVisible()) closeReader();
});
byId('pdf-prev').addEventListener('click', () => { if (session) goToPage(session.current - 1); });
byId('pdf-next').addEventListener('click', () => { if (session) goToPage(session.current + 1); });
pageInput.addEventListener('change', () => goToPage(pageInput.valueAsNumber));
pageInput.addEventListener('keydown', (event) => {
  if (event.key !== 'Enter') return;
  event.preventDefault();
  goToPage(pageInput.valueAsNumber);
  viewport.focus({ preventScroll: true });
});
byId('pdf-zoom-out').addEventListener('click', () => changeZoom(1 / 1.2));
byId('pdf-zoom-in').addEventListener('click', () => changeZoom(1.2));
zoomSelect.addEventListener('change', () => {
  const value = zoomSelect.value;
  if (value !== 'custom') setZoom(value === 'width' || value === 'page' ? value : Number(value));
});
viewport.addEventListener('scroll', () => {
  if (!session || scrollFrame) return;
  scrollFrame = requestAnimationFrame(() => {
    scrollFrame = 0;
    if (!session) return;
    session.current = viewAtMarker(session).number;
    updateTools(session);
    scheduleRendering(session);
  });
}, { passive: true });
new ResizeObserver(() => {
  if (!session || layoutFrame) return;
  const position = captureReadingPosition(session);
  const state = session;
  layoutFrame = requestAnimationFrame(() => {
    layoutFrame = 0;
    if (session === state) updateLayout(state, position);
  });
}).observe(viewport);

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape' && immersive && !passwordDialog.open) {
    immersive = false;
    syncFullscreen();
    return;
  }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'o' && !passwordDialog.open) {
    event.preventDefault();
    fileInput.click();
    return;
  }
  if (!session || event.target !== viewport || event.altKey || event.ctrlKey || event.metaKey) return;
  if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
    if (viewport.scrollWidth > viewport.clientWidth + 1) return;
    event.preventDefault();
    goToPage(session.current + (event.key === 'ArrowLeft' ? -1 : 1));
  } else if (event.key === '+' || event.key === '=' || event.key === '-') {
    event.preventDefault();
    changeZoom(event.key === '-' ? 1 / 1.2 : 1.2);
  }
});

for (const id of ['home-fullscreen-button', 'pdf-fullscreen']) byId(id).addEventListener('click', toggleFullscreen);
document.addEventListener('fullscreenchange', syncFullscreen);
byId('pdf-error').querySelector('button').addEventListener('click', () => showError());
byId('pdf-password-cancel').addEventListener('click', cancelOpening);
passwordDialog.addEventListener('cancel', (event) => { event.preventDefault(); cancelOpening(); });
passwordDialog.addEventListener('close', updatePoetryMotion);
passwordDialog.addEventListener('toggle', updatePoetryMotion);
byId('pdf-password-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const request = passwordRequest;
  if (!request || request.attempt !== pending) return;
  const entered = passwordInput.value;
  cancelPassword();
  byId('pdf-loading').hidden = false;
  request.updatePassword(entered);
});
byId('hero-motion-toggle').addEventListener('click', () => {
  poetryPaused = !poetryPaused;
  try { localStorage.setItem('shaosong-reader:poetry-paused', String(poetryPaused)); } catch (_) {}
  updatePoetryMotion();
});
document.addEventListener('visibilitychange', updatePoetryMotion);
settings.addEventListener('change', updatePoetryMotion);

document.addEventListener('dragenter', (event) => {
  if (!event.dataTransfer?.types.includes('Files')) return;
  event.preventDefault();
  dragDepth += 1;
  document.body.classList.add('pdf-drop-active');
});
document.addEventListener('dragleave', () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) document.body.classList.remove('pdf-drop-active');
});
document.addEventListener('dragover', (event) => {
  if (event.dataTransfer?.types.includes('Files')) event.preventDefault();
});
document.addEventListener('drop', (event) => {
  if (!event.dataTransfer?.files.length) return;
  event.preventDefault();
  dragDepth = 0;
  document.body.classList.remove('pdf-drop-active');
  void openFile(event.dataTransfer.files[0]);
});

updatePoetryMotion();
