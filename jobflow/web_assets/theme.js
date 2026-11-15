(function () {
  'use strict';

  // Run before styles load so a saved appearance never flashes the other theme.
  const storageKey = 'jobflow-theme';
  const validTheme = value => value === 'light' || value === 'dark' ? value : null;
  const readPreference = () => {
    try { return validTheme(window.localStorage.getItem(storageKey)); }
    catch { return null; }
  };
  let preference = readPreference();
  const daytimeTheme = () => {
    const hour = new Date().getHours();
    return hour >= 7 && hour < 19 ? 'light' : 'dark';
  };
  const moon = '<path d="M20.9 13A9 9 0 0 1 11 3.1 9 9 0 1 0 20.9 13Z"/>';
  const sun = '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M4.93 4.93l1.42 1.42m11.3 11.3 1.42 1.42M4.93 19.07l1.42-1.42m11.3-11.3 1.42-1.42"/>';

  function applyTheme() {
    const theme = preference || daytimeTheme();
    document.documentElement.dataset.theme = theme;
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.content = theme === 'light' ? '#f5f6f7' : '#101416';
    const toggle = document.getElementById('theme-toggle');
    if (!toggle) return;
    const label = 'Switch to ' + (theme === 'light' ? 'dark' : 'light') + ' mode';
    toggle.setAttribute('aria-label', label);
    toggle.setAttribute('title', label);
    toggle.setAttribute('aria-pressed', String(theme === 'dark'));
    toggle.innerHTML = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (theme === 'light' ? moon : sun) + '</svg>';
  }

  function bindToggle() {
    const toggle = document.getElementById('theme-toggle');
    if (toggle) toggle.addEventListener('click', () => {
      preference = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
      try { window.localStorage.setItem(storageKey, preference); } catch { /* Keep this session's choice. */ }
      applyTheme();
    });
    applyTheme();
  }

  applyTheme();
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bindToggle, {once: true});
  else bindToggle();

  // Browser-local time follows the device when it wakes or changes time zones.
  window.setInterval(() => { if (!preference) applyTheme(); }, 60000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !preference) applyTheme();
  });
  window.addEventListener('storage', event => {
    if (event.key !== storageKey && event.key !== null) return;
    preference = readPreference();
    applyTheme();
  });
})();
