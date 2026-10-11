// diagram.js — draw the Mermaid diagrams on a page (and in fragments HTMX swaps in).
// If mermaid did not load (no outbound access), the diagram's source stays readable as text.
(function () {
  function draw() {
    if (!window.mermaid) return;
    try {
      window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "neutral" });
      const nodes = document.querySelectorAll("pre.mermaid:not([data-processed])");
      if (nodes.length) window.mermaid.run({ nodes }).catch(() => {});
    } catch (error) {
      /* leave the source as text */
    }
  }
  window.addEventListener("load", draw);
  document.addEventListener("htmx:afterSwap", draw);
})();
