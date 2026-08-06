function isStandalonePwa() {
  return (
    window.matchMedia("(display-mode: standalone)").matches ||
    window.navigator.standalone === true
  );
}

function lockPortraitOrientation() {
  if (!isStandalonePwa() || !screen.orientation || typeof screen.orientation.lock !== "function") {
    return;
  }

  const lockPortrait = () => {
    screen.orientation.lock("portrait").catch(() => {});
  };

  lockPortrait();
  screen.orientation.addEventListener("change", lockPortrait);
}

window.addEventListener("load", () => {
  if ("serviceWorker" in navigator) {
    const shouldReloadForUpdate = Boolean(navigator.serviceWorker.controller);
    let reloading = false;

    if (shouldReloadForUpdate) {
      navigator.serviceWorker.addEventListener("controllerchange", () => {
        if (reloading) {
          return;
        }
        reloading = true;
        window.location.reload();
      });
    }

    navigator.serviceWorker
      .register("/service-worker.js", { scope: "/", updateViaCache: "none" })
      .then((registration) => registration.update())
      .catch(() => {});
  }

  lockPortraitOrientation();
});
