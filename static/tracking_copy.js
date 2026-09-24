// Copy controls for the tracking setup card. Falls back to selecting the
// text where the Clipboard API is unavailable. Never logs or stores values.
(() => {
  "use strict";

  const COPIED_STATUS = "Copied.";
  const FALLBACK_STATUS = "Press Ctrl/Cmd+C to copy the selected text.";

  const copyInputValue = async (input) => {
    if (window.isSecureContext && window.navigator.clipboard
        && typeof window.navigator.clipboard.writeText === "function") {
      try {
        await window.navigator.clipboard.writeText(input.value);
        return true;
      } catch (err) {
        // Fall through to the selection fallback below.
      }
    }
    input.focus();
    input.select();
    return false;
  };

  const init = () => {
    const status = document.getElementById("tracking-copy-status");
    const buttons = document.querySelectorAll("[data-copy-target]");
    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        const input = document.getElementById(button.getAttribute("data-copy-target"));
        if (!input) return;
        copyInputValue(input).then((copied) => {
          if (status) status.textContent = copied ? COPIED_STATUS : FALLBACK_STATUS;
        });
      });
    });
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
