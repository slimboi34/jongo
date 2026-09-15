// The browser half of Jongo: virtual DOM, hooks, hydration, client-side
// navigation, server-function calls and dev tooling. Mirrors jongo/vdom.py.

const $SVG_NS = "http://www.w3.org/2000/svg";
const $TEXT = "#";
const $COMPONENTS = {};
let $BOOT = { build: "", boot: "", dev: false, csrf: "" };

class VNode {
  constructor(t, p, c, k) {
    this.t = t; // tag name, component function, or "#" for text
    this.p = p; // props (text for text nodes)
    this.c = c; // children
    this.k = k === undefined ? null : k;
    this.d = null; // DOM node
    this.i = null; // component instance
  }
}

// ---- props (mirrors vdom.py) ---------------------------------------------------

const $KEEP_PROPS = new Set(["style", "ref", "value", "checked", "selected", "inner_html"]);
const $UNITLESS = new Set(
  "opacity z-index flex flex-grow flex-shrink font-weight line-height order zoom scale grid-row grid-column aspect-ratio".split(" ")
);

function $propName(name) {
  if (name.startsWith("on_")) return "on:" + name.slice(3).replace(/_/g, "").toLowerCase();
  if ($KEEP_PROPS.has(name)) return name;
  if (name === "class_" || name === "cls" || name === "className") return "class";
  return name.replace(/_+$/, "").replace(/_/g, "-");
}

function $classNames(v) {
  if (v === null || v === undefined || v === false || v === true) return "";
  if (typeof v === "string") return v;
  if (Array.isArray(v)) return v.map($classNames).filter(Boolean).join(" ");
  if ($isPlain(v)) return Object.keys(v).filter((k) => $t(v[k])).join(" ");
  return String(v);
}

function $cssProp(name) {
  if (name.startsWith("--")) return name;
  return name.replace(/([a-z0-9])([A-Z])/g, "$1-$2").replace(/_/g, "-").toLowerCase();
}

function $cssValue(prop, v) {
  if (typeof v === "boolean") return String(v);
  if (typeof v === "number" && !$UNITLESS.has(prop) && !prop.startsWith("--")) return v === 0 ? "0" : `${v}px`;
  return String(v);
}

// ---- element and component factories ---------------------------------------------

function $flat(children, out = []) {
  for (const c of children) {
    if (c === null || c === undefined || c === true || c === false) continue;
    if (c instanceof VNode) out.push(c);
    else if (typeof c === "string") out.push(new VNode($TEXT, c));
    else if (typeof c === "number") out.push(new VNode($TEXT, String(c)));
    else if (Array.isArray(c) || c instanceof Set) $flat(c, out);
    else out.push(new VNode($TEXT, $str(c)));
  }
  return out;
}

function $splitProps(args) {
  return args.length && args[args.length - 1] instanceof KW ? args.pop().o : {};
}

const $tagCache = {};
function $h(tag) {
  if ($tagCache[tag]) return $tagCache[tag];
  const f = function (...args) {
    const props = $splitProps(args);
    const p = {};
    let key = null;
    for (const k in props) {
      if (k === "key") key = props[k];
      else p[$propName(k)] = props[k];
    }
    return new VNode(tag, p, $flat(args), key);
  };
  f.__jkw = true;
  return ($tagCache[tag] = f);
}

const $hfn = function (tag, ...args) {
  return $h(tag)(...args);
};
$hfn.__jkw = true;

const $raw = $fn((markup, tag = "span") => new VNode(tag, { inner_html: markup }, []), ["markup", "tag"]);
const $fragment = (...children) => $flat(children);

function $component(id, render) {
  const C = function (...args) {
    const props = { ...$splitProps(args) };
    let key = null;
    if ("key" in props) {
      key = props.key;
      delete props.key;
    }
    return new VNode(C, props, $flat(args), key);
  };
  const sig = render.__jsig || { p: [], k: [], d: null };
  C.__jkw = true;
  C.__id = id;
  C.__render = render;
  C.__name = id.split(".").pop();
  C.__children = sig.p.includes("children") || sig.k.includes("children");
  C.__kwargs = !!sig.d;
  $COMPONENTS[id] = C;
  return C;
}

function $callComponent(C, props, children) {
  const kw = { ...props };
  if (C.__children || (children.length && C.__kwargs)) kw.children = children;
  else if (children.length) throw $b.TypeError(`<${C.__name}> was given children but has no \`children\` parameter`);
  const out = $call(C.__render, [], kw);
  if (out instanceof VNode) return out;
  if (out === null || out === undefined || out === false) return new VNode($TEXT, "");
  if (Array.isArray(out)) {
    throw $b.TypeError(`<${C.__name}> returned a list; components must return a single element (wrap it in div())`);
  }
  return new VNode($TEXT, $str(out));
}

// ---- component instances and hooks -----------------------------------------------------

let $current = null;
let $pendingEffects = [];
const $dirty = new Set();
let $scheduled = false;

class Instance {
  constructor(vnode, depth) {
    this.v = vnode;
    this.depth = depth;
    this.hooks = [];
    this.hi = 0;
    this.out = null;
    this.dirty = false;
    this.dead = false;
  }
  render() {
    const previous = $current;
    $current = this;
    this.hi = 0;
    this.dirty = false;
    try {
      return $callComponent(this.v.t, this.v.p, this.v.c);
    } finally {
      $current = previous;
    }
  }
  invalidate() {
    if (this.dead) return;
    this.dirty = true;
    $dirty.add(this);
    if (!$scheduled) {
      $scheduled = true;
      queueMicrotask($flush);
    }
  }
}

function $flush() {
  $scheduled = false;
  const batch = [...$dirty].sort((a, b) => a.depth - b.depth);
  $dirty.clear();
  for (const inst of batch) {
    if (!inst.dirty || inst.dead) continue;
    try {
      const old = inst.out;
      inst.out = inst.render();
      const dom = $domOf(old);
      $patch(old, inst.out, dom.parentNode, dom.namespaceURI === $SVG_NS && dom.localName !== "svg", inst.depth);
    } catch (err) {
      $report(err);
    }
  }
  $runEffects();
}

function $runEffects() {
  const effects = $pendingEffects;
  $pendingEffects = [];
  for (const run of effects) {
    try {
      run();
    } catch (err) {
      $report(err);
    }
  }
}

function $hookOwner(name) {
  if (!$current) throw $b.RuntimeError(`${name}() can only be called while a @component is rendering`);
  return $current;
}

class State {
  constructor(value, inst) {
    this._v = value;
    this._i = inst;
  }
  get value() {
    return this._v;
  }
  set value(v) {
    this._v = v;
    this._i.invalidate();
  }
  set(v) {
    this.value = v;
  }
  update(f) {
    this.value = f(this._v);
  }
  toString() {
    return $str(this._v);
  }
}

class Ref {
  constructor(current) {
    this.current = current;
  }
}

const $state = $fn(function state(initial = null) {
  const inst = $hookOwner("state");
  const i = inst.hi++;
  if (i >= inst.hooks.length) {
    const value = typeof initial === "function" && !initial.__jkw ? initial() : initial;
    inst.hooks.push(new State(value, inst));
  }
  return inst.hooks[i];
}, ["initial"]);

const $effect = $fn(function effect(fn, deps = null) {
  const inst = $hookOwner("effect");
  const i = inst.hi++;
  const hook = inst.hooks[i] || (inst.hooks[i] = { deps: undefined, cleanup: null });
  const next = deps === null || deps === undefined ? null : [...$iter(deps)];
  const changed =
    next === null ||
    hook.deps === undefined ||
    hook.deps.length !== next.length ||
    next.some((d, j) => !Object.is(d, hook.deps[j]));
  if (!changed) return null;
  hook.deps = next;
  $pendingEffects.push(() => {
    if (inst.dead) return;
    if (typeof hook.cleanup === "function") hook.cleanup();
    hook.cleanup = null;
    const result = fn();
    if (typeof result === "function") hook.cleanup = result;
    else if (result && typeof result.then === "function") result.catch($report);
  });
  return null;
}, ["fn", "deps"]);

const $ref = $fn(function ref(initial = null) {
  const inst = $hookOwner("ref");
  const i = inst.hi++;
  if (i >= inst.hooks.length) inst.hooks.push(new Ref(initial));
  return inst.hooks[i];
}, ["initial"]);

// ---- DOM reconciliation -----------------------------------------------------------------------

function $domOf(v) {
  while (typeof v.t === "function") v = v.i.out;
  return v.d;
}

function $mount(v, parent, before, svg, depth) {
  if (v.t === $TEXT) {
    const node = document.createTextNode(v.p);
    v.d = node;
    parent.insertBefore(node, before);
    return node;
  }
  if (typeof v.t === "function") {
    const inst = (v.i = new Instance(v, depth + 1));
    inst.out = inst.render();
    return $mount(inst.out, parent, before, svg, depth + 1);
  }
  const isSvg = svg || v.t === "svg";
  const node = isSvg ? document.createElementNS($SVG_NS, v.t) : document.createElement(v.t);
  v.d = node;
  if (v.p.inner_html !== undefined && v.p.inner_html !== null) node.innerHTML = v.p.inner_html;
  else for (const child of v.c) $mount(child, node, null, isSvg && v.t !== "foreignObject", depth);
  $setProps(node, {}, v.p, isSvg);
  parent.insertBefore(node, before);
  return node;
}

function $destroy(v) {
  if (typeof v.t === "function") {
    const inst = v.i;
    inst.dead = true;
    for (const hook of inst.hooks) {
      if (hook && typeof hook.cleanup === "function") {
        try {
          hook.cleanup();
        } catch (err) {
          $report(err);
        }
      }
    }
    $destroy(inst.out);
    return;
  }
  if (v.t === $TEXT) return;
  if (v.p.ref) v.p.ref.current = null;
  for (const child of v.c) $destroy(child);
}

function $unmount(v) {
  const node = $domOf(v);
  $destroy(v);
  if (node && node.parentNode) node.parentNode.removeChild(node);
}

function $patch(old, next, parent, svg, depth) {
  if (old === next) return;
  if (old.t !== next.t || old.k !== next.k) {
    $mount(next, parent, $domOf(old), svg, depth);
    $unmount(old);
    return;
  }
  if (next.t === $TEXT) {
    next.d = old.d;
    if (old.p !== next.p) old.d.nodeValue = next.p;
    return;
  }
  if (typeof next.t === "function") {
    const inst = (next.i = old.i);
    inst.v = next;
    const previous = inst.out;
    inst.out = inst.render();
    $patch(previous, inst.out, parent, svg, inst.depth);
    return;
  }
  const node = (next.d = old.d);
  const isSvg = svg || next.t === "svg";
  const html = next.p.inner_html;
  if (html !== undefined && html !== null) {
    if (old.p.inner_html === undefined || old.p.inner_html === null) old.c.forEach($destroy);
    if (html !== old.p.inner_html) node.innerHTML = html;
  } else if (old.p.inner_html !== undefined && old.p.inner_html !== null) {
    node.innerHTML = "";
    for (const child of next.c) $mount(child, node, null, isSvg, depth);
  } else {
    $patchChildren(node, old.c, next.c, isSvg && next.t !== "foreignObject", depth);
  }
  $setProps(node, old.p, next.p, isSvg);
}

function $patchChildren(parent, oldChildren, newChildren, svg, depth) {
  const keyOf = (v, i) => (v.k !== null ? "k:" + v.k : "i:" + i);
  const byKey = new Map();
  oldChildren.forEach((v, i) => byKey.set(keyOf(v, i), v));
  const used = new Set();
  newChildren.forEach((v, i) => {
    const match = byKey.get(keyOf(v, i));
    if (match && !used.has(match) && match.t === v.t) {
      used.add(match);
      $patch(match, v, parent, svg, depth);
    } else {
      $mount(v, parent, null, svg, depth);
    }
  });
  for (const v of oldChildren) if (!used.has(v)) $unmount(v);
  let ref = null;
  for (let i = newChildren.length - 1; i >= 0; i--) {
    const node = $domOf(newChildren[i]);
    if (node.nextSibling !== ref || node.parentNode !== parent) parent.insertBefore(node, ref);
    ref = node;
  }
}

function $setProps(node, oldProps, newProps, svg) {
  for (const k in oldProps) if (!(k in newProps)) $setProp(node, k, null, oldProps[k], svg);
  for (const k in newProps) {
    const v = newProps[k];
    if (v !== oldProps[k] || k === "value" || k === "checked") $setProp(node, k, v, oldProps[k], svg);
  }
}

function $setProp(node, k, v, old, svg) {
  if (k === "inner_html") return;
  if (k === "ref") {
    if (v) v.current = node;
    return;
  }
  if (k.startsWith("on:")) {
    const type = k.slice(3);
    const handlers = node.__jl || (node.__jl = {});
    if (!(type in handlers)) node.addEventListener(type, $dispatch);
    handlers[type] = v;
    return;
  }
  if (k === "style") {
    if (v === null || v === undefined || typeof v === "string") {
      node.style.cssText = v || "";
      return;
    }
    if (old === null || old === undefined || typeof old === "string") node.style.cssText = "";
    else for (const s in old) if (!(s in v)) node.style.removeProperty($cssProp(s));
    for (const s in v) {
      const prop = $cssProp(s);
      if (v[s] === null || v[s] === undefined || v[s] === false) node.style.removeProperty(prop);
      else node.style.setProperty(prop, $cssValue(prop, v[s]));
    }
    return;
  }
  if (!svg && k === "value") {
    const text = v === null || v === undefined ? "" : String(v);
    if (node.value !== text) node.value = text;
    return;
  }
  if (!svg && (k === "checked" || k === "selected")) {
    node[k] = $t(v);
    return;
  }
  if (k === "class") v = $classNames(v) || null;
  if (v === null || v === undefined || v === false) node.removeAttribute(k);
  else node.setAttribute(k, v === true ? "" : v);
}

function $dispatch(event) {
  const handler = this.__jl && this.__jl[event.type];
  if (!handler) return;
  if (event.type === "submit") event.preventDefault();
  try {
    const result = handler(event);
    if (result && typeof result.then === "function") result.catch($report);
  } catch (err) {
    $report(err);
  }
}

// ---- hydration: adopt server-rendered DOM instead of rebuilding it ------------------------------

function $hydrate(v, parent, cur, svg, depth) {
  while (cur && cur.nodeType === 8) {
    const next = cur.nextSibling;
    parent.removeChild(cur);
    cur = next;
  }
  if (typeof v.t === "function") {
    const inst = (v.i = new Instance(v, depth + 1));
    inst.out = inst.render();
    return $hydrate(inst.out, parent, cur, svg, depth + 1);
  }
  if (v.t === $TEXT) {
    if (cur && cur.nodeType === 3) {
      if (cur.nodeValue !== v.p) cur.nodeValue = v.p;
      v.d = cur;
      return cur.nextSibling;
    }
    $mount(v, parent, cur, svg, depth);
    return cur;
  }
  const isSvg = svg || v.t === "svg";
  if (cur && cur.nodeType === 1 && cur.localName === v.t) {
    v.d = cur;
    if (v.p.inner_html === undefined || v.p.inner_html === null) {
      let child = cur.firstChild;
      for (const c of v.c) child = $hydrate(c, cur, child, isSvg && v.t !== "foreignObject", depth);
      while (child) {
        const next = child.nextSibling;
        cur.removeChild(child);
        child = next;
      }
    }
    $setProps(cur, {}, v.p, isSvg);
    return cur.nextSibling;
  }
  if ($BOOT.dev) console.warn("[jongo] hydration mismatch: expected <%s>, found", v.t, cur);
  $mount(v, parent, cur, svg, depth);
  if (!cur) return null;
  const next = cur.nextSibling;
  parent.removeChild(cur);
  return next;
}

// ---- JSON trees from the server ------------------------------------------------------------------

function $build(d) {
  if (typeof d === "string") return new VNode($TEXT, d);
  const children = (d.c || []).map($build);
  const props = $revive(d.p || {});
  if (d.C !== undefined) {
    const C = $COMPONENTS[d.C];
    if (!C) throw $b.RuntimeError(`unknown component ${d.C}; is the page's JavaScript out of date?`);
    return new VNode(C, props, children, d.k);
  }
  return new VNode(d.t, props, children, d.k);
}

function $revive(x) {
  if (Array.isArray(x)) return x.map($revive);
  if (x !== null && typeof x === "object") {
    const keys = Object.keys(x);
    if (keys.length === 1 && keys[0] === "$v") return $build(x.$v);
    for (const k of keys) x[k] = $revive(x[k]);
  }
  return x;
}

const $root = { el: null, vnode: null };

function $renderRoot(tree) {
  const next = $build(tree);
  if ($root.vnode) {
    $patch($root.vnode, next, $root.el, false, 0);
  } else {
    let rest = $hydrate(next, $root.el, $root.el.firstChild, false, 0);
    while (rest) {
      const after = rest.nextSibling;
      $root.el.removeChild(rest);
      rest = after;
    }
  }
  $root.vnode = next;
  $runEffects();
}

// ---- client-side navigation -------------------------------------------------------------------------

async function $load(href, mode) {
  let res;
  try {
    res = await fetch(href, { headers: { "X-Jongo-Nav": "1" }, credentials: "same-origin" });
  } catch (err) {
    window.location.href = href;
    return;
  }
  const finalUrl = res.url || href;
  if (!res.headers.get("X-Jongo-Page")) {
    if (mode === "pop" || mode === "none") window.location.reload();
    else window.location.href = finalUrl;
    return;
  }
  const data = await res.json();
  if (data.build !== $BOOT.build) {
    window.location.href = finalUrl;
    return;
  }
  if (mode === "push") history.pushState({ jongo: true }, "", finalUrl);
  else if (mode === "replace" || finalUrl !== href) history.replaceState({ jongo: true }, "", finalUrl);
  document.title = data.title;
  $renderRoot(data.tree);
  if (mode === "push" || mode === "replace") {
    const hash = new URL(finalUrl).hash;
    const target = hash && document.getElementById(decodeURIComponent(hash.slice(1)));
    if (target) target.scrollIntoView();
    else window.scrollTo(0, 0);
  }
}

const $navigate = $fn(function navigate(url, replace = false) {
  const target = new URL(url, window.location.href);
  if (target.origin !== window.location.origin) {
    window.location.href = target.href;
    return Promise.resolve(null);
  }
  return $load(target.href, replace ? "replace" : "push");
}, ["url", "replace"]);

const $refresh = function refresh() {
  return $load(window.location.href, "none");
};

function $onLinkClick(event) {
  if (event.defaultPrevented || event.button !== 0) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const link = event.target.closest && event.target.closest("a[href]");
  if (!link) return;
  if ((link.target && link.target !== "_self") || link.hasAttribute("download") || link.hasAttribute("data-reload")) return;
  const url = new URL(link.href, window.location.href);
  if (url.origin !== window.location.origin || url.pathname.startsWith("/_jongo/")) return;
  if (url.pathname === window.location.pathname && url.search === window.location.search && url.hash) return;
  event.preventDefault();
  $navigate(url.href);
}

// ---- server functions --------------------------------------------------------------------------------

const ServerError = $defExc("ServerError", $b.Exception);

function $csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)jongo_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : $BOOT.csrf;
}

function $rpc(id, refreshAfter = false) {
  const invoke = async (args, kwargs) => {
    let res;
    let data;
    try {
      res = await fetch(`/_jongo/rpc/${id}`, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": $csrfToken() },
        body: JSON.stringify({ args, kwargs: kwargs || {} }),
      });
      data = await res.json();
    } catch (err) {
      throw ServerError(`couldn't reach the server for ${id}: ${err.message}`);
    }
    if (data.error) {
      const err = ServerError(data.error.message);
      err.type = data.error.type;
      err.status = res.status;
      err.errors = data.error.errors || null;
      if (data.error.traceback) err.traceback = data.error.traceback;
      throw err;
    }
    if (data.redirect) {
      await $navigate(data.redirect);
      return null;
    }
    if (refreshAfter) await $refresh();
    return $revive(data.ok === undefined ? null : data.ok);
  };
  const stub = (...args) => invoke(args, null);
  stub.__jrpc = invoke;
  stub.__id = id;
  return stub;
}

const $form_values = function form_values(eventOrForm) {
  let form = eventOrForm && eventOrForm.target ? eventOrForm.target : eventOrForm;
  if (form && form.tagName !== "FORM") form = form.form || form.closest("form");
  const values = {};
  for (const [name, value] of new FormData(form)) {
    values[name] = name in values ? [].concat(values[name], value) : value;
  }
  return values;
};

// ---- dev tooling and startup --------------------------------------------------------------------------

function $report(err) {
  console.error(err);
  if (!$BOOT.dev || typeof document === "undefined") return;
  let box = document.getElementById("jongo-error");
  if (!box) {
    box = document.createElement("pre");
    box.id = "jongo-error";
    box.title = "Click to dismiss";
    box.style.cssText =
      "position:fixed;left:16px;right:16px;bottom:16px;z-index:2147483647;margin:0;background:#17131c;" +
      "color:#ffb4a9;font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;padding:16px 18px;" +
      "border-radius:12px;border:1px solid #ff6b57;box-shadow:0 18px 50px rgba(0,0,0,.45);" +
      "white-space:pre-wrap;max-height:45vh;overflow:auto;cursor:pointer";
    box.onclick = () => box.remove();
    document.body.appendChild(box);
  }
  const name = (err && err.name) || "Error";
  const message = err && err.message !== undefined ? err.message : String(err);
  box.textContent = `${name}: ${message}${err && err.traceback ? "\n\n" + err.traceback : ""}`;
}

function $liveReload() {
  if (!$BOOT.dev || !window.EventSource) return;
  const source = new EventSource("/_jongo/live");
  source.onmessage = (event) => {
    if (event.data && event.data !== $BOOT.boot) window.location.reload();
  };
}

function $start() {
  const dataEl = document.getElementById("jongo-data");
  $root.el = document.getElementById("jongo-root");
  if (!dataEl || !$root.el) return;
  $BOOT = JSON.parse(dataEl.textContent);
  window.addEventListener("error", (e) => $report(e.error || e.message));
  window.addEventListener("unhandledrejection", (e) => $report(e.reason));
  document.addEventListener("click", $onLinkClick);
  window.addEventListener("popstate", () => $load(window.location.href, "pop"));
  history.replaceState({ jongo: true }, "");
  try {
    $renderRoot($BOOT.tree);
  } catch (err) {
    $report(err);
  }
  $liveReload();
}
