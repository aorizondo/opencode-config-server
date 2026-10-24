// OpenCode Config admin panel. No framework and no build step: this file is
// served as-is from /admin/static under a CSP that only allows same-origin
// scripts, styles and requests. All user data reaches the DOM through
// textContent / properties, never as HTML.
"use strict";

(() => {
  const API = "/admin/api";

  // -- DOM helpers ------------------------------------------------------------

  const PROPS = new Set(["value", "checked", "disabled", "hidden", "selected", "readOnly", "rows", "min", "max", "required"]);
  let uidCounter = 0;
  const uid = () => `f${++uidCounter}`;

  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      el.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    // Props after children, so a <select> can take its value from its options.
    for (const [key, value] of Object.entries(props || {})) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
      else if (PROPS.has(key)) el[key] = value;
      else el.setAttribute(key, value === true ? "" : String(value));
    }
    return el;
  }

  /** replaceChildren that skips null/false, so conditional children can be inline. */
  function put(el, ...children) {
    el.replaceChildren(...children.flat(Infinity).filter((child) => child !== null && child !== undefined && child !== false));
  }

  const clone = (value) => (value === undefined ? undefined : JSON.parse(JSON.stringify(value)));
  const enc = encodeURIComponent;
  const encPath = (path) => path.split("/").map(enc).join("/");

  /** Set obj[key], or remove it when the value is empty. */
  function assign(obj, key, value) {
    if (value === "" || value === undefined || value === null) delete obj[key];
    else obj[key] = value;
  }

  function renameKey(obj, from, to) {
    const entries = Object.entries(obj).map(([key, value]) => [key === from ? to : key, value]);
    for (const key of Object.keys(obj)) delete obj[key];
    for (const [key, value] of entries) obj[key] = value;
  }

  function move(list, index, delta) {
    const target = index + delta;
    if (target < 0 || target >= list.length) return;
    [list[index], list[target]] = [list[target], list[index]];
  }

  function formatBytes(size) {
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / 1024 / 1024).toFixed(1)} MB`;
  }

  function formatTime(seconds) {
    return new Date(seconds * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "medium" });
  }

  // -- API --------------------------------------------------------------------

  class ApiError extends Error {
    constructor(status, data) {
      super(errorText(status, data));
      this.status = status;
      this.data = data || {};
    }
  }

  function errorText(status, data) {
    const detail = data && data.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail)) {
      return detail.map((item) => `${(item.loc || []).slice(1).join(".") || "request"}: ${item.msg}`).join("; ");
    }
    return status ? `Request failed (HTTP ${status}).` : "Cannot reach the server.";
  }

  async function api(method, path, body) {
    const init = { method, credentials: "same-origin", headers: { "X-OCS-Admin": "1", accept: "application/json" } };
    if (body !== undefined) {
      init.headers["content-type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    let response;
    try {
      response = await fetch(API + path, init);
    } catch {
      throw new ApiError(0, null);
    }
    const data = await response.json().catch(() => ({}));
    if (response.status === 401 && path !== "/session") {
      // Only a session that was open can have "ended"; on first load just ask.
      renderLogin(main ? "Your session ended. Sign in again." : "");
      throw new ApiError(401, data);
    }
    if (!response.ok) throw new ApiError(response.status, data);
    return data;
  }

  // -- feedback: toasts, dialogs, banners --------------------------------------

  let toastBox = null;
  function toast(text, kind = "ok") {
    if (!toastBox) {
      toastBox = h("div", { class: "toasts", role: "status", "aria-live": "polite" });
      document.body.append(toastBox);
    }
    const item = h("div", { class: `toast ${kind}`, text });
    toastBox.append(item);
    setTimeout(() => item.remove(), kind === "error" ? 9000 : 4000);
  }

  function fail(error) {
    if (error && error.status === 401) return;
    toast(error && error.message ? error.message : String(error), "error");
  }

  /**
   * A modal dialog. An action's onClick may be async; returning true keeps the
   * dialog open (for validation errors), anything else closes it.
   */
  function modal({ title, body, actions = [], wide = false, onClose }) {
    const dialog = h("dialog", { class: wide ? "modal wide" : "modal", "aria-label": title });
    const close = () => dialog.open && dialog.close();
    dialog.addEventListener("close", () => {
      dialog.remove();
      if (onClose) onClose();
    });
    const buttons = actions.map((action) => {
      const button = h("button", { type: "button", class: `btn ${action.kind || ""}`, text: action.label });
      button.addEventListener("click", async () => {
        if (!action.onClick) return close();
        button.disabled = true;
        try {
          if ((await action.onClick(close)) !== true) close();
        } catch (error) {
          fail(error);
        } finally {
          button.disabled = false;
        }
      });
      return button;
    });
    put(dialog,
      h("div", { class: "modal-head" },
        h("h2", { text: title }),
        h("button", { type: "button", class: "icon-btn", "aria-label": "Close", text: "×", onclick: close })),
      h("div", { class: "modal-body" }, body),
      buttons.length ? h("div", { class: "modal-actions" }, buttons) : null,
    );
    document.body.append(dialog);
    dialog.showModal();
    return { dialog, close };
  }

  function confirmBox(title, text, { confirm = "Confirm", danger = false } = {}) {
    return new Promise((resolve) => {
      let answered = false;
      const answer = (value) => () => { answered = true; resolve(value); };
      modal({
        title,
        body: h("p", { text }),
        actions: [
          { label: "Cancel", kind: "ghost", onClick: answer(false) },
          { label: confirm, kind: danger ? "danger" : "primary", onClick: answer(true) },
        ],
        onClose: () => { if (!answered) resolve(false); },
      });
    });
  }

  /** Ask for a name; resolves to it, or null when cancelled. */
  function promptName(title, { label = "Name", value = "", taken = [], pattern = /^[A-Za-z0-9][A-Za-z0-9._-]*$/, hint } = {}) {
    return new Promise((resolve) => {
      let answered = false;
      const input = h("input", { type: "text", value, spellcheck: "false", autocomplete: "off", id: uid() });
      const error = h("p", { class: "form-error", role: "alert", hidden: true });
      const submit = () => {
        const name = input.value.trim();
        let problem = "";
        if (!name) problem = "Enter a name.";
        else if (!pattern.test(name)) problem = "Use letters, digits, dot, dash or underscore, starting with a letter or digit.";
        else if (name !== value && taken.includes(name)) problem = `"${name}" already exists.`;
        if (problem) {
          error.textContent = problem;
          error.hidden = false;
          input.setAttribute("aria-invalid", "true");
          input.focus();
          return true;
        }
        answered = true;
        resolve(name);
        return false;
      };
      const { close } = modal({
        title,
        body: [field(label, input, hint), error],
        actions: [
          { label: "Cancel", kind: "ghost" },
          { label: value ? "Rename" : "Create", kind: "primary", onClick: submit },
        ],
        onClose: () => { if (!answered) resolve(null); },
      });
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !submit()) close();
      });
      input.focus();
      input.select();
    });
  }

  function banner(kind, title, items = [], ...extra) {
    return h("div", { class: `banner ${kind}`, role: kind === "error" ? "alert" : null },
      title ? h("strong", { text: title }) : null,
      items.length ? h("ul", {}, items.map((item) => h("li", { text: item }))) : null,
      extra);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const area = h("textarea", { value: text });
      document.body.append(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    toast("Copied to the clipboard.");
  }

  function copyBlock(text) {
    return h("div", { class: "copy" }, h("code", { text }),
      h("button", { type: "button", class: "btn small", text: "Copy", onclick: () => copyText(text) }));
  }

  // -- form controls -------------------------------------------------------------

  function field(label, control, hint) {
    const target = control instanceof HTMLElement && /^(INPUT|SELECT|TEXTAREA)$/.test(control.tagName) ? control : null;
    if (target && !target.id) target.id = uid();
    return h("div", { class: "field" },
      target ? h("label", { for: target.id, text: label }) : h("span", { class: "label", text: label }),
      control,
      hint ? h("p", { class: "hint", text: hint }) : null);
  }

  function textInput(value, onInput, attrs = {}) {
    return h("input", {
      type: "text", value: value ?? "", spellcheck: "false", autocomplete: "off", ...attrs,
      oninput: (event) => onInput(event.target.value),
    });
  }

  function select(options, value, onChange, attrs = {}) {
    return h("select", { ...attrs, value: value ?? "", onchange: (event) => onChange(event.target.value) },
      options.map(([optionValue, label]) => h("option", { value: optionValue, text: label })));
  }

  function checkbox(label, checked, onChange) {
    return h("label", { class: "check" },
      h("input", { type: "checkbox", checked: Boolean(checked), onchange: (event) => onChange(event.target.checked) }),
      h("span", { text: label }));
  }

  function iconButton(label, symbol, onClick, disabled = false) {
    return h("button", { type: "button", class: "icon-btn", title: label, "aria-label": label, text: symbol, disabled, onclick: onClick });
  }

  /** Monospace editor: Tab indents, Ctrl/Cmd+S calls onSave. */
  function codeArea(value, onInput, { rows = 16, onSave, label, prose = false } = {}) {
    const area = h("textarea", {
      class: prose ? "code prose" : "code", rows, spellcheck: "false", autocomplete: "off", wrap: prose ? "soft" : "off", value: value ?? "",
      "aria-label": label || null, oninput: (event) => onInput(event.target.value),
    });
    area.addEventListener("keydown", (event) => {
      if (event.key === "Tab" && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey) {
        event.preventDefault();
        area.setRangeText("  ", area.selectionStart, area.selectionEnd, "end");
        onInput(area.value);
      } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s" && onSave) {
        event.preventDefault();
        onSave();
      }
    });
    return area;
  }

  /** Editable list of strings stored at holder[key]. */
  function stringList(holder, key, { placeholder, onChange, addLabel = "+ Add" }) {
    const box = h("div", { class: "list-editor" });
    const render = (focusLast = false) => {
      const items = Array.isArray(holder[key]) ? holder[key] : [];
      put(box, 
        ...items.map((value, index) => h("div", { class: "list-row" },
          textInput(value, (next) => { holder[key][index] = next; onChange(); }, { placeholder, "aria-label": `${key} ${index + 1}` }),
          iconButton("Remove", "×", () => {
            holder[key].splice(index, 1);
            if (!holder[key].length) delete holder[key];
            onChange();
            render();
          }))),
        h("button", {
          type: "button", class: "btn small", text: addLabel,
          onclick: () => { (holder[key] ||= []).push(""); onChange(); render(true); },
        }),
      );
      if (focusLast) box.querySelector(".list-row:last-of-type input")?.focus();
    };
    render();
    return box;
  }

  /** Editable string→string object at holder[key] (environment, headers). */
  function keyValues(holder, key, { keyPlaceholder, valuePlaceholder, onChange }) {
    const rows = Object.entries(holder[key] || {});
    const box = h("div", { class: "kv-editor" });
    const commit = () => {
      const entries = rows.filter(([name]) => name.trim());
      if (entries.length) holder[key] = Object.fromEntries(entries.map(([name, value]) => [name.trim(), value]));
      else delete holder[key];
      onChange();
    };
    const render = (focusLast = false) => {
      put(box, 
        ...rows.map((row, index) => h("div", { class: "kv-row" },
          textInput(row[0], (value) => { row[0] = value; commit(); }, { placeholder: keyPlaceholder, "aria-label": "Name" }),
          textInput(row[1], (value) => { row[1] = value; commit(); }, { placeholder: valuePlaceholder, "aria-label": "Value" }),
          iconButton("Remove", "×", () => { rows.splice(index, 1); commit(); render(); }))),
        h("button", { type: "button", class: "btn small", text: "+ Add", onclick: () => { rows.push(["", ""]); render(true); } }),
      );
      if (focusLast) box.querySelector(".kv-row:last-of-type input")?.focus();
    };
    render();
    return box;
  }

  const EFFECT_LABELS = { allow: "Allow", ask: "Ask", deny: "Deny" };
  const KNOWN_ACTIONS = ["read", "edit", "shell", "glob", "grep", "webfetch", "websearch", "external_directory", "question", "subagent"];

  /** Ordered permission rules at holder[key]; last match wins in OpenCode. */
  function rulesEditor(holder, key, { onChange, actions = null, effects = ["allow", "ask", "deny"], defaultEffect = "ask" }) {
    const box = h("div", { class: "rules" });
    const render = () => {
      const rules = Array.isArray(holder[key]) ? holder[key] : [];
      const rows = rules.map((rule, index) => h("div", { class: "rule-row" },
        actions
          ? select(actions.map((action) => [action, action]), rule.action, (value) => { rule.action = value; onChange(); }, { "aria-label": "Action" })
          : textInput(rule.action, (value) => { rule.action = value.trim(); onChange(); }, { list: "ocs-actions", placeholder: "action", "aria-label": "Action" }),
        textInput(rule.resource, (value) => { rule.resource = value; onChange(); }, { placeholder: "pattern, e.g. **/.env", "aria-label": "Resource" }),
        select(effects.map((effect) => [effect, EFFECT_LABELS[effect]]), rule.effect, (value) => { rule.effect = value; onChange(); }, { "aria-label": "Effect" }),
        h("div", { class: "row-actions" },
          iconButton("Move up", "↑", () => { move(rules, index, -1); onChange(); render(); }, index === 0),
          iconButton("Move down", "↓", () => { move(rules, index, 1); onChange(); render(); }, index === rules.length - 1),
          iconButton("Remove", "×", () => {
            rules.splice(index, 1);
            if (!rules.length) delete holder[key];
            onChange();
            render();
          })),
      ));
      put(box, 
        rows.length
          ? h("div", { class: "rule-head" }, ["Action", "Resource", "Effect", ""].map((text) => h("span", { text })))
          : h("p", { class: "muted", text: "No rules." }),
        ...rows,
        h("button", {
          type: "button", class: "btn small", text: "+ Add rule",
          onclick: () => {
            (holder[key] ||= []).push({ action: actions ? actions[0] : "", resource: "*", effect: defaultEffect });
            onChange();
            render();
          },
        }),
      );
    };
    render();
    return box;
  }

  function pageHead(title, description, ...actions) {
    return h("div", { class: "page-head" },
      h("div", {}, h("h1", { text: title }), description ? h("p", { text: description }) : null),
      actions.length ? h("div", { class: "page-actions" }, actions) : null);
  }

  function readOnlyBanner(what, path) {
    return banner("warn", `${what} is read-only`, [
      `The server cannot write ${path}. Mount the config directory read-write (see docs/OPERATIONS.md) to edit it here.`,
    ]);
  }

  // -- history -------------------------------------------------------------------

  async function openHistory(doc, { title, etag = null, onRestored }) {
    const data = await api("GET", `/history?doc=${enc(doc)}`);
    const body = h("div");
    const showList = () => {
      if (!data.versions.length) {
        put(body, h("p", { class: "muted", text: "No earlier versions yet. Every save from this panel keeps the previous one here." }));
        return;
      }
      put(body, 
        data.viewable ? null : h("p", { class: "hint", text: "Old versions of users.json hold password hashes, so they can be restored but not displayed." }),
        h("table", { class: "list" },
          h("thead", {}, h("tr", {}, h("th", { text: "Saved" }), h("th", { class: "hide-small", text: "Size" }), h("th", {}))),
          h("tbody", {}, data.versions.map((version) => h("tr", {},
            h("td", { text: formatTime(version.time) }),
            h("td", { class: "hide-small", text: formatBytes(version.size) }),
            h("td", { class: "actions" },
              data.viewable ? h("button", { type: "button", class: "btn small ghost", text: "View", onclick: () => view(version) }) : null,
              h("button", { type: "button", class: "btn small", text: "Restore", onclick: () => restore(version) }))))),
        ),
      );
    };
    const view = async (version) => {
      try {
        const snapshot = await api("GET", `/history/${enc(version.id)}?doc=${enc(doc)}`);
        put(body, 
          h("button", { type: "button", class: "btn small ghost", text: "← All versions", onclick: showList }),
          h("p", { class: "hint", text: `Saved ${formatTime(version.time)}` }),
          h("pre", { class: "preview", text: snapshot.text }),
        );
      } catch (error) {
        fail(error);
      }
    };
    const restore = async (version) => {
      const ok = await confirmBox("Restore this version?", `Replace the current ${title} with the version saved ${formatTime(version.time)}. The current one is kept in the history.`, { confirm: "Restore" });
      if (!ok) return;
      try {
        await api("POST", `/history/${enc(version.id)}/restore?doc=${enc(doc)}`, { etag });
        dialog.close();
        toast("Version restored.");
        if (onRestored) onRestored();
      } catch (error) {
        if (error.status === 422) toast(`${error.message}: ${(error.data.errors || []).join("; ")}`, "error");
        else fail(error);
      }
    };
    showList();
    const { close } = modal({ title: `History — ${title}`, body, wide: true });
    const dialog = { close };
  }

  // -- sign in and shell ---------------------------------------------------------

  const NAV = [
    ["overview", "Overview", "#/"],
    ["config", "Configuration", "#/config/general"],
    ["users", "Users", "#/users"],
    ["skills", "Skills", "#/skills"],
    ["installer", "Installer", "#/installer"],
  ];
  let main = null;
  let navLinks = {};
  let current = null;
  let currentHash = location.hash;

  function renderLogin(message) {
    current = null;
    main = null;
    const app = document.getElementById("app");
    app.className = "login";
    const input = h("input", { type: "password", id: "admin-token", autocomplete: "current-password", spellcheck: "false", required: true });
    const error = h("p", { class: "form-error", role: "alert", hidden: !message, text: message || "" });
    const button = h("button", { type: "submit", class: "btn primary block", text: "Sign in" });
    const form = h("form", { class: "card login-card", novalidate: true },
      h("h1", { text: "OpenCode Config" }),
      h("p", { class: "muted", text: "Admin panel. Sign in with the admin token (OCS_ADMIN_TOKEN)." }),
      field("Admin token", input),
      error,
      button);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      button.disabled = true;
      error.hidden = true;
      try {
        await api("POST", "/session", { token: input.value });
        input.value = "";
        await start();
      } catch (problem) {
        error.textContent = problem.message;
        error.hidden = false;
        button.disabled = false;
        input.select();
      }
    });
    put(app, form);
    input.focus();
  }

  async function signOut() {
    if (current && current.dirty && current.dirty() && !(await confirmBox("Sign out?", "You have unsaved changes; they will be lost.", { confirm: "Sign out", danger: true }))) return;
    await api("DELETE", "/session").catch(() => null);
    renderLogin("Signed out.");
  }

  function renderShell(overview) {
    const app = document.getElementById("app");
    app.className = "shell";
    navLinks = {};
    main = h("main", { id: "main", tabindex: "-1" });
    put(app, 
      h("header", { class: "top" },
        h("div", { class: "brand" }, h("span", { class: "logo", "aria-hidden": "true", text: "◆" }), h("span", { text: "OpenCode Config" }), h("span", { class: "tag", text: "Admin" })),
        h("span", { class: "origin", title: "Public address of this server", text: overview.base_url }),
        h("button", { type: "button", class: "btn ghost small", text: "Sign out", onclick: signOut })),
      h("nav", { class: "side", "aria-label": "Sections" }, NAV.map(([id, label, href]) => (navLinks[id] = h("a", { href, text: label })))),
      main,
    );
    if (!document.getElementById("ocs-actions")) {
      document.body.append(h("datalist", { id: "ocs-actions" }, KNOWN_ACTIONS.map((action) => h("option", { value: action }))));
    }
  }

  function parseHash() {
    const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
    return { page: parts[0] || "overview", sub: parts.slice(1) };
  }

  async function route() {
    if (!main) return;
    const { page, sub } = parseHash();
    const name = PAGES[page] ? page : "overview";
    if (current && current.name === name && current.navigate) {
      current.navigate(sub);
      currentHash = location.hash;
      return;
    }
    if (current && current.dirty && current.dirty() && !window.confirm("You have unsaved changes. Leave this page without saving?")) {
      history.replaceState(null, "", currentHash || "#/");
      return;
    }
    for (const [id, link] of Object.entries(navLinks)) {
      link.classList.toggle("active", id === name);
      if (id === name) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    current = null;
    currentHash = location.hash;
    put(main, h("p", { class: "muted", text: "Loading…" }));
    try {
      current = { name, ...(await PAGES[name](sub)) };
    } catch (error) {
      if (error.status !== 401 && main) put(main, banner("error", "This page could not load", [error.message]));
    }
  }

  window.addEventListener("hashchange", route);
  window.addEventListener("beforeunload", (event) => {
    if (current && current.dirty && current.dirty()) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  // -- overview --------------------------------------------------------------------

  async function overviewPage() {
    const overview = await api("GET", "/overview");
    const health = overview.health;
    const features = overview.features;
    const fileRows = [
      ["Configuration", overview.files.config],
      ["Users", overview.files.users],
      ["Skills", overview.files.skills],
      ["Installer", overview.files.bootstrap],
    ];
    put(main, 
      pageHead("Overview", "What this server hands to every employee's OpenCode, and whether it is healthy."),
      h("div", { class: "grid two" },
        h("section", { class: "card" },
          h("h2", { text: "Status" }),
          h("div", { class: `status ${health.status}` }, h("span", { class: "dot" }), h("strong", { text: health.status === "healthy" ? "Healthy" : "Needs attention" })),
          health.problems.length
            ? h("ul", { class: "problems" }, health.problems.map((problem) => h("li", { text: problem })))
            : h("p", { class: "muted", text: "No problems detected." })),
        h("section", { class: "card" },
          h("h2", { text: "Connect a machine" }),
          h("p", { text: "Employees run this once and sign in from the browser that opens:" }),
          copyBlock(overview.login_command),
          h("p", { class: "hint", text: "OpenCode then fetches the configuration at start-up and every 10 minutes." }))),
      h("div", { class: "grid four" },
        h("a", { class: "card stat", href: "#/users" }, h("span", { text: "Users" }), h("strong", { text: String(health.users) })),
        h("a", { class: "card stat", href: "#/skills" }, h("span", { text: "Published skills" }), h("strong", { text: features.skills ? String(health.skills) : "Off" })),
        h("div", { class: "card stat" }, h("span", { text: "Session lifetime" }), h("strong", { text: `${features.token_ttl_days} days` })),
        h("div", { class: "card stat" }, h("span", { text: "Bifrost virtual keys" }), h("strong", { text: features.bifrost ? `On (${features.vk_token_ttl_days} d)` : "Off" }))),
      h("section", { class: "card" },
        h("h2", { text: "Files" }),
        h("p", { class: "hint", text: "The panel edits these files directly; editing them by hand still works, and the server picks changes up without a restart." }),
        h("table", { class: "list" },
          h("tbody", {}, fileRows.map(([label, info]) => h("tr", {},
            h("td", { text: label }),
            h("td", { class: "path", text: info.path }),
            h("td", { class: "actions" }, h("span", { class: info.writable ? "chip ok" : "chip warn", text: info.writable ? "Editable" : "Read-only" }))))))),
    );
    return {};
  }

  // -- configuration -----------------------------------------------------------------

  const TABS = [
    ["general", "General"],
    ["agents", "Agents"],
    ["commands", "Commands"],
    ["mcp", "MCP servers"],
    ["permissions", "Permissions"],
    ["plugins", "Plugins"],
    ["advanced", "Advanced"],
  ];
  const FORM_KEYS = new Set([
    "$schema", "model", "default_agent", "share", "update", "snapshots", "shell", "instructions", "skills",
    "agents", "commands", "mcp", "permissions", "policies", "plugins",
  ]);
  const NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]*$/;

  /** Drop the empty rows the list editors leave behind. */
  function tidy(document) {
    for (const key of ["instructions", "skills"]) {
      if (Array.isArray(document[key])) document[key] = document[key].filter((item) => item.trim() !== "");
    }
    for (const agent of Object.values(document.agents || {})) {
      if (Array.isArray(agent.permissions)) agent.permissions = agent.permissions.filter((rule) => rule.action);
    }
    if (Array.isArray(document.permissions)) document.permissions = document.permissions.filter((rule) => rule.action);
    return document;
  }

  async function configPage(sub) {
    let cfg = await api("GET", "/config");
    let draft = clone(cfg.document || {});
    let textDraft = cfg.text;
    let dirty = null; // null | "forms" | "text"
    let tab = TABS.some(([id]) => id === sub[0]) ? sub[0] : "general";
    let feedback = null;
    const selection = { agents: null, commands: null, mcp: null };

    const problems = h("div");
    const tabs = h("nav", { class: "tabs", "aria-label": "Configuration sections" });
    const body = h("div", { class: "tab-body" });
    const saveBar = h("div", { class: "savebar", hidden: true });
    const historyButton = h("button", {
      type: "button", class: "btn", text: "History",
      onclick: () => openHistory("config", { title: cfg.file, etag: cfg.etag, onRestored: reload }).catch(fail),
    });
    const head = pageHead("Configuration", null, historyButton);
    const subtitle = h("p");
    head.firstChild.append(subtitle);
    put(main, head, problems, tabs, body, saveBar);

    const changed = () => { if (dirty !== "forms") { dirty = "forms"; renderSaveBar(); } };

    function load(state) {
      cfg = state;
      draft = clone(cfg.document || {});
      textDraft = cfg.text;
      dirty = null;
      renderAll();
    }

    async function reload() {
      try {
        load(await api("GET", "/config"));
      } catch (error) {
        fail(error);
      }
    }

    function discard() {
      draft = clone(cfg.document || {});
      textDraft = cfg.text;
      dirty = null;
      feedback = null;
      renderAll();
    }

    function payload() {
      return dirty === "text" ? { text: textDraft } : { document: tidy(clone(draft)) };
    }

    async function check() {
      try {
        const result = await api("POST", "/config/check", payload());
        feedback = { kind: result.errors.length ? "error" : "info", title: result.errors.length ? "This draft would be rejected" : "This draft is valid", errors: result.errors, warnings: result.warnings };
        renderProblems();
        modal({
          title: "What employees receive",
          wide: true,
          body: [
            h("p", { class: "hint", text: "The draft as served by GET /config/opencode.json: {server_url} and {session_token} are filled in; {env:…} and {file:…} stay for each employee's machine to resolve." }),
            h("pre", { class: "preview", text: JSON.stringify(result.rendered, null, 2) }),
          ],
        });
      } catch (error) {
        if (error.status === 422) {
          feedback = { kind: "error", title: error.message, errors: error.data.errors || [], warnings: [] };
          renderProblems();
        } else fail(error);
      }
    }

    async function save() {
      if (dirty === "forms" && cfg.has_comments) {
        const ok = await confirmBox(
          "Remove the comments?",
          `Saving from the forms rewrites ${cfg.file} as plain JSON, so its comments are removed. The current version stays in History, and saving from the Advanced tab keeps comments.`,
          { confirm: "Save anyway" });
        if (!ok) return;
      }
      try {
        const result = await api("PUT", "/config", { ...payload(), etag: cfg.etag });
        feedback = null;
        load(result);
        toast("Saved. Employees get it on their next refresh (within 10 minutes).");
      } catch (error) {
        if (error.status === 409 && /changed since/.test(error.message)) conflict();
        else if (error.status === 422) {
          feedback = { kind: "error", title: error.message, errors: error.data.errors || [], warnings: error.data.warnings || [] };
          renderProblems();
          problems.scrollIntoView({ behavior: "smooth", block: "start" });
        } else fail(error);
      }
    }

    function conflict() {
      modal({
        title: "Changed by someone else",
        body: h("p", { text: `${cfg.file} was saved elsewhere (another admin, or an edit on disk) after you opened it. Reload to get the current version; your unsaved changes here will be lost.` }),
        actions: [
          { label: "Keep editing", kind: "ghost" },
          { label: "Reload", kind: "primary", onClick: reload },
        ],
      });
    }

    function renderProblems() {
      const blocks = [];
      if (!cfg.writable) blocks.push(readOnlyBanner("The configuration", cfg.file));
      if (cfg.error) blocks.push(banner("error", "The file cannot be read as JSON", [cfg.error], h("p", { text: "Fix it in the Advanced tab; employees keep their last good copy meanwhile." })));
      if (cfg.errors.length) blocks.push(banner("error", "The stored file has errors OpenCode rejects", cfg.errors));
      if (feedback) blocks.push(banner(feedback.kind, feedback.title, [...feedback.errors, ...feedback.warnings]));
      else if (cfg.warnings.length) blocks.push(banner("warn", "Warnings", cfg.warnings));
      put(problems, ...blocks);
    }

    function renderSaveBar() {
      saveBar.hidden = !dirty;
      if (!dirty) return;
      put(saveBar, 
        h("span", { class: "grow", text: dirty === "text" ? "Unsaved changes in the text editor" : "Unsaved changes" }),
        h("button", { type: "button", class: "btn ghost", text: "Discard", onclick: discard }),
        h("button", { type: "button", class: "btn", text: "Check & preview", onclick: check }),
        h("button", { type: "button", class: "btn primary", text: "Save", disabled: !cfg.writable, onclick: save }),
      );
    }

    function renderTabs() {
      put(tabs, ...TABS.map(([id, label]) => h("a", {
        href: `#/config/${id}`, class: id === tab ? "active" : null, "aria-current": id === tab ? "page" : null, text: label,
      })));
    }

    function renderBody() {
      if (cfg.error && tab !== "advanced") {
        put(body, banner("info", "Forms are unavailable until the file parses", [], h("div", { class: "banner-actions" }, h("a", { class: "btn", href: "#/config/advanced", text: "Open the text editor" }))));
        return;
      }
      if (dirty === "text" && tab !== "advanced") {
        put(body, banner("info", "You have unsaved changes in the text editor", ["Save or discard them before using the forms."],
          h("div", { class: "banner-actions" }, h("a", { class: "btn", href: "#/config/advanced", text: "Back to the text editor" }))));
        return;
      }
      if (dirty === "forms" && tab === "advanced") {
        put(body, banner("info", "You have unsaved changes in the forms", ["Save or discard them before editing the file as text."]));
        return;
      }
      const render = { general, agents, commands, mcp, permissions, plugins, advanced }[tab];
      put(body, render());
    }

    function renderAll() {
      subtitle.textContent = `${cfg.file}${cfg.has_comments ? " · has comments" : ""} — served below each employee's own settings: anything they set themselves wins.`;
      historyButton.disabled = false;
      renderProblems();
      renderTabs();
      renderBody();
      renderSaveBar();
    }

    // ---- tabs

    function general() {
      const agents = Object.keys(draft.agents || {});
      const otherKeys = Object.keys(draft).filter((key) => !FORM_KEYS.has(key));
      const modelControl = draft.model && typeof draft.model === "object"
        ? h("p", { class: "muted", text: `Set as an object (${JSON.stringify(draft.model)}); edit it in Advanced.` })
        : textInput(draft.model, (value) => { assign(draft, "model", value.trim()); changed(); }, { placeholder: "provider/model, e.g. bifrost/claude-sonnet-4-5" });
      return [
        h("section", { class: "card" },
          h("div", { class: "grid two" },
            field("Default model", modelControl, "Used when neither the session nor the agent picks one."),
            field("Default agent", textInput(draft.default_agent, (value) => { assign(draft, "default_agent", value.trim()); changed(); }, { list: "ocs-agent-names", placeholder: "build" }),
              "Primary agent new sessions start with."),
            field("Session sharing", select([["", "OpenCode default"], ["disabled", "Disabled"], ["manual", "Manual"], ["auto", "Automatic"]], draft.share, (value) => { assign(draft, "share", value); changed(); })),
            field("Updates", select([["", "OpenCode default"], ["disable", "Never update"], ["notify", "Notify only"], ["auto", "Install automatically"]], draft.update, (value) => { assign(draft, "update", value); changed(); })),
            field("Snapshots (undo / revert)", select([["", "OpenCode default"], ["true", "On"], ["false", "Off"]], draft.snapshots === undefined ? "" : String(draft.snapshots),
              (value) => { assign(draft, "snapshots", value === "" ? undefined : value === "true"); changed(); })),
            field("Shell", textInput(draft.shell, (value) => { assign(draft, "shell", value.trim()); changed(); }, { placeholder: "OpenCode default" }))),
          h("datalist", { id: "ocs-agent-names" }, agents.map((name) => h("option", { value: name })))),
        h("section", { class: "card" },
          h("h2", { text: "Instructions" }),
          h("p", { class: "hint", text: "Paths or URLs whose content every session gets as ambient instructions." }),
          stringList(draft, "instructions", { placeholder: "https://… or /absolute/path.md", onChange: changed })),
        h("section", { class: "card" },
          h("h2", { text: "Skill sources" }),
          h("p", { class: "hint", text: "{server_url}/skills/{session_token}/ publishes the skills managed in the Skills section. Add other URLs or paths if you have them." }),
          stringList(draft, "skills", { placeholder: "{server_url}/skills/{session_token}/", onChange: changed })),
        otherKeys.length
          ? h("section", { class: "card" },
            h("h2", { text: "Other settings" }),
            h("p", { class: "hint", text: "Present in the file without a form here. They are kept as they are; edit them in Advanced." }),
            h("div", { class: "chips" }, otherKeys.map((key) => h("span", { class: "chip", text: key }))))
          : null,
      ];
    }

    /** A named collection (agents, commands, MCP servers) as list + editor. */
    function collection({ kind, holder, key, blank, summary, editor, addLabel, empty }) {
      const map = holder()?.[key] || {};
      const names = Object.keys(map);
      if (selection[kind] && !(selection[kind] in map)) selection[kind] = null;
      if (!selection[kind] && names.length) selection[kind] = names[0];
      const selected = selection[kind];
      const pick = (name) => { selection[kind] = name; renderBody(); };
      const add = async () => {
        const name = await promptName(addLabel, { taken: names });
        if (!name) return;
        const target = holder(true);
        target[key] ||= {};
        target[key][name] = blank(name);
        changed();
        pick(name);
      };
      const rename = async () => {
        const name = await promptName(`Rename ${selected}`, { value: selected, taken: names });
        if (!name || name === selected) return;
        renameKey(map, selected, name);
        changed();
        pick(name);
      };
      const remove = async () => {
        if (!(await confirmBox(`Delete ${selected}?`, "It disappears from the configuration when you save.", { confirm: "Delete", danger: true }))) return;
        delete map[selected];
        if (!Object.keys(map).length) delete holder()[key];
        selection[kind] = null;
        changed();
        renderBody();
      };
      return h("div", { class: "master-detail" },
        h("div", { class: "collection" },
          names.length
            ? names.map((name) => h("button", { type: "button", class: name === selected ? "item active" : "item", "aria-pressed": String(name === selected), onclick: () => pick(name) },
              h("span", { class: "item-name", text: name }),
              h("span", { class: "item-sub", text: summary(map[name]) || " " })))
            : h("p", { class: "muted", text: empty }),
          h("button", { type: "button", class: "btn small", text: `+ ${addLabel}`, onclick: add })),
        selected
          ? h("div", { class: "detail" },
            h("div", { class: "detail-head" },
              h("h3", { text: selected }),
              h("div", { class: "row-actions" },
                h("button", { type: "button", class: "btn small ghost", text: "Rename", onclick: rename }),
                h("button", { type: "button", class: "btn small danger", text: "Delete", onclick: remove }))),
            editor(map[selected]))
          : h("div", { class: "detail empty", text: names.length ? "Select one to edit it." : "Nothing here yet." }));
    }

    function extrasNote(object, known) {
      const extras = Object.keys(object).filter((key) => !known.includes(key));
      return extras.length ? h("p", { class: "hint", text: `Also set: ${extras.join(", ")} — kept as is; edit in Advanced.` }) : null;
    }

    function modelField(object, hint) {
      if (object.model && typeof object.model === "object") {
        return field("Model", h("p", { class: "muted", text: "Set as an object; edit it in Advanced." }));
      }
      return field("Model", textInput(object.model, (value) => { assign(object, "model", value.trim()); changed(); }, { placeholder: "inherit" }), hint);
    }

    function agents() {
      return [
        h("p", { class: "hint", text: "Override built-in agents (build, plan, …) by using their name, or define new ones. Employees' own agent settings win field by field." }),
        collection({
          kind: "agents",
          holder: () => draft,
          key: "agents",
          addLabel: "New agent",
          empty: "No agents defined.",
          blank: () => ({ description: "", mode: "subagent" }),
          summary: (agent) => [agent.mode, agent.disabled ? "disabled" : null, agent.description].filter(Boolean).join(" · "),
          editor: (agent) => {
            const swatch = h("span", { class: "swatch", "aria-hidden": "true" });
            const paint = (value) => { swatch.style.backgroundColor = /^#[0-9a-fA-F]{6}$/.test(value || "") ? value : "transparent"; };
            paint(agent.color);
            return h("div", {},
              field("Description", textInput(agent.description, (value) => { assign(agent, "description", value); changed(); }),
                "For subagents this is how the main agent decides when to delegate."),
              h("div", { class: "grid two" },
                field("Mode", select([["", "OpenCode default"], ["primary", "Primary — picked by the user"], ["subagent", "Subagent — delegated to"], ["all", "Both"]], agent.mode, (value) => { assign(agent, "mode", value); changed(); })),
                modelField(agent, "Leave empty to use the session's model."),
                field("Colour", h("div", { class: "with-swatch" }, swatch,
                  textInput(agent.color, (value) => { assign(agent, "color", value.trim()); paint(value.trim()); changed(); }, { placeholder: "#3b82f6", "aria-label": "Colour" }))),
                field("Max steps", h("input", {
                  type: "number", min: "1", value: agent.steps ?? "", "aria-label": "Max steps",
                  oninput: (event) => { assign(agent, "steps", event.target.value === "" ? undefined : Number(event.target.value)); changed(); },
                }))),
              h("div", { class: "checks" },
                checkbox("Hidden from the agent picker", agent.hidden, (value) => { assign(agent, "hidden", value || undefined); changed(); }),
                checkbox("Disabled", agent.disabled, (value) => { assign(agent, "disabled", value || undefined); changed(); })),
              field("System prompt", codeArea(agent.system ?? "", (value) => { assign(agent, "system", value); changed(); }, { rows: 12, onSave: save, label: "System prompt", prose: true })),
              h("h4", { text: "Permissions for this agent" }),
              h("p", { class: "hint", text: "Checked after the global rules; the last matching rule wins." }),
              rulesEditor(agent, "permissions", { onChange: changed }),
              extrasNote(agent, ["description", "mode", "model", "system", "color", "steps", "hidden", "disabled", "permissions"]));
          },
        }),
      ];
    }

    function commands() {
      return [
        h("p", { class: "hint", text: "Slash commands every employee gets, e.g. /corp-review." }),
        collection({
          kind: "commands",
          holder: () => draft,
          key: "commands",
          addLabel: "New command",
          empty: "No commands defined.",
          blank: () => ({ description: "", template: "" }),
          summary: (command) => command.description || "",
          editor: (command) => h("div", {},
            field("Description", textInput(command.description, (value) => { assign(command, "description", value); changed(); })),
            field("Prompt template", codeArea(command.template ?? "", (value) => { command.template = value; changed(); }, { rows: 8, onSave: save, label: "Prompt template", prose: true }),
              "$ARGUMENTS is replaced with whatever follows the command."),
            h("div", { class: "grid two" },
              field("Agent", textInput(command.agent, (value) => { assign(command, "agent", value.trim()); changed(); }, { list: "ocs-agent-list", placeholder: "current agent" })),
              modelField(command, "Leave empty to use the current model.")),
            h("datalist", { id: "ocs-agent-list" }, Object.keys(draft.agents || {}).map((name) => h("option", { value: name }))),
            h("div", { class: "checks" },
              checkbox("Run as a subagent", command.subagent ?? command.subtask, (value) => {
                delete command.subtask;
                assign(command, "subagent", value || undefined);
                changed();
              })),
            extrasNote(command, ["description", "template", "agent", "model", "subagent", "subtask"])),
        }),
      ];
    }

    function mcp() {
      const ensure = (create) => {
        if (create && !draft.mcp) draft.mcp = {};
        return draft.mcp;
      };
      const timeout = (draft.mcp && draft.mcp.timeout) || {};
      const timeoutInput = (name, label) => field(label, h("input", {
        type: "number", min: "1", value: timeout[name] ?? "", placeholder: "default",
        oninput: (event) => {
          const holder = ensure(true);
          holder.timeout ||= {};
          assign(holder.timeout, name, event.target.value === "" ? undefined : Number(event.target.value));
          if (!Object.keys(holder.timeout).length) delete holder.timeout;
          changed();
        },
      }));
      const LOCAL = ["command", "cwd", "environment"];
      const REMOTE = ["url", "headers", "oauth"];
      return [
        h("p", { class: "hint", text: "Servers merge by name; an employee's server with the same name replaces this one. Never paste a real secret: everyone who signs in can read this document — use {env:NAME}." }),
        collection({
          kind: "mcp",
          holder: ensure,
          key: "servers",
          addLabel: "New server",
          empty: "No MCP servers defined.",
          blank: () => ({ type: "remote", url: "" }),
          summary: (server) => [server.type, server.disabled ? "disabled" : null, server.type === "remote" ? server.url : (server.command || [])[0]].filter(Boolean).join(" · "),
          editor: (server) => {
            const typeSwitch = select([["remote", "Remote — reached by URL"], ["local", "Local — runs a command"]], server.type, (value) => {
              server.type = value;
              for (const key of value === "local" ? REMOTE : LOCAL) delete server[key];
              if (value === "local") server.command ||= [];
              else server.url ||= "";
              changed();
              renderBody();
            });
            const specific = server.type === "local"
              ? [
                field("Command", codeArea((server.command || []).join("\n"), (value) => { server.command = value.split("\n").map((part) => part.trim()).filter(Boolean); changed(); }, { rows: 4, label: "Command" }),
                  "One argument per line, program first: npx, -y, @scope/server…"),
                field("Working directory", textInput(server.cwd, (value) => { assign(server, "cwd", value.trim()); changed(); }, { placeholder: "workspace directory" })),
                h("h4", { text: "Environment" }),
                keyValues(server, "environment", { keyPlaceholder: "NAME", valuePlaceholder: "value or {env:NAME}", onChange: changed }),
              ]
              : [
                field("URL", textInput(server.url, (value) => { server.url = value.trim(); changed(); }, { placeholder: "https://mcp.example.com/mcp" })),
                h("h4", { text: "Headers" }),
                keyValues(server, "headers", { keyPlaceholder: "authorization", valuePlaceholder: "Bearer {env:TOKEN}", onChange: changed }),
                server.oauth && typeof server.oauth === "object"
                  ? field("OAuth", h("p", { class: "muted", text: "Configured as an object; edit it in Advanced." }))
                  : field("OAuth", select([["", "Automatic"], ["false", "Disabled"]], server.oauth === false ? "false" : "", (value) => { assign(server, "oauth", value === "false" ? false : undefined); changed(); })),
              ];
            return h("div", {},
              field("Type", typeSwitch),
              specific,
              h("div", { class: "grid two" },
                field("Code Mode", select([["", "Default (on)"], ["true", "On"], ["false", "Off"]], server.codemode === undefined ? "" : String(server.codemode),
                  (value) => { assign(server, "codemode", value === "" ? undefined : value === "true"); changed(); })),
                field("Protocol", select([["", "Default (legacy)"], ["legacy", "legacy"], ["auto", "auto"], ["2026-07-28", "2026-07-28"]], server.protocol, (value) => { assign(server, "protocol", value); changed(); }))),
              h("div", { class: "checks" }, checkbox("Disabled", server.disabled, (value) => { assign(server, "disabled", value || undefined); changed(); })),
              extrasNote(server, ["type", ...LOCAL, ...REMOTE, "codemode", "protocol", "disabled"]));
          },
        }),
        h("section", { class: "card" },
          h("h2", { text: "Timeouts for every server" }),
          h("p", { class: "hint", text: "In milliseconds. Empty means OpenCode's default." }),
          h("div", { class: "grid two" },
            timeoutInput("startup", "Start-up"),
            timeoutInput("catalog", "Tool discovery"),
            timeoutInput("execution", "Tool execution"))),
      ];
    }

    function permissions() {
      return [
        h("section", { class: "card" },
          h("h2", { text: "Tool permissions" }),
          h("p", { class: "hint", text: "Checked in order and the last matching rule wins. Employees' own rules come after these, so this is a sensible default, not a lock — enforce hard limits outside OpenCode." }),
          rulesEditor(draft, "permissions", { onChange: changed })),
        h("section", { class: "card" },
          h("h2", { text: "Policies" }),
          h("p", { class: "hint", text: "Allow or deny whole providers, tools or integrations, e.g. provider.use · openai/* · deny." }),
          rulesEditor(draft, "policies", { onChange: changed, actions: ["provider.use", "tool.use", "integration.use"], effects: ["allow", "deny"], defaultEffect: "deny" })),
      ];
    }

    function plugins() {
      const list = h("div", { class: "list-editor" });
      const render = () => {
        const entries = Array.isArray(draft.plugins) ? draft.plugins : [];
        put(list, 
          ...entries.map((entry, index) => {
            const spec = typeof entry === "string" ? entry : entry.package || "";
            const options = typeof entry === "object" && entry.options ? JSON.stringify(entry.options, null, 2) : "";
            const optionsArea = codeArea(options, (value) => {
              if (!value.trim()) {
                draft.plugins[index] = spec;
                optionsArea.removeAttribute("aria-invalid");
                changed();
                return;
              }
              try {
                const parsed = JSON.parse(value);
                if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("not an object");
                draft.plugins[index] = { package: currentSpec(), options: parsed };
                optionsArea.removeAttribute("aria-invalid");
                changed();
              } catch {
                optionsArea.setAttribute("aria-invalid", "true");
              }
            }, { rows: 4, label: `Options for ${spec || "plugin"}` });
            optionsArea.hidden = !options;
            const specInput = textInput(spec, (value) => {
              const current = draft.plugins[index];
              draft.plugins[index] = typeof current === "object" ? { ...current, package: value.trim() } : value.trim();
              changed();
            }, { placeholder: "npm spec, e.g. opencode-apex@latest", "aria-label": "Plugin package" });
            const currentSpec = () => specInput.value.trim();
            return h("div", { class: "plugin-row" },
              specInput,
              h("div", { class: "row-actions" },
                h("button", { type: "button", class: "btn small ghost", text: "Options", "aria-expanded": String(!optionsArea.hidden), onclick: (event) => { optionsArea.hidden = !optionsArea.hidden; event.target.setAttribute("aria-expanded", String(!optionsArea.hidden)); if (!optionsArea.hidden) optionsArea.focus(); } }),
                iconButton("Move up", "↑", () => { move(entries, index, -1); changed(); render(); }, index === 0),
                iconButton("Move down", "↓", () => { move(entries, index, 1); changed(); render(); }, index === entries.length - 1),
                iconButton("Remove", "×", () => { entries.splice(index, 1); changed(); render(); })),
              optionsArea);
          }),
          h("button", { type: "button", class: "btn small", text: "+ Add plugin", onclick: () => { (draft.plugins ||= []).push(""); changed(); render(); list.querySelector(".plugin-row:last-of-type input")?.focus(); } }),
        );
      };
      render();
      return h("section", { class: "card" },
        h("h2", { text: "Server plugins" }),
        h("p", { class: "hint", text: "Added to every employee's plugin list. Use an npm spec or an absolute path (a relative path resolves against each employee's working directory). An employee can opt out with \"-package\" in their own config. TUI plugins and other local tooling go in the Installer." }),
        list);
    }

    function advanced() {
      const area = codeArea(textDraft, (value) => {
        textDraft = value;
        const next = value === cfg.text ? null : "text";
        if (next !== dirty) { dirty = next; renderSaveBar(); }
      }, { rows: 30, onSave: save, label: cfg.file });
      return [
        h("p", { class: "hint", text: "The file exactly as stored, comments included. {server_url} and {session_token} are filled in for each employee by this server; {env:…} and {file:…} are resolved on their machine." }),
        area,
      ];
    }

    renderAll();
    return {
      dirty: () => Boolean(dirty),
      navigate: (next) => {
        tab = TABS.some(([id]) => id === next[0]) ? next[0] : "general";
        renderTabs();
        renderBody();
      },
    };
  }

  // -- users -------------------------------------------------------------------------

  function apiKeyDialog(name, key) {
    modal({
      title: `API key for ${name}`,
      body: [
        h("p", { text: "Copy it now and hand it over securely: only its digest is stored, so it cannot be shown again." }),
        copyBlock(key),
        h("p", { class: "hint", text: "The employee pastes it on the sign-in page, or sets OPENCODE_CORP_TOKEN for unattended machines." }),
      ],
      actions: [{ label: "Done", kind: "primary" }],
    });
  }

  async function usersPage() {
    let data = await api("GET", "/users");

    const reload = async () => {
      data = await api("GET", "/users");
      render();
    };

    const addUser = () => {
      const name = h("input", { type: "text", spellcheck: "false", autocomplete: "off", placeholder: "ana.ruiz" });
      const password = h("input", { type: "password", autocomplete: "new-password" });
      let withKey = false;
      const error = h("p", { class: "form-error", role: "alert", hidden: true });
      modal({
        title: "Add a user",
        body: [
          field("Username", name, "Letters, digits and . _ @ + - — what they type on the sign-in page."),
          field("Password", password, "Optional, at least 12 characters. Leave empty for an API-key-only account."),
          h("div", { class: "checks" }, checkbox("Create an API key now", false, (value) => { withKey = value; })),
          error,
        ],
        actions: [
          { label: "Cancel", kind: "ghost" },
          {
            label: "Add user", kind: "primary",
            onClick: async () => {
              try {
                const result = await api("POST", "/users", { name: name.value.trim(), password: password.value || null, api_key: withKey });
                await reload();
                toast(`Added ${result.user.name}.`);
                if (result.api_key) apiKeyDialog(result.user.name, result.api_key);
                return false;
              } catch (problem) {
                error.textContent = problem.message;
                error.hidden = false;
                return true;
              }
            },
          },
        ],
      });
      name.focus();
    };

    const manage = (initial) => {
      let user = initial;
      const body = h("div");
      const act = async (method, path, payload, message) => {
        try {
          const result = await api(method, path, payload);
          if (result.user) user = result.user;
          await reload();
          renderManage();
          if (message) toast(message);
          return result;
        } catch (error) {
          fail(error);
          return null;
        }
      };
      const base = `/users/${enc(user.name)}`;
      const renderManage = () => {
        const password = h("input", { type: "password", autocomplete: "new-password", placeholder: "new password, 12+ characters", "aria-label": "New password" });
        put(body, 
          h("section", {},
            h("h3", { text: "Password" }),
            h("p", { class: "hint", text: user.password ? "A password is set." : "No password: this account signs in with API keys only." }),
            h("div", { class: "inline-form" }, password,
              h("button", {
                type: "button", class: "btn", text: user.password ? "Change" : "Set",
                onclick: async () => {
                  if (await act("PATCH", base, { password: password.value }, "Password updated.")) password.value = "";
                },
              }))),
          h("section", {},
            h("h3", { text: "API keys" }),
            user.api_keys.length
              ? user.api_keys.map((fingerprint) => h("div", { class: "key-row" },
                h("code", { text: `key ${fingerprint}…` }),
                h("button", {
                  type: "button", class: "btn small danger", text: "Remove",
                  onclick: async () => {
                    if (await confirmBox("Remove this API key?", "Anything still using it can no longer sign in. Sessions it already opened keep working until you sign the user out everywhere.", { confirm: "Remove", danger: true })) {
                      await act("DELETE", `${base}/api-keys/${enc(fingerprint)}`, undefined, "API key removed.");
                    }
                  },
                })))
              : h("p", { class: "hint", text: "No API keys." }),
            h("button", {
              type: "button", class: "btn small", text: "+ New API key",
              onclick: async () => {
                const result = await act("POST", `${base}/api-keys`, undefined);
                if (result) apiKeyDialog(user.name, result.api_key);
              },
            })),
          h("section", {},
            h("h3", { text: "Sessions" }),
            h("p", { class: "hint", text: "Ends every OpenCode session of this user on every machine at once; they must run opencode auth login again." }),
            h("button", {
              type: "button", class: "btn", text: "Sign out everywhere",
              onclick: async () => {
                if (await confirmBox(`Sign ${user.name} out everywhere?`, "Their OpenCode stops receiving the configuration until they sign in again.", { confirm: "Sign out everywhere", danger: true })) {
                  await act("POST", `${base}/revoke`, undefined, "Every session was ended.");
                }
              },
            })),
          h("section", {},
            h("h3", { text: "Account" }),
            h("div", { class: "editor-actions" },
              h("button", {
                type: "button", class: "btn", text: user.disabled ? "Enable account" : "Disable account",
                onclick: () => act("PATCH", base, { disabled: !user.disabled }, user.disabled ? "Account enabled." : "Account disabled: its sessions stop working now."),
              }),
              h("button", {
                type: "button", class: "btn danger", text: "Delete user",
                onclick: async () => {
                  if (await confirmBox(`Delete ${user.name}?`, "Their sessions stop working immediately. users.json keeps the previous version in History.", { confirm: "Delete", danger: true })) {
                    if (await act("DELETE", base, undefined, `Deleted ${user.name}.`)) dialog.close();
                  }
                },
              }))),
        );
      };
      renderManage();
      const dialog = modal({ title: user.name, body, actions: [{ label: "Close", kind: "ghost" }] });
    };

    function render() {
      const rows = data.users.map((user) => h("tr", {},
        h("td", {}, h("strong", { text: user.name })),
        h("td", {}, h("div", { class: "chips" },
          user.password ? h("span", { class: "chip", text: "password" }) : null,
          user.api_keys.length ? h("span", { class: "chip", text: `${user.api_keys.length} API key${user.api_keys.length > 1 ? "s" : ""}` }) : null,
          !user.password && !user.api_keys.length ? h("span", { class: "chip warn", text: "cannot sign in" }) : null)),
        h("td", {}, h("span", { class: user.disabled ? "chip err" : "chip ok", text: user.disabled ? "Disabled" : "Active" })),
        h("td", { class: "actions" }, h("button", { type: "button", class: "btn small", text: "Manage", onclick: () => manage(user) }))));
      put(main, 
        pageHead("Users", "Accounts that sign in with a password or an API key. Bifrost virtual keys are checked against Bifrost and are not listed here.",
          h("button", { type: "button", class: "btn", text: "History", onclick: () => openHistory("users", { title: data.file, onRestored: reload }).catch(fail) }),
          h("button", { type: "button", class: "btn primary", text: "Add user", disabled: !data.writable || Boolean(data.error), onclick: addUser })),
        data.writable ? null : readOnlyBanner("The user database", data.file),
        data.error ? banner("error", "users.json has a problem", [data.error]) : null,
        h("section", { class: "card" },
          rows.length
            ? h("table", { class: "list" },
              h("thead", {}, h("tr", {}, h("th", { text: "User" }), h("th", { text: "Signs in with" }), h("th", { text: "Status" }), h("th", {}))),
              h("tbody", {}, rows))
            : h("p", { class: "muted", text: "No users yet. Add one, then send them the sign-in command from the Overview." })),
      );
    }

    render();
    return {};
  }

  // -- skills ----------------------------------------------------------------------------

  async function skillsPage(sub) {
    let data = await api("GET", "/skills");
    let selected = sub[0] || (data.skills[0] && data.skills[0].name) || null;
    let file = null; // { path, text, etag, draft }
    const layout = h("div", { class: "master-detail" });
    const notices = h("div");

    const skillOf = (name) => data.skills.find((skill) => skill.name === name) || null;
    const fileDirty = () => Boolean(file) && file.draft !== file.text;

    async function reload() {
      data = await api("GET", "/skills");
      if (selected && !skillOf(selected)) {
        selected = data.skills[0] ? data.skills[0].name : null;
        file = null;
      }
      render();
    }

    async function leaveFile() {
      return !fileDirty() || confirmBox("Discard your changes?", `${file.path} has unsaved changes.`, { confirm: "Discard", danger: true });
    }

    async function choose(name) {
      if (name === selected || !(await leaveFile())) return;
      selected = name;
      file = null;
      history.replaceState(null, "", `#/skills/${enc(name)}`);
      render();
    }

    async function openFile(path) {
      if (file && file.path === path) return;
      if (!(await leaveFile())) return;
      try {
        const result = await api("GET", `/skills/${enc(selected)}/files/${encPath(path)}`);
        file = { ...result, draft: result.text };
        render();
      } catch (error) {
        fail(error);
      }
    }

    async function saveFile() {
      try {
        const result = await api("PUT", `/skills/${enc(selected)}/files/${encPath(file.path)}`, { text: file.draft, etag: file.etag });
        file = { path: file.path, text: file.draft, draft: file.draft, etag: result.etag };
        toast(`Saved ${file.path}. Employees download it on their next refresh.`);
        await reload();
        for (const warning of result.warnings || []) toast(warning, "error");
      } catch (error) {
        if (error.status === 409) toast(error.message, "error");
        else fail(error);
      }
    }

    function newSkill() {
      const name = h("input", { type: "text", spellcheck: "false", autocomplete: "off", placeholder: "corp-standards" });
      const description = h("input", { type: "text", placeholder: "Use when reviewing code against the company's engineering rules" });
      const error = h("p", { class: "form-error", role: "alert", hidden: true });
      modal({
        title: "New skill",
        body: [
          field("Name", name, "Becomes the folder name and the skill's id."),
          field("When should agents use it?", description, "This sentence is all agents see until they load the skill, so make it specific."),
          error,
        ],
        actions: [
          { label: "Cancel", kind: "ghost" },
          {
            label: "Create", kind: "primary",
            onClick: async () => {
              try {
                const result = await api("POST", "/skills", { name: name.value.trim(), description: description.value });
                selected = result.name;
                file = null;
                await reload();
                await openFile("SKILL.md");
                return false;
              } catch (problem) {
                error.textContent = problem.message;
                error.hidden = false;
                return true;
              }
            },
          },
        ],
      });
      name.focus();
    }

    async function newFile() {
      if (!(await leaveFile())) return;
      const skill = skillOf(selected);
      const path = await promptName("New file", {
        label: "Path inside the skill",
        pattern: /^(?!.*(^|\/)\.)[A-Za-z0-9._\-/]+\.[A-Za-z0-9]+$/,
        taken: skill.files.map((item) => item.path),
        hint: `e.g. references/guide.md. Published types: ${data.suffixes.join(" ")}`,
      });
      if (!path) return;
      file = { path, text: "", draft: "", etag: "absent" };
      render();
    }

    async function deleteFile() {
      if (file.etag === "absent") {
        file = null;
        render();
        return;
      }
      if (!(await confirmBox(`Delete ${file.path}?`, "Employees lose it on their next refresh. The content stays in the history.", { confirm: "Delete", danger: true }))) return;
      try {
        await api("DELETE", `/skills/${enc(selected)}/files/${encPath(file.path)}`);
        toast(`Deleted ${file.path}.`);
        file = null;
        await reload();
      } catch (error) {
        fail(error);
      }
    }

    async function deleteSkill() {
      if (!(await confirmBox(`Delete the skill ${selected}?`, "Every file in it is removed and employees lose it on their next refresh. Each file's content stays in the history.", { confirm: "Delete skill", danger: true }))) return;
      try {
        await api("DELETE", `/skills/${enc(selected)}`);
        toast(`Deleted ${selected}.`);
        selected = null;
        file = null;
        await reload();
      } catch (error) {
        fail(error);
      }
    }

    function editor() {
      const area = codeArea(file.draft, (value) => { file.draft = value; saveButton.disabled = !data.writable || !fileDirty(); }, { rows: 24, onSave: () => fileDirty() && saveFile(), label: file.path });
      const saveButton = h("button", { type: "button", class: "btn primary", text: file.etag === "absent" ? "Create file" : "Save", disabled: !data.writable || (!fileDirty() && file.etag !== "absent"), onclick: saveFile });
      return h("div", {},
        h("div", { class: "detail-head" },
          h("h3", { class: "path", text: file.path }),
          h("div", { class: "row-actions" },
            file.etag !== "absent"
              ? h("button", {
                type: "button", class: "btn small ghost", text: "History",
                onclick: () => openHistory(`skill:${selected}/${file.path}`, { title: file.path, etag: file.etag, onRestored: async () => { const path = file.path; file = null; await reload(); await openFile(path); } }).catch(fail),
              })
              : null,
            h("button", { type: "button", class: "btn small danger", text: file.etag === "absent" ? "Cancel" : "Delete", disabled: !data.writable, onclick: deleteFile }))),
        area,
        h("div", { class: "editor-actions" }, saveButton));
    }

    function detail() {
      const skill = skillOf(selected);
      if (!skill) return h("div", { class: "detail empty", text: data.skills.length ? "Select a skill." : "No skills yet." });
      return h("div", { class: "detail" },
        h("div", { class: "detail-head" },
          h("h3", { text: skill.name }),
          h("span", { class: skill.published ? "chip ok" : "chip warn", text: skill.published ? `published · ${skill.version}` : "not published" }),
          h("button", { type: "button", class: "btn small danger", text: "Delete skill", disabled: !data.writable, onclick: deleteSkill })),
        skill.warnings.length ? banner("warn", null, skill.warnings) : null,
        h("div", { class: "files" },
          skill.files.map((item) => h("button", {
            type: "button",
            class: file && file.path === item.path ? "file active" : "file",
            disabled: !item.published,
            title: item.published ? null : "Not published: only small text files with the listed extensions reach employees",
            onclick: () => openFile(item.path),
          }, h("span", { class: "path", text: item.path }), h("span", { class: "muted", text: item.published ? formatBytes(item.size) : "not published" }))),
          h("button", { type: "button", class: "btn small", text: "+ New file", disabled: !data.writable, onclick: newFile })),
        file ? editor() : h("p", { class: "muted", text: "Pick a file to edit it." }));
    }

    function render() {
      put(notices, 
        data.enabled ? null : banner("warn", "Skills are switched off", ["OCS_SKILLS_ENABLED=false: nothing here reaches employees until it is enabled."]),
        data.writable ? null : readOnlyBanner("The skills folder", "skills/"),
      );
      put(layout, 
        h("div", { class: "collection" },
          data.skills.length
            ? data.skills.map((skill) => h("button", { type: "button", class: skill.name === selected ? "item active" : "item", "aria-pressed": String(skill.name === selected), onclick: () => choose(skill.name) },
              h("span", { class: "item-name", text: skill.name }),
              h("span", { class: "item-sub", text: `${skill.files.length} file${skill.files.length === 1 ? "" : "s"}${skill.published ? "" : " · not published"}${skill.warnings.length ? " · ⚠" : ""}` })))
            : h("p", { class: "muted", text: "No skills yet." }),
          h("button", { type: "button", class: "btn small", text: "+ New skill", disabled: !data.writable, onclick: newSkill })),
        detail(),
      );
    }

    put(main, 
      pageHead("Skills", "Instructions agents load on demand. Published to employees through the skill source in the configuration; each change gets a new version, so their copy refreshes."),
      notices,
      layout,
    );
    render();
    return {
      dirty: fileDirty,
      navigate: (next) => { if (next[0] && next[0] !== selected) choose(next[0]); },
    };
  }

  // -- installer --------------------------------------------------------------------

  const INSTALLER_TEMPLATE = "#!/usr/bin/env bash\n# Runs once on each employee's machine, during `opencode auth login`.\n# Install what cannot live in opencode.json: TUI plugins, companion tools\n# such as Hindsight, environment files.\nset -euo pipefail\n\n";

  async function installerPage() {
    let state = await api("GET", "/bootstrap");
    let draft = state.exists ? state.text : INSTALLER_TEMPLATE;
    // A brand-new installer starts from the template: only typing makes it dirty.
    let baseline = draft;
    let errors = [];
    const dirty = () => draft !== baseline;
    const savable = () => state.writable && (dirty() || !state.exists);
    const view = h("div");

    async function save() {
      try {
        state = await api("PUT", "/bootstrap", { text: draft, etag: state.etag });
        draft = baseline = state.text;
        errors = [];
        toast("Installer saved. It runs on the next opencode auth login of each employee.");
        render();
      } catch (error) {
        if (error.status === 422) {
          errors = error.data.errors || [error.message];
          render();
        } else fail(error);
      }
    }

    function render() {
      const saveButton = h("button", { type: "button", class: "btn primary", text: state.exists ? "Save" : "Create installer", disabled: !savable(), onclick: save });
      const area = codeArea(draft, (value) => { draft = value; saveButton.disabled = !savable(); }, { rows: 28, onSave: () => savable() && save(), label: state.file });
      put(view, 
        state.enabled ? null : banner("warn", "The installer is switched off", ["OCS_BOOTSTRAP_ENABLED=false: employees do not receive it."]),
        state.writable ? null : readOnlyBanner("The installer", state.file),
        errors.length ? banner("error", "bash cannot parse the script; nothing was saved", errors) : null,
        h("p", { class: "hint", text: "Checked with bash -n before saving. It runs with the employee's permissions, as part of the login, and only then — not on every refresh." }),
        area,
        h("div", { class: "editor-actions" },
          saveButton,
          state.exists ? h("button", { type: "button", class: "btn", text: "History", onclick: () => openHistory("bootstrap", { title: state.file, etag: state.etag, onRestored: async () => { state = await api("GET", "/bootstrap"); draft = baseline = state.text; render(); } }).catch(fail) }) : null));
    }

    put(main, 
      pageHead("Installer", "bootstrap/install.sh: the place for everything that is not an opencode.json setting."),
      view,
    );
    render();
    return { dirty };
  }

  const PAGES = { overview: overviewPage, config: configPage, users: usersPage, skills: skillsPage, installer: installerPage };

  // -- start ---------------------------------------------------------------------------

  async function start() {
    let overview;
    try {
      overview = await api("GET", "/overview");
    } catch (error) {
      if (error.status !== 401) {
        put(document.getElementById("app"), banner("error", "The admin panel cannot reach the server", [error.message]));
      }
      return;
    }
    renderShell(overview);
    current = null;
    await route();
  }

  start();
})();
