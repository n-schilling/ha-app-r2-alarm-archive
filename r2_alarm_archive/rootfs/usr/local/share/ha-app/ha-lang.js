// The language of Home Assistant's interface for the ingress page of an app
// (STANDARD.md §7 of ha-repo-template). The same file in every app, unchanged:
// the page puts it inline before its own script, in the same <script> block.
//
// The ingress page has the origin of Home Assistant, so it can read the
// user's language from the frontend around it. It keeps that language in the
// cookie ha_lang, valid for this app's ingress path only; the server renders
// the page in it (else English) and uses the browser's languages only while
// there is no cookie. When the cookie changes, the page reloads once, hidden.
// No reload when the cookie already holds the language, or cannot be set, so
// no loop; outside Home Assistant's frame it does nothing.
(() => {
  try {
    if (window.parent === window) return;
    const ha = window.parent.document.querySelector("home-assistant");
    const hass = ha && ha.hass;
    const raw = hass && ((hass.locale && hass.locale.language) || hass.language);
    const want = String(raw || "").toLowerCase().split(/[-_]/)[0];
    if (!/^[a-z]{2,3}$/.test(want)) return;
    const held = () => (document.cookie.match(/(?:^|;\s*)ha_lang=([^;]*)/) || [])[1];
    if (held() === want) return;
    const path = (location.pathname.match(/^\/api\/hassio_ingress\/[^/]+\//) || ["/"])[0];
    document.cookie = `ha_lang=${want}; path=${path}; max-age=31536000; samesite=strict` +
      (location.protocol === "https:" ? "; secure" : "");
    if (held() !== want) return;
    document.documentElement.style.visibility = "hidden";
    location.reload();
  } catch (e) {}
})();
