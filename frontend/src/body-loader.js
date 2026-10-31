// Keep the HTML silhouette until after a paint and the model enters the viewport.
// A bounded fallback also covers background tabs, hidden panels and broken observers.
export function setupBodyLoader(container, { getMotion, load = () => import('./body.js') }) {
  let body = null, metrics = null, started = false, disposed = false, intersecting = false, observer;
  let frame;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const motion = () => getMotion() && !reduced.matches && !document.hidden && container.getClientRects().length > 0;
  const updateMotion = () => body?.setMotion(motion());
  const stopWaiting = () => {
    clearTimeout(fallback);
    cancelAnimationFrame(frame);
    observer?.disconnect();
  };
  async function start() {
    if (started || disposed) return;
    started = true;
    stopWaiting();
    try {
      const { createBody } = await load();
      if (disposed) return;
      body = createBody(container, motion());
      if (metrics) body.setBodyMetrics(metrics);
      updateMotion();
    } catch {
      container.dataset.modelFailed = 'true';
    }
  }
  function observe() {
    if (started || disposed) return;
    try {
      observer = new IntersectionObserver(entries => {
        intersecting = entries.some(entry => entry.isIntersecting);
        if (!document.hidden && intersecting) start();
      });
      observer.observe(container);
    } catch {
      start(); // No IntersectionObserver: still load after the first paint.
    }
  }
  const fallback = setTimeout(start, 5000);
  frame = requestAnimationFrame(() => { frame = requestAnimationFrame(observe); });
  function onVisibility() {
    updateMotion();
    if (!document.hidden && intersecting) start();
  }
  document.addEventListener('visibilitychange', onVisibility);
  reduced.addEventListener('change', updateMotion);
  window.addEventListener('pagehide', () => {
    disposed = true;
    stopWaiting();
    document.removeEventListener('visibilitychange', onVisibility);
    reduced.removeEventListener('change', updateMotion);
    body?.dispose();
  }, { once: true });
  return {
    setBodyMetrics(value) { metrics = value; body?.setBodyMetrics(value); },
    updateMotion,
  };
}
