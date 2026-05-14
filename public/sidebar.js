(function () {
  const SIDEBAR_WIDTH = 300;
  const ENDPOINT = "/datasets";
  const STORAGE_OPEN = "datasets-sidebar-open";
  const STORAGE_COLLAPSED = "datasets-sidebar-collapsed-groups";

  function readJSON(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  }

  let sidebarOpen = readJSON(STORAGE_OPEN, true);
  if (typeof sidebarOpen !== "boolean") sidebarOpen = true;
  const collapsedGroups = new Set(
    Array.isArray(readJSON(STORAGE_COLLAPSED, [])) ? readJSON(STORAGE_COLLAPSED, []) : []
  );

  function ensurePanel() {
    if (document.getElementById("datasets-sidebar")) return;

    const panel = document.createElement("aside");
    panel.id = "datasets-sidebar";
    panel.innerHTML =
      '<header>Datasets</header>' +
      '<div class="datasets-body"><div class="datasets-loading">Loading…</div></div>';
    document.body.appendChild(panel);

    document.body.style.boxSizing = "border-box";

    const btn = document.createElement("button");
    btn.id = "datasets-toggle";
    btn.type = "button";
    btn.setAttribute("aria-controls", "datasets-sidebar");
    btn.addEventListener("click", toggleSidebar);
    document.body.appendChild(btn);

    applySidebarState();
  }

  function applySidebarState() {
    const panel = document.getElementById("datasets-sidebar");
    const btn = document.getElementById("datasets-toggle");
    if (!panel || !btn) return;
    if (sidebarOpen) {
      panel.classList.remove("hidden");
      document.body.style.paddingRight = SIDEBAR_WIDTH + "px";
      btn.textContent = "⟩";
      btn.setAttribute("aria-label", "Hide datasets sidebar");
      btn.setAttribute("aria-expanded", "true");
      btn.style.right = SIDEBAR_WIDTH + "px";
    } else {
      panel.classList.add("hidden");
      document.body.style.paddingRight = "";
      btn.textContent = "⟨";
      btn.setAttribute("aria-label", "Show datasets sidebar");
      btn.setAttribute("aria-expanded", "false");
      btn.style.right = "0";
    }
  }

  function toggleSidebar() {
    sidebarOpen = !sidebarOpen;
    localStorage.setItem(STORAGE_OPEN, JSON.stringify(sidebarOpen));
    applySidebarState();
  }

  function toggleGroup(label) {
    if (collapsedGroups.has(label)) collapsedGroups.delete(label);
    else collapsedGroups.add(label);
    localStorage.setItem(STORAGE_COLLAPSED, JSON.stringify([...collapsedGroups]));

    const section = document.querySelector(`#datasets-sidebar [data-group="${CSS.escape(label)}"]`);
    if (!section) return;
    const wrapper = section.querySelector(".group-body");
    const chev = section.querySelector(".group-label .chev");
    const header = section.querySelector(".group-label");
    const collapsed = collapsedGroups.has(label);
    if (wrapper) wrapper.classList.toggle("collapsed", collapsed);
    if (chev) chev.textContent = collapsed ? "▶" : "▼";
    if (header) header.setAttribute("aria-expanded", String(!collapsed));
  }

  function injectStyles() {
    const style = document.createElement("style");
    style.textContent = `
      #datasets-sidebar {
        position: fixed; top: 0; right: 0; bottom: 0;
        width: ${SIDEBAR_WIDTH}px;
        background: var(--background, #fff);
        color: var(--foreground, #111);
        border-left: 1px solid rgba(127,127,127,0.2);
        font: 13px/1.4 system-ui, -apple-system, sans-serif;
        display: flex; flex-direction: column;
        z-index: 50;
        transition: transform 180ms ease;
      }
      #datasets-sidebar.hidden { transform: translateX(100%); }
      body { transition: padding-right 180ms ease; }

      #datasets-sidebar > header {
        padding: 12px 16px; font-weight: 600; font-size: 14px;
        border-bottom: 1px solid rgba(127,127,127,0.2);
      }
      #datasets-sidebar .datasets-body {
        flex: 1; overflow-y: auto; padding: 8px 0;
      }
      #datasets-sidebar section { display: block; }
      #datasets-sidebar .group-label {
        width: 100%;
        display: flex; align-items: center; gap: 6px;
        padding: 8px 16px 4px;
        background: none; border: none; text-align: left;
        color: rgba(127,127,127,0.95);
        font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
        cursor: pointer;
        font-family: inherit;
      }
      #datasets-sidebar .group-label:hover { background: rgba(127,127,127,0.06); }
      #datasets-sidebar .group-label .chev {
        width: 10px; display: inline-block; text-align: center;
      }
      #datasets-sidebar .group-label .group-count {
        margin-left: auto; opacity: 0.7;
      }
      #datasets-sidebar .group-body.collapsed { display: none; }

      #datasets-sidebar .file-row {
        padding: 4px 16px; cursor: default;
        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
      }
      #datasets-sidebar .file-row:hover { background: rgba(127,127,127,0.08); }
      #datasets-sidebar .empty {
        padding: 4px 16px; color: rgba(127,127,127,0.7); font-style: italic;
      }
      #datasets-sidebar .error {
        padding: 4px 16px; color: #b00020; font-size: 12px;
      }
      #datasets-sidebar .datasets-loading {
        padding: 12px 16px; color: rgba(127,127,127,0.7);
      }

      #datasets-toggle {
        position: fixed; top: 50%;
        transform: translateY(-50%);
        width: 22px; height: 56px;
        border: 1px solid rgba(127,127,127,0.25);
        border-right: none;
        border-radius: 6px 0 0 6px;
        background: var(--background, #fff);
        color: var(--foreground, #111);
        cursor: pointer;
        font: 14px/1 system-ui, sans-serif;
        z-index: 51;
        transition: right 180ms ease;
        padding: 0;
      }
      #datasets-toggle:hover { background: rgba(127,127,127,0.08); }
    `;
    document.head.appendChild(style);
  }

  function render(groups) {
    const body = document.querySelector("#datasets-sidebar .datasets-body");
    if (!body) return;
    body.innerHTML = "";
    for (const g of groups) {
      const section = document.createElement("section");
      section.dataset.group = g.label;

      const collapsed = collapsedGroups.has(g.label);

      const header = document.createElement("button");
      header.type = "button";
      header.className = "group-label";
      header.setAttribute("aria-expanded", String(!collapsed));

      const chev = document.createElement("span");
      chev.className = "chev";
      chev.textContent = collapsed ? "▶" : "▼";
      const name = document.createElement("span");
      name.className = "group-name";
      name.textContent = g.label;
      const count = document.createElement("span");
      count.className = "group-count";
      count.textContent = `(${g.files.length})`;
      header.appendChild(chev);
      header.appendChild(name);
      header.appendChild(count);

      header.addEventListener("click", () => toggleGroup(g.label));
      section.appendChild(header);

      const wrapper = document.createElement("div");
      wrapper.className = "group-body" + (collapsed ? " collapsed" : "");

      if (g.error) {
        const e = document.createElement("div");
        e.className = "error";
        e.textContent = g.error;
        wrapper.appendChild(e);
      } else if (!g.files.length) {
        const e = document.createElement("div");
        e.className = "empty";
        e.textContent = "no .csv files";
        wrapper.appendChild(e);
      } else {
        for (const f of g.files) {
          const row = document.createElement("div");
          row.className = "file-row";
          const parts = (f.key || f.name).split("/").filter(Boolean);
          row.textContent = parts.length > 1 ? parts.slice(-2).join("/") : (parts[0] || f.name);
          row.title = `s3://${g.bucket}/${f.key}`;
          wrapper.appendChild(row);
        }
      }

      section.appendChild(wrapper);
      body.appendChild(section);
    }
  }

  async function load() {
    try {
      const r = await fetch(ENDPOINT, { headers: { Accept: "application/json" } });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      render(data.groups || []);
    } catch (e) {
      const body = document.querySelector("#datasets-sidebar .datasets-body");
      if (body) {
        body.innerHTML = `<div class="error">Failed to load: ${e.message}</div>`;
      }
    }
  }

  function boot() {
    injectStyles();
    if (document.getElementById("root")) {
      ensurePanel();
      load();
      return;
    }
    const obs = new MutationObserver(() => {
      if (document.getElementById("root")) {
        obs.disconnect();
        ensurePanel();
        load();
      }
    });
    obs.observe(document.documentElement, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
