// Civil dates use UTC so changing the dashboard time zone never shifts a day.
const civil = (value) => new Date(`${value}T12:00:00Z`);
const key = (date) => date.toISOString().slice(0, 10);
const format = (date, options) => new Intl.DateTimeFormat('tr-TR', { timeZone: 'UTC', ...options }).format(date);

export function setupCalendar({ getToday, getSelected, onSelect }) {
  const trigger = document.getElementById('header-date');
  const popover = document.getElementById('date-calendar');
  const days = document.getElementById('calendar-days');
  let month, focused;
  const selected = () => getSelected() || getToday();
  const setMonth = (date) => { month = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1, 12)); };

  function render() {
    const restoreFocus = days.contains(document.activeElement);
    const value = selected();
    trigger.textContent = format(civil(value), { day: 'numeric', month: 'long', year: 'numeric' });
    trigger.setAttribute('aria-label', `${trigger.textContent}, tarih seç`);
    document.getElementById('calendar-selected').textContent = format(civil(value), { weekday: 'long', day: 'numeric', month: 'long' });
    document.getElementById('calendar-month').textContent = format(month, { month: 'long', year: 'numeric' });
    const start = new Date(month);
    start.setUTCDate(1 - (month.getUTCDay() + 6) % 7);
    days.replaceChildren();
    for (let index = 0; index < 42; index++) {
      const date = new Date(start);
      date.setUTCDate(start.getUTCDate() + index);
      const valueKey = key(date);
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = date.getUTCDate();
      button.dataset.date = valueKey;
      button.className = 'calendar-day';
      button.classList.toggle('outside-month', date.getUTCMonth() !== month.getUTCMonth());
      button.classList.toggle('selected', valueKey === value);
      button.classList.toggle('is-today', valueKey === getToday());
      button.setAttribute('aria-label', format(date, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }));
      button.setAttribute('aria-pressed', String(valueKey === value));
      if (valueKey === getToday()) button.setAttribute('aria-current', 'date');
      button.disabled = valueKey > getToday();
      button.tabIndex = valueKey === focused ? 0 : -1;
      days.append(button);
    }
    const nextMonth = new Date(Date.UTC(month.getUTCFullYear(), month.getUTCMonth() + 1, 1, 12));
    document.getElementById('calendar-next').disabled = key(nextMonth) > getToday();
    if (restoreFocus) focusDay();
  }

  function position() {
    const rect = trigger.getBoundingClientRect();
    popover.style.left = `${Math.max(12, Math.min(rect.right - popover.offsetWidth, window.innerWidth - popover.offsetWidth - 12))}px`;
    popover.style.top = `${rect.bottom + 12}px`;
  }
  function focusDay() { days.querySelector(`[data-date="${focused}"]`)?.focus(); }
  function choose(value) {
    onSelect(value);
    focused = value;
    setMonth(civil(value));
    render();
    popover.hidePopover();
    trigger.focus();
  }
  popover.addEventListener('toggle', (event) => {
    const open = event.newState === 'open';
    trigger.setAttribute('aria-expanded', String(open));
    if (open) { focused = selected(); setMonth(civil(focused)); render(); position(); focusDay(); }
  });
  window.addEventListener('resize', () => { if (popover.matches(':popover-open')) position(); });
  for (const [id, offset] of [['calendar-prev', -1], ['calendar-next', 1]]) {
    document.getElementById(id).addEventListener('click', () => {
      month.setUTCMonth(month.getUTCMonth() + offset);
      focused = key(month);
      render();
    });
  }
  days.addEventListener('click', (event) => {
    const button = event.target.closest('[data-date]');
    if (button && !button.disabled) choose(button.dataset.date);
  });
  days.addEventListener('keydown', (event) => {
    const button = event.target.closest('[data-date]');
    if (!button) return;
    const date = civil(button.dataset.date);
    const weekday = (date.getUTCDay() + 6) % 7;
    const offsets = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7, Home: -weekday, End: 6 - weekday };
    if (!(event.key in offsets)) return;
    event.preventDefault();
    date.setUTCDate(date.getUTCDate() + offsets[event.key]);
    if (key(date) > getToday()) return;
    focused = key(date);
    setMonth(date);
    render();
    focusDay();
  });
  document.getElementById('calendar-today').addEventListener('click', () => choose(getToday()));
  focused = selected();
  setMonth(civil(focused));
  render();
  return { update: render };
}
