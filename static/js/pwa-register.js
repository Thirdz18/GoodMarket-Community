/* GoodMarket PWA registration + install prompt
 *
 * Included on every user-facing template. Safe to include more than once on a page;
 * guards against double registration and double-mounted install buttons.
 */
(function () {
  if (window.__gmPwaInitialized) return;
  window.__gmPwaInitialized = true;

  // Skip inside Telegram Mini App — it already runs full-screen and a SW can
  // interfere with Telegram's own network handling.
  if (window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData) {
    return;
  }

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      // Register from root URL so the default max scope covers the whole
      // site. The Flask route in main.py serves the file that physically
      // lives at /static/service-worker.js and sends Service-Worker-Allowed
      // as a backup for any cached registrations still pointing at /static/.
      navigator.serviceWorker
        .register('/service-worker.js', { scope: '/' })
        .then(function (registration) {
          // Poll for updates every 60s so long-lived tabs pick up new versions.
          setInterval(function () {
            registration.update().catch(function () {});
          }, 60000);
        })
        .catch(function (err) {
          console.warn('[PWA] Service worker registration failed:', err);
        });
    });
  }

  // ----- Install prompt handling ---------------------------------------------
  var deferredPrompt = null;
  var installBtn = null;

  function isStandalone() {
    return (
      window.matchMedia('(display-mode: standalone)').matches ||
      window.navigator.standalone === true
    );
  }

  function createInstallButton() {
    if (installBtn || isStandalone()) return;
    installBtn = document.createElement('button');
    installBtn.id = 'gm-install-btn';
    installBtn.type = 'button';
    installBtn.setAttribute('aria-label', 'Install GoodMarket app');
    installBtn.textContent = 'Install App';
    installBtn.style.cssText = [
      'position:fixed',
      'bottom:20px',
      'right:20px',
      'z-index:9999',
      'padding:10px 18px',
      'border:0',
      'border-radius:999px',
      'background:linear-gradient(135deg,#7c3aed 0%,#6366f1 100%)',
      'color:#fff',
      'font:600 14px/1 -apple-system,BlinkMacSystemFont,"Segoe UI","Inter",sans-serif',
      'box-shadow:0 10px 30px rgba(124,58,237,0.45)',
      'cursor:pointer',
      'display:none',
    ].join(';');

    installBtn.addEventListener('click', async function () {
      if (!deferredPrompt) return;
      installBtn.disabled = true;
      try {
        deferredPrompt.prompt();
        await deferredPrompt.userChoice;
      } catch (e) {
        console.warn('[PWA] Install prompt error:', e);
      }
      deferredPrompt = null;
      hideInstallButton();
    });

    document.body.appendChild(installBtn);
  }

  function showInstallButton() {
    if (!installBtn) createInstallButton();
    if (installBtn) installBtn.style.display = 'inline-flex';
  }

  function hideInstallButton() {
    if (installBtn) installBtn.style.display = 'none';
  }

  window.addEventListener('beforeinstallprompt', function (event) {
    event.preventDefault();
    deferredPrompt = event;
    if (document.body) {
      showInstallButton();
    } else {
      document.addEventListener('DOMContentLoaded', showInstallButton, { once: true });
    }
  });

  window.addEventListener('appinstalled', function () {
    deferredPrompt = null;
    hideInstallButton();
  });
})();
