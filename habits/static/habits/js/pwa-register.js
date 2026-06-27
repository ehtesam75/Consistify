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
    navigator.serviceWorker.register("/service-worker.js", { scope: "/" }).catch(() => {});
  }

  lockPortraitOrientation();
});
