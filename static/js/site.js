/**
 * Site-wide JS, loaded as an external file specifically so it's exempt
 * from the production CSP's `script-src 'self'` (same-origin script
 * *files* are allowed; inline <script> blocks are not, and previously
 * weren't — see PRODUCTION_DEPLOYMENT.md "CSP audit").
 */
(function () {
  "use strict";

  function setupMobileNav() {
    var toggle = document.getElementById("nav-toggle");
    var nav = document.getElementById("primary-navigation");
    var navWrapper = document.getElementById("primary-nav-wrapper");
    if (!toggle || !nav || !navWrapper) return;
    toggle.addEventListener("click", function () {
      var isOpen = nav.classList.toggle("is-open");
      // Toggle the wrapper visibility too — on mobile the <nav> wrapper
      // is a third flex sibling alongside brand and toggle. If the wrapper
      // stays in the flow even when the ul is hidden, flex's
      // space-between places the toggle button in the centre rather than
      // the right. Adding/removing is-open on the wrapper lets CSS hide
      // the wrapper itself (removing it from the flex flow) while keeping
      // the list visible when open.
      navWrapper.classList.toggle("is-open", isOpen);
      toggle.setAttribute("aria-expanded", isOpen ? "true" : "false");
    });
  }

  function applyVoteBarWidths() {
    // Sets each bar's width via individual CSSOM property assignment
    // (el.style.width = "..."), not via setAttribute("style", ...) or
    // el.style.cssText — only the latter two are subject to CSP's
    // style-src restriction. Reading the percentage from a data-
    // attribute keeps the HTML itself free of any inline style="".
    var bars = document.querySelectorAll("[data-percentage]");
    for (var i = 0; i < bars.length; i++) {
      var value = parseFloat(bars[i].getAttribute("data-percentage"));
      if (!isNaN(value)) {
        bars[i].style.width = Math.max(0, Math.min(100, value)) + "%";
      }
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    setupMobileNav();
    applyVoteBarWidths();
  });
})();
