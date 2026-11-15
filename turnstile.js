(() => {
  let scriptLoad;

  function loadTurnstile() {
    if (window.turnstile) return Promise.resolve(window.turnstile);
    if (scriptLoad) return scriptLoad;

    scriptLoad = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
      script.async = true;
      script.defer = true;
      script.onload = () => {
        if (window.turnstile) resolve(window.turnstile);
        else {
          scriptLoad = null;
          reject(new Error('The security check did not load. Check your internet connection and try again.'));
        }
      };
      script.onerror = () => {
        scriptLoad = null;
        reject(new Error('The security check could not load. Check your internet connection and try again.'));
      };
      document.head.append(script);
    });

    return scriptLoad;
  }

  async function render(container, siteKey, action, callbacks) {
    const turnstile = await loadTurnstile();
    return turnstile.render(container, {
      sitekey: siteKey,
      action,
      size: 'flexible',
      callback: callbacks.onToken,
      'expired-callback': callbacks.onExpired,
      'error-callback': callbacks.onError
    });
  }

  function reset(widgetId) {
    if (window.turnstile && widgetId !== null) window.turnstile.reset(widgetId);
  }

  window.CareflowTurnstile = { render, reset };
})();
