// Home Assistant's frame for the ingress page of an app (STANDARD.md §7 of
// ha-repo-template). The same file in every app, unchanged: the page puts it
// inline right after ha-lang.js, before its own script, in the same <script>
// block; ha.css draws what it sets.
//
// Home Assistant's app panel (ha-panel-app) answers a page in its frame that
// subscribes to its properties: whether it is narrow (below 870 px) and the
// safe-area insets of a phone (notch, rounded corners, home bar). This script
// subscribes with handleSafeArea, so Home Assistant leaves the insets to the
// page, and sets html.narrow and --safe-area-inset-top/right/bottom/left from
// the answer. While narrow (or with the sidebar always hidden) it asks for
// kiosk mode, which hides Home Assistant's own 40 px bar above the app; never
// on a wide screen, where kiosk mode would turn the docked sidebar into an
// overlay. The menu button ([data-ha-menu]) opens Home Assistant's sidebar.
// Home Assistant ends kiosk mode itself when the app is left; a new page load
// subscribes again. Outside the frame, or until Home Assistant answers, the
// width of the window decides narrow and ha.css falls back to the browser's
// insets; outside the frame nothing is posted. It never throws and never loops.
(() => {
  try {
    const root = document.documentElement;
    const NARROW = "(max-width: 870px)";
    const framed = window.parent !== window;
    const narrow = (on) => root.classList.toggle("narrow", !!on);
    const post = (message) => {
      try { window.parent.postMessage(message, location.origin); } catch (e) {}
    };
    // The sidebar set to "always hidden" in the profile: Home Assistant's bar shows on a wide screen too
    const hiddenSidebar = () => {
      try {
        const ha = window.parent.document.querySelector("home-assistant");
        return !!(ha && ha.hass && ha.hass.dockedSidebar === "always_hidden");
      } catch (e) {
        return false;
      }
    };
    // Kiosk mode as last requested; on when an earlier load of this page left it on,
    // so that a page that opens wide hands it back (Home Assistant only ends what it began)
    let kiosk = (() => {
      try {
        const ha = window.parent.document.querySelector("home-assistant");
        return framed && !!(ha && ha.hass && ha.hass.kioskMode);
      } catch (e) {
        return false;
      }
    })();
    const subscribe = (wanted) => {
      kiosk = wanted;
      post({ type: "home-assistant/subscribe-properties", kioskMode: wanted, handleSafeArea: true });
    };
    if (!framed) {
      const media = matchMedia(NARROW);
      narrow(media.matches);
      media.addEventListener("change", (e) => narrow(e.matches));
    } else {
      let answered = false;
      const guess = () => {
        try { return window.parent.matchMedia(NARROW).matches; } catch (e) { return matchMedia(NARROW).matches; }
      };
      narrow(guess());
      window.addEventListener("resize", () => { if (!answered) narrow(guess()); });
      window.addEventListener("message", (e) => {
        try {
          const data = e.data;
          if (e.source !== window.parent || !data || data.type !== "home-assistant/properties") return;
          answered = true;
          narrow(data.narrow);
          const insets = data.safeAreaInsets || {};
          for (const side of ["top", "right", "bottom", "left"]) {
            const value = String(insets[side] ?? "").trim();
            if (/^\d+(\.\d+)?px$/.test(value)) root.style.setProperty("--safe-area-inset-" + side, value);
            else root.style.removeProperty("--safe-area-inset-" + side);
          }
          const wanted = !!data.narrow || hiddenSidebar();
          if (wanted && !kiosk) subscribe(true);
          else if (!wanted && kiosk) {
            post({ type: "home-assistant/unsubscribe-properties" });
            subscribe(false);
          }
        } catch (err) {}
      });
      subscribe(guess() || hiddenSidebar());
    }
    // The menu button: Home Assistant's sidebar, by click, Enter or Space
    const menu = (e) => {
      const button = e.target instanceof Element && e.target.closest("[data-ha-menu]");
      if (!button) return false;
      e.preventDefault();
      if (framed) post({ type: "home-assistant/toggle-menu" });
      return true;
    };
    document.addEventListener("click", menu);
    document.addEventListener("keydown", (e) => {
      // Enter on a link or button already clicks; Space does on a button only
      if (e.key === " " && !(e.target instanceof HTMLButtonElement)) menu(e);
    });
  } catch (e) {}
})();
