// Let the browser own modal focus containment and Escape/cancel. Only opening,
// return focus and focus lost through DOM replacement need application policy.
export function canFocus(element) {
  const modal = document.querySelector('dialog:modal');
  return Boolean(element?.isConnected && !element.disabled &&
    !element.closest('[hidden], [inert]') && element.getClientRects().length &&
    getComputedStyle(element).visibility === 'visible' && (!modal || modal.contains(element)));
}

export function focusVisible(element) {
  if (!canFocus(element)) return false;
  element.focus(); // Native scrolling reveals controls inside their own panels.
  return document.activeElement === element;
}

export function returnFocus(opener) {
  if (!focusVisible(opener)) focusVisible(document.querySelector('.nav-button[aria-current="page"]'));
}

export function setupDialog(dialog) {
  let opener;
  // The close event runs a task after the native focus restore; by then the user may already
  // have moved on (Tab, another control). Only recover focus that was actually lost.
  dialog.addEventListener('close', () => {
    const current = document.activeElement;
    if (!dialog.open && (!current || current === document.body || dialog.contains(current))) returnFocus(opener);
  });
  return {
    open({ initial, trigger = document.activeElement } = {}) {
      if (dialog.open) return;
      opener = trigger;
      dialog.showModal();
      focusVisible(initial || dialog.querySelector('button'));
    },
  };
}

// Restore only focus which this synchronous render removed or hid. A response
// arriving after the user left the panel must never pull focus back into it.
export function replaceWithFocus(container, render, replacement, fallback) {
  const previous = document.activeElement;
  const owned = container.contains(previous);
  render();
  if (owned && !canFocus(previous)) returnFocus(replacement(previous) || fallback);
}
