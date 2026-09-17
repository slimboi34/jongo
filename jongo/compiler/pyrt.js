// Python semantics for compiled Jongo code: truthiness, equality, operators,
// indexing, method calls with keyword arguments, builtins and exceptions.

const $hasOwn = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
const $isPlain = (o) => {
  if (o === null || typeof o !== "object") return false;
  const proto = Object.getPrototypeOf(o);
  return proto === Object.prototype || proto === null;
};
const $camel = (name) => name.replace(/_([a-z0-9])/g, (_, c) => c.toUpperCase());

function $typename(x) {
  if (x === null || x === undefined) return "NoneType";
  if (typeof x === "string") return "str";
  if (typeof x === "number") return Number.isInteger(x) ? "int" : "float";
  if (typeof x === "boolean") return "bool";
  if (Array.isArray(x)) return "list";
  if (x instanceof Set) return "set";
  if ($isPlain(x)) return "dict";
  if (typeof x === "function") return "function";
  return (x.constructor && x.constructor.name) || "object";
}

// ---- exceptions -------------------------------------------------------------

class PyException extends Error {
  constructor(...args) {
    super(args.length ? $str(args[0]) : "");
    this.args = args;
  }
  get name() {
    return this.constructor.__pyname || "Exception";
  }
}

function $defExc(name, Base) {
  const Cls = class extends Base {};
  Cls.__pyname = name;
  // Python raises with `ValueError("msg")`, no `new`.
  return new Proxy(Cls, { apply: (target, _this, args) => new target(...args) });
}

const $b = {};
$b.BaseException = $defExc("BaseException", PyException);
$b.Exception = $defExc("Exception", $b.BaseException);
for (const name of [
  "ValueError", "TypeError", "KeyError", "IndexError", "AttributeError", "RuntimeError",
  "AssertionError", "ZeroDivisionError", "NotImplementedError",
]) {
  $b[name] = $defExc(name, $b.Exception);
}
const $NATIVE_EXC = new Map([
  [$b.TypeError, TypeError],
  [$b.RuntimeError, Error],
  [$b.IndexError, RangeError],
]);

function $isexc(e, T) {
  if (T === $b.Exception || T === $b.BaseException) return true;
  if (typeof T !== "function") return false;
  if (e instanceof T) return true;
  const native = $NATIVE_EXC.get(T);
  return native ? e instanceof native && !(e instanceof PyException) : false;
}

function $raise(x) {
  if (typeof x === "function" && x.prototype instanceof Error) return x();
  return x;
}

// ---- truthiness, equality, operators ------------------------------------------

function $t(x) {
  if (x === null || x === undefined || x === false || x === 0 || x === "") return false;
  if (x === true) return true;
  if (typeof x === "number") return !Number.isNaN(x);
  if (Array.isArray(x)) return x.length > 0;
  if (x instanceof Set || x instanceof Map) return x.size > 0;
  if ($isPlain(x)) {
    for (const _ in x) return true;
    return false;
  }
  return true;
}

const $and = (a, f) => ($t(a) ? f() : a);
const $or = (a, f) => ($t(a) ? a : f());

function $eq(a, b) {
  if (a === b) return true;
  if (a == null || b == null) return a == null && b == null;
  if (Array.isArray(a) && Array.isArray(b)) {
    return a.length === b.length && a.every((x, i) => $eq(x, b[i]));
  }
  if ($isPlain(a) && $isPlain(b)) {
    const ka = Object.keys(a);
    return ka.length === Object.keys(b).length && ka.every((k) => $hasOwn(b, k) && $eq(a[k], b[k]));
  }
  if (a instanceof Set && b instanceof Set) return a.size === b.size && [...a].every((x) => b.has(x));
  // Python treats booleans as ints: True == 1, False == 0, and 1 == 1.0.
  if (typeof a === "boolean" || typeof b === "boolean") {
    const na = typeof a === "boolean" ? +a : a;
    const nb = typeof b === "boolean" ? +b : b;
    if (typeof na === "number" && typeof nb === "number") return na === nb;
  }
  return false;
}

function $cmp(a, b) {
  if (Array.isArray(a) && Array.isArray(b)) {
    for (let i = 0; i < Math.min(a.length, b.length); i++) {
      const c = $cmp(a[i], b[i]);
      if (c) return c;
    }
    return a.length - b.length;
  }
  if (a == null || b == null) {
    if (a == null && b == null) return 0;
    throw $b.TypeError(`'<' not supported between '${$typename(a)}' and '${$typename(b)}'`);
  }
  return a < b ? -1 : a > b ? 1 : 0;
}

function $in(x, c) {
  if (typeof c === "string") return c.includes(x);
  if (Array.isArray(c)) return c.some((y) => $eq(x, y));
  if (c instanceof Set || c instanceof Map) return c.has(x);
  if ($isPlain(c)) return $hasOwn(c, x);
  if (c !== null && typeof c === "object") return x in c;
  throw $b.TypeError(`argument of type '${$typename(c)}' is not iterable`);
}

function $add(a, b) {
  if (Array.isArray(a) && Array.isArray(b)) return a.concat(b);
  return a + b;
}

function $iadd(a, b) {
  if (Array.isArray(a)) {
    a.push(...$iter(b));
    return a;
  }
  return $add(a, b);
}

function $mul(a, b) {
  if (typeof a === "number" && typeof b !== "number") [a, b] = [b, a];
  if (typeof a === "string") return b > 0 ? a.repeat(b) : "";
  if (Array.isArray(a)) {
    const out = [];
    for (let i = 0; i < b; i++) out.push(...a);
    return out;
  }
  return a * b;
}

function $floordiv(a, b) {
  if (b === 0) throw $b.ZeroDivisionError("integer division or modulo by zero");
  return Math.floor(a / b);
}

function $mod(a, b) {
  if (typeof a === "string") return $percentFormat(a, b);
  if (b === 0) throw $b.ZeroDivisionError("integer division or modulo by zero");
  return ((a % b) + b) % b;
}

function $bor(a, b) {
  if ($isPlain(a) && $isPlain(b)) return { ...a, ...b };
  if (a instanceof Set && b instanceof Set) return new Set([...a, ...b]);
  return a | b;
}

function $sub(a, b) {
  if (a instanceof Set && b instanceof Set) return new Set([...a].filter((x) => !b.has(x)));
  return a - b;
}

function $band(a, b) {
  if (a instanceof Set && b instanceof Set) return new Set([...a].filter((x) => b.has(x)));
  return a & b;
}

function $bxor(a, b) {
  if (a instanceof Set && b instanceof Set) {
    return new Set([...a].filter((x) => !b.has(x)).concat([...b].filter((x) => !a.has(x))));
  }
  return a ^ b;
}

// ---- iteration, indexing, attributes ------------------------------------------

function $iter(x) {
  if (x === null || x === undefined) throw $b.TypeError("'NoneType' object is not iterable");
  if (Array.isArray(x) || typeof x === "string") return x;
  if ($isPlain(x)) return Object.keys(x);
  if (typeof x[Symbol.iterator] === "function") return x;
  if (typeof x.length === "number") return Array.from(x);
  throw $b.TypeError(`'${$typename(x)}' object is not iterable`);
}

function $unpack(v, n, star) {
  const a = Array.isArray(v) ? v : [...$iter(v)];
  if (star < 0) {
    if (a.length !== n) throw $b.ValueError(`expected ${n} values to unpack, got ${a.length}`);
    return a;
  }
  const after = n - star - 1;
  if (a.length < n - 1) throw $b.ValueError(`expected at least ${n - 1} values to unpack, got ${a.length}`);
  return [...a.slice(0, star), a.slice(star, a.length - after), ...a.slice(a.length - after)];
}

function $gi(o, k) {
  if (o === null || o === undefined) throw $b.TypeError("'NoneType' object is not subscriptable");
  if (Array.isArray(o) || typeof o === "string") {
    if (typeof k === "number") {
      const i = k < 0 ? o.length + k : k;
      if (i < 0 || i >= o.length) throw $b.IndexError(`${$typename(o)} index out of range`);
      return o[i];
    }
  } else if ($isPlain(o)) {
    if (!$hasOwn(o, k)) throw $b.KeyError($repr(k));
    return o[k];
  } else if (o instanceof Map) {
    if (!o.has(k)) throw $b.KeyError($repr(k));
    return o.get(k);
  }
  return o[k];
}

function $si(o, k, v) {
  if (Array.isArray(o) && typeof k === "number") {
    const i = k < 0 ? o.length + k : k;
    if (i < 0 || i >= o.length) throw $b.IndexError("list assignment index out of range");
    o[i] = v;
  } else if (o instanceof Map) {
    o.set(k, v);
  } else {
    o[k] = v;
  }
  return v;
}

function $del(o, k) {
  if (Array.isArray(o)) o.splice(k < 0 ? o.length + k : k, 1);
  else if (o instanceof Map) o.delete(k);
  else {
    if ($isPlain(o) && !$hasOwn(o, k)) throw $b.KeyError($repr(k));
    delete o[k];
  }
}

function $slice(o, start, stop, step) {
  const len = o.length;
  step = step === null || step === undefined ? 1 : step;
  if (step === 0) throw $b.ValueError("slice step cannot be zero");
  const norm = (i, dflt, lo, hi) => {
    if (i === null || i === undefined) return dflt;
    if (i < 0) i += len;
    return Math.max(lo, Math.min(hi, i));
  };
  if (step === 1) return o.slice(norm(start, 0, 0, len), norm(stop, len, 0, len));
  const out = [];
  if (step > 0) {
    for (let i = norm(start, 0, 0, len); i < norm(stop, len, 0, len); i += step) out.push(o[i]);
  } else {
    for (let i = norm(start, len - 1, -1, len - 1); i > norm(stop, -1, -1, len - 1); i += step) out.push(o[i]);
  }
  return typeof o === "string" ? out.join("") : out;
}

function $ga(o, name) {
  if (o === null || o === undefined) throw $b.AttributeError(`'NoneType' object has no attribute '${name}'`);
  if (name in Object(o)) return o[name];
  const camel = $camel(name);
  return camel in Object(o) ? o[camel] : o[name];
}

function $sa(o, name, v) {
  if (!(name in o)) {
    const camel = $camel(name);
    if (camel in o) {
      o[camel] = v;
      return;
    }
  }
  o[name] = v;
}

// ---- functions and calls -------------------------------------------------------

class KW {
  constructor(o) {
    this.o = o || {};
  }
}

// Attach a Python signature to a compiled function so keyword calls can bind.
// JS parameter layout: positional..., [*args], keyword-only..., [**kwargs]
function $fn(f, p, s = null, k = [], d = null) {
  const sig = { p, s, k, d };
  if (s) {
    const wrapper = function (...args) {
      return f.apply(this, $bind(sig, args, null));
    };
    wrapper.__jsig = sig;
    wrapper.__inner = f;
    return wrapper;
  }
  f.__jsig = sig;
  return f;
}

function $bind(sig, args, kw) {
  const np = sig.p.length;
  const out = new Array(np).fill(undefined);
  for (let i = 0; i < Math.min(np, args.length); i++) out[i] = args[i];
  if (args.length > np && !sig.s) {
    throw $b.TypeError(`takes ${np} positional argument${np === 1 ? "" : "s"} but ${args.length} were given`);
  }
  const kwonly = sig.k.map(() => undefined);
  const rest = {};
  if (kw) {
    for (const name of Object.keys(kw)) {
      let i = sig.p.indexOf(name);
      if (i >= 0) {
        if (i < args.length) throw $b.TypeError(`got multiple values for argument '${name}'`);
        out[i] = kw[name];
      } else if ((i = sig.k.indexOf(name)) >= 0) {
        kwonly[i] = kw[name];
      } else if (sig.d) {
        rest[name] = kw[name];
      } else {
        throw $b.TypeError(`got an unexpected keyword argument '${name}'`);
      }
    }
  }
  if (sig.s) out.push(args.slice(np));
  out.push(...kwonly);
  if (sig.d) out.push(rest);
  return out;
}

function $call(f, args, kw) {
  if (typeof f !== "function") throw $b.TypeError(`'${$typename(f)}' object is not callable`);
  if (f.__jsig) return (f.__inner || f)(...$bind(f.__jsig, args, kw));
  if (f.__jkw) return f(...args, new KW(kw));
  if (f.__jrpc) return f.__jrpc(args, kw);
  return kw && Object.keys(kw).length ? f(...args, kw) : f(...args);
}

// ---- str / repr / formatting -----------------------------------------------------

function $str(x) {
  if (typeof x === "string") return x;
  if (x instanceof Error) return x.message;
  return $repr(x);
}

function $repr(x) {
  if (x === null || x === undefined) return "None";
  if (x === true) return "True";
  if (x === false) return "False";
  if (typeof x === "number") {
    if (Number.isFinite(x)) return String(x);
    return Number.isNaN(x) ? "nan" : x > 0 ? "inf" : "-inf";
  }
  if (typeof x === "string") return "'" + x.replace(/\\/g, "\\\\").replace(/'/g, "\\'") + "'";
  if (Array.isArray(x)) return "[" + x.map($repr).join(", ") + "]";
  if (x instanceof Set) return x.size ? "{" + [...x].map($repr).join(", ") + "}" : "set()";
  if ($isPlain(x)) return "{" + Object.entries(x).map(([k, v]) => `${$repr(k)}: ${$repr(v)}`).join(", ") + "}";
  if (x instanceof Error) return `${x.name}(${$repr(x.message)})`;
  if (typeof x === "function") return `<function ${x.name || "lambda"}>`;
  return String(x);
}

function $fmt(x, spec) {
  if (!spec) return $str(x);
  const m = /^(?:(.)?([<>^=]))?([+\- ])?(#)?(0)?(\d+)?([,_])?(?:\.(\d+))?([bcdeEfFgGnosxX%])?$/.exec(spec);
  if (!m) throw $b.ValueError(`Invalid format specifier '${spec}'`);
  let [, fill = " ", align, sign, alt, zero, width, group, prec, type] = m;
  const p = prec === undefined ? undefined : +prec;
  let body;
  let signStr = "";
  if (typeof x === "number") {
    const n = Math.abs(x);
    switch (type) {
      case "f": case "F": body = n.toFixed(p ?? 6); break;
      case "e": case "E": body = n.toExponential(p ?? 6).replace(/e([+-])(\d)$/, "e$10$2"); break;
      case "%": body = (n * 100).toFixed(p ?? 6); break;
      case "d": case "n": body = String(Math.trunc(n)); break;
      case "x": body = Math.trunc(n).toString(16); break;
      case "X": body = Math.trunc(n).toString(16).toUpperCase(); break;
      case "b": body = Math.trunc(n).toString(2); break;
      case "o": body = Math.trunc(n).toString(8); break;
      case "g": case "G": body = String(p === undefined ? n : +n.toPrecision(p || 1)); break;
      default: body = p === undefined ? String(n) : String(+n.toPrecision(p || 1));
    }
    if (type === "E") body = body.toUpperCase();
    if (alt) {
      const prefix = { x: "0x", X: "0X", o: "0o", b: "0b" }[type];
      if (prefix) body = prefix + body;
    }
    if (group) body = body.replace(/^(\d+)/, (d) => d.replace(/\B(?=(\d{3})+(?!\d))/g, group));
    if (type === "%") body += "%";
    signStr = x < 0 ? "-" : sign === "+" ? "+" : sign === " " ? " " : "";
    if (zero && !align) {
      fill = "0";
      align = "=";
    }
    align = align || ">";
  } else {
    body = $str(x);
    if (p !== undefined) body = body.slice(0, p);
    align = align || "<";
  }
  const w = width === undefined ? 0 : +width;
  const pad = Math.max(0, w - body.length - signStr.length);
  if (align === "=") return signStr + fill.repeat(pad) + body;
  const text = signStr + body;
  if (align === "<") return text + fill.repeat(pad);
  if (align === "^") return fill.repeat(Math.floor(pad / 2)) + text + fill.repeat(Math.ceil(pad / 2));
  return fill.repeat(pad) + text;
}

function $percentFormat(template, values) {
  const positional = Array.isArray(values) ? values : [values];
  let index = 0;
  return template.replace(/%(?:\((\w+)\))?([-+ 0#]*)(\d+)?(?:\.(\d+))?([sdifrx%])/g, (all, key, flags, width, prec, type) => {
    if (type === "%") return "%";
    const v = key !== undefined ? values[key] : positional[index++];
    const zero = flags.includes("0") ? "0" : "";
    const align = flags.includes("-") ? "<" : "";
    const spec = {
      s: `${align}${width || ""}`,
      r: `${align}${width || ""}`,
      d: `${align}${zero}${width || ""}d`,
      i: `${align}${zero}${width || ""}d`,
      f: `${align}${zero}${width || ""}.${prec ?? 6}f`,
      x: `${align}${zero}${width || ""}x`,
    }[type];
    return $fmt(type === "r" ? $repr(v) : v, spec);
  });
}

function $formatBraces(s, args, kw) {
  let auto = 0;
  return s.replace(/\{\{|\}\}|\{([^{}:!]*)(?:!([rsa]))?(?::([^{}]*))?\}/g, (all, field, conv, spec) => {
    if (all === "{{") return "{";
    if (all === "}}") return "}";
    let value;
    if (field === "") value = args[auto++];
    else if (/^\d+$/.test(field)) value = args[+field];
    else {
      // Resolve a field path with attribute (.x) and subscript ([k]) access, e.g. {x[0].y}
      // or {0[1]} (a positional arg, then a subscript).
      const tokens = field.match(/[^.[\]]+|\[[^\]]*\]/g) || [];
      let cur;
      let first = true;
      for (const tok of tokens) {
        if (first) {
          first = false;
          cur = /^\d+$/.test(tok) ? args[+tok] : kw[tok];
          continue;
        }
        if (tok[0] === "[") {
          let key = tok.slice(1, -1);
          if (/^-?\d+$/.test(key)) {
            key = +key;
            if (Array.isArray(cur) && key < 0) key += cur.length;
          }
          cur = cur[key];
        } else {
          cur = $ga(cur, tok);
        }
      }
      value = cur;
    }
    if (conv === "r") value = $repr(value);
    return $fmt(value, spec);
  });
}

// ---- builtins -------------------------------------------------------------------------

function $sort(items, key, reverse) {
  const decorated = items.map((x, i) => [key ? key(x) : x, i, x]);
  decorated.sort((a, b) => $cmp(a[0], b[0]) * (reverse ? -1 : 1) || a[1] - b[1]);
  return decorated.map((d) => d[2]);
}

function $minmax(which) {
  return $fn(
    function (args, key, dflt) {
      const items = args.length === 1 ? [...$iter(args[0])] : args;
      if (!items.length) {
        if (dflt !== undefined) return dflt;
        throw $b.ValueError(`${which}() arg is an empty sequence`);
      }
      let best = items[0];
      let bestKey = key ? key(best) : best;
      for (const item of items.slice(1)) {
        const k = key ? key(item) : item;
        if (which === "min" ? $cmp(k, bestKey) < 0 : $cmp(k, bestKey) > 0) {
          best = item;
          bestKey = k;
        }
      }
      return best;
    },
    [], "args", ["key", "default"]
  );
}

function $toInt(x, base) {
  if (typeof x === "string") {
    const text = x.trim().replace(/_/g, "");
    const n = parseInt(text, base || 10);
    if (Number.isNaN(n) || !/^[-+]?[0-9a-zA-Z]+$/.test(text)) {
      throw $b.ValueError(`invalid literal for int() with base ${base || 10}: ${$repr(x)}`);
    }
    return n;
  }
  if (typeof x === "boolean") return x ? 1 : 0;
  if (x === undefined) return 0;
  return Math.trunc(x);
}

function $round(x, n) {
  if (n === undefined || n === null) {
    const r = Math.round(x);
    return Math.abs(x % 1) === 0.5 && r % 2 !== 0 ? r - 1 : r; // banker's rounding
  }
  if (!Number.isFinite(x)) return x;
  if (n < 0) {
    const m = 10 ** -n;
    return $round(x / m, 0) * m;
  }
  // Round the double's exact decimal expansion, half-to-even at n places, so results
  // match CPython even where x*10**n would cross .5 in binary (e.g. round(2.675, 2) == 2.67).
  const neg = x < 0;
  const digits = Math.abs(x).toFixed(Math.min(n + 18, 100));
  const dot = digits.indexOf(".");
  const cut = dot + 1 + n;
  let keep = digits.slice(0, cut).replace(".", "");
  const rest = digits.slice(cut);
  const first = rest.charCodeAt(0) - 48;
  let roundUp = false;
  if (first > 5) roundUp = true;
  else if (first === 5) {
    if (/[1-9]/.test(rest.slice(1))) roundUp = true;
    else roundUp = (keep.charCodeAt(keep.length - 1) - 48) % 2 === 1; // exact tie → to even
  }
  const intval = BigInt(keep || "0") + (roundUp ? 1n : 0n);
  const result = Number(intval) / 10 ** n;
  return neg ? -result : result;
}

Object.assign($b, {
  len(x) {
    if (typeof x === "string" || Array.isArray(x)) return x.length;
    if (x instanceof Set || x instanceof Map) return x.size;
    if ($isPlain(x)) return Object.keys(x).length;
    if (x !== null && x !== undefined && typeof x.length === "number") return x.length;
    throw $b.TypeError(`object of type '${$typename(x)}' has no len()`);
  },
  str: (x = "") => $str(x),
  int: $toInt,
  float(x = 0) {
    const n = typeof x === "string" ? Number(x.trim()) : Number(x);
    if (Number.isNaN(n) && !/^\s*nan\s*$/i.test(String(x))) {
      throw $b.ValueError(`could not convert string to float: ${$repr(x)}`);
    }
    return n;
  },
  bool: (x) => $t(x),
  list: (x) => (x === undefined ? [] : [...$iter(x)]),
  tuple: (x) => (x === undefined ? [] : [...$iter(x)]),
  set: (x) => new Set(x === undefined ? [] : $iter(x)),
  dict: $fn(
    function (x, kw) {
      const o = {};
      if (x !== undefined && x !== null) {
        if ($isPlain(x)) Object.assign(o, x);
        else for (const [k, v] of $iter(x)) o[k] = v;
      }
      return Object.assign(o, kw);
    },
    ["x"], null, [], "kwargs"
  ),
  range(a, b, step = 1) {
    if (b === undefined) [a, b] = [0, a];
    const out = [];
    if (step > 0) for (let i = a; i < b; i += step) out.push(i);
    else if (step < 0) for (let i = a; i > b; i += step) out.push(i);
    else throw $b.ValueError("range() arg 3 must not be zero");
    return out;
  },
  enumerate: $fn((it, start = 0) => [...$iter(it)].map((x, i) => [i + start, x]), ["iterable", "start"]),
  zip: $fn(
    function (its) {
      const arrays = its.map((x) => [...$iter(x)]);
      const n = arrays.length ? Math.min(...arrays.map((a) => a.length)) : 0;
      return Array.from({ length: n }, (_, i) => arrays.map((a) => a[i]));
    },
    [], "iterables"
  ),
  sorted: $fn((it, key = null, reverse = false) => $sort([...$iter(it)], key, reverse), ["iterable"], null, ["key", "reverse"]),
  reversed: (it) => [...$iter(it)].reverse(),
  sum(it, start = 0) {
    let total = start;
    for (const x of $iter(it)) total = $add(total, x);
    return total;
  },
  min: $minmax("min"),
  max: $minmax("max"),
  abs: Math.abs,
  round: $round,
  any: (it) => [...$iter(it)].some($t),
  all: (it) => [...$iter(it)].every($t),
  map: (f, ...its) => $b.zip(...its).map((args) => f(...args)),
  filter: (f, it) => [...$iter(it)].filter((x) => $t(f === null ? x : f(x))),
  print: $fn((args, sep = " ") => console.log(args.map($str).join(sep)), [], "args", ["sep", "end"]),
  isinstance(x, T) {
    if (Array.isArray(T)) return T.some((t) => $b.isinstance(x, t));
    switch (T) {
      case $b.str: return typeof x === "string";
      case $b.int: return Number.isInteger(x);
      case $b.float: return typeof x === "number";
      case $b.bool: return typeof x === "boolean";
      case $b.list: case $b.tuple: return Array.isArray(x);
      case $b.dict: return $isPlain(x);
      case $b.set: return x instanceof Set;
    }
    return typeof T === "function" && x instanceof T;
  },
  repr: $repr,
  chr: (n) => String.fromCodePoint(n),
  ord: (s) => s.codePointAt(0),
  divmod: (a, b) => [$floordiv(a, b), $mod(a, b)],
  pow: (a, b, m) => {
    if (m === undefined || m === null) return a ** b;
    if (b < 0) throw $b.ValueError("pow() 2nd argument cannot be negative when 3rd argument specified");
    let base = ((a % m) + m) % m;
    let exp = b;
    let result = 1 % m;
    while (exp > 0) {
      if (exp % 2 === 1) result = (result * base) % m;
      exp = Math.floor(exp / 2);
      base = (base * base) % m;
    }
    return result;
  },
  bin: (n) => (n < 0 ? "-0b" + (-n).toString(2) : "0b" + n.toString(2)),
  oct: (n) => (n < 0 ? "-0o" + (-n).toString(8) : "0o" + n.toString(8)),
  hex: (n) => (n < 0 ? "-0x" + (-n).toString(16) : "0x" + n.toString(16)),
  frozenset: (it) => new Set(it === undefined ? [] : $iter(it)),
  hasattr: (o, name) => o !== null && o !== undefined && (name in Object(o) || $camel(name) in Object(o)),
  getattr(o, name, dflt) {
    const v = $ga(o, name);
    if (v === undefined) {
      if (dflt !== undefined) return dflt;
      throw $b.AttributeError(`'${$typename(o)}' object has no attribute '${name}'`);
    }
    return v;
  },
  setattr: (o, name, v) => $sa(o, name, v),
  callable: (f) => typeof f === "function",
});

// ---- methods on str / list / dict / set --------------------------------------------------

function $stripChars(s, chars, left, right) {
  if (chars === undefined || chars === null) {
    return left && right ? s.trim() : left ? s.trimStart() : s.trimEnd();
  }
  let a = 0;
  let b = s.length;
  while (left && a < b && chars.includes(s[a])) a++;
  while (right && b > a && chars.includes(s[b - 1])) b--;
  return s.slice(a, b);
}

const $STR = {
  upper: (s) => s.toUpperCase(),
  lower: (s) => s.toLowerCase(),
  strip: (s, [c]) => $stripChars(s, c, true, true),
  lstrip: (s, [c]) => $stripChars(s, c, true, false),
  rstrip: (s, [c]) => $stripChars(s, c, false, true),
  split(s, [sep = null, max = -1], kw) {
    sep = kw.sep !== undefined ? kw.sep : sep;
    max = kw.maxsplit !== undefined ? kw.maxsplit : max;
    let parts;
    if (sep === null) {
      const trimmed = s.trim();
      parts = trimmed ? trimmed.split(/\s+/) : [];
      if (max >= 0 && parts.length > max + 1) {
        const head = parts.slice(0, max);
        let rest = trimmed;
        for (const h of head) rest = rest.slice(rest.indexOf(h) + h.length).trimStart();
        parts = [...head, rest];
      }
      return parts;
    }
    parts = s.split(sep);
    if (max >= 0 && parts.length > max + 1) parts = [...parts.slice(0, max), parts.slice(max).join(sep)];
    return parts;
  },
  rsplit(s, [sep = null, max = -1], kw) {
    max = kw.maxsplit !== undefined ? kw.maxsplit : max;
    if (sep === null) return $STR.split(s, [null, max], {});
    if (max < 0) return s.split(sep);
    const parts = s.split(sep);
    if (parts.length <= max + 1) return parts;
    return [parts.slice(0, parts.length - max).join(sep), ...parts.slice(parts.length - max)];
  },
  partition(s, [sep]) {
    const i = s.indexOf(sep);
    return i < 0 ? [s, "", ""] : [s.slice(0, i), sep, s.slice(i + sep.length)];
  },
  rpartition(s, [sep]) {
    const i = s.lastIndexOf(sep);
    return i < 0 ? ["", "", s] : [s.slice(0, i), sep, s.slice(i + sep.length)];
  },
  removeprefix: (s, [p]) => (p && s.startsWith(p) ? s.slice(p.length) : s),
  removesuffix: (s, [p]) => (p && s.endsWith(p) ? s.slice(0, s.length - p.length) : s),
  splitlines: (s) => (s ? s.replace(/\r?\n$/, "").split(/\r?\n/) : []),
  join(s, [it]) {
    return [...$iter(it)]
      .map((x) => {
        if (typeof x !== "string") throw $b.TypeError(`sequence item: expected str instance, ${$typename(x)} found`);
        return x;
      })
      .join(s);
  },
  startswith: (s, [p]) => (Array.isArray(p) ? p.some((x) => s.startsWith(x)) : s.startsWith(p)),
  endswith: (s, [p]) => (Array.isArray(p) ? p.some((x) => s.endsWith(x)) : s.endsWith(p)),
  replace(s, [a, b, count = -1]) {
    if (count < 0) return s.split(a).join(b);
    const parts = s.split(a);
    return parts.slice(0, count + 1).join(b) + (parts.length > count + 1 ? a + parts.slice(count + 1).join(a) : "");
  },
  find: (s, [x]) => s.indexOf(x),
  rfind: (s, [x]) => s.lastIndexOf(x),
  index(s, [x]) {
    const i = s.indexOf(x);
    if (i < 0) throw $b.ValueError("substring not found");
    return i;
  },
  count: (s, [x]) => (x === "" ? s.length + 1 : s.split(x).length - 1),
  title: (s) => s.replace(/[A-Za-z]+/g, (w) => w[0].toUpperCase() + w.slice(1).toLowerCase()),
  capitalize: (s) => (s ? s[0].toUpperCase() + s.slice(1).toLowerCase() : s),
  swapcase: (s) => s.replace(/\p{L}/gu, (c) => (c === c.toLowerCase() ? c.toUpperCase() : c.toLowerCase())),
  casefold: (s) => s.toLowerCase(),
  isdigit: (s) => /^\d+$/.test(s),
  isnumeric: (s) => /^\d+$/.test(s),
  isalpha: (s) => /^\p{L}+$/u.test(s),
  isalnum: (s) => /^[\p{L}\d]+$/u.test(s),
  isspace: (s) => /^\s+$/.test(s),
  isupper: (s) => /\p{Lu}/u.test(s) && s === s.toUpperCase(),
  islower: (s) => /\p{Ll}/u.test(s) && s === s.toLowerCase(),
  format: (s, args, kw) => $formatBraces(s, args, kw),
  zfill: (s, [w]) => (s[0] === "-" || s[0] === "+" ? s[0] + s.slice(1).padStart(w - 1, "0") : s.padStart(w, "0")),
  ljust: (s, [w, c = " "]) => s.padEnd(w, c),
  rjust: (s, [w, c = " "]) => s.padStart(w, c),
  center: (s, [w, c = " "]) => $fmt(s, `${c}^${w}`),
};

const $LIST = {
  append(a, [x]) { a.push(x); },
  extend(a, [it]) { a.push(...$iter(it)); },
  insert(a, [i, x]) { a.splice(i < 0 ? Math.max(0, a.length + i) : i, 0, x); },
  pop(a, [i]) {
    if (!a.length) throw $b.IndexError("pop from empty list");
    if (i === undefined) return a.pop();
    const j = i < 0 ? a.length + i : i;
    if (j < 0 || j >= a.length) throw $b.IndexError("pop index out of range");
    return a.splice(j, 1)[0];
  },
  remove(a, [x]) {
    const i = a.findIndex((y) => $eq(x, y));
    if (i < 0) throw $b.ValueError("list.remove(x): x not in list");
    a.splice(i, 1);
  },
  index(a, [x]) {
    const i = a.findIndex((y) => $eq(x, y));
    if (i < 0) throw $b.ValueError(`${$repr(x)} is not in list`);
    return i;
  },
  count: (a, [x]) => a.filter((y) => $eq(x, y)).length,
  sort(a, args, kw) { a.splice(0, a.length, ...$sort(a, kw.key, kw.reverse)); },
  reverse(a) { a.reverse(); },
  copy: (a) => a.slice(),
  clear(a) { a.length = 0; },
};

const $DICT = {
  get: (d, [k, dflt = null]) => ($hasOwn(d, k) ? d[k] : dflt),
  keys: (d) => Object.keys(d),
  values: (d) => Object.values(d),
  items: (d) => Object.entries(d),
  pop(d, args) {
    const k = args[0];
    if ($hasOwn(d, k)) {
      const v = d[k];
      delete d[k];
      return v;
    }
    if (args.length > 1) return args[1];
    throw $b.KeyError($repr(k));
  },
  update(d, [o], kw) {
    if (o !== undefined && o !== null) {
      if ($isPlain(o)) Object.assign(d, o);
      else for (const [k, v] of $iter(o)) d[k] = v;
    }
    Object.assign(d, kw);
  },
  setdefault(d, [k, v = null]) {
    if (!$hasOwn(d, k)) d[k] = v;
    return d[k];
  },
  copy: (d) => ({ ...d }),
  clear(d) { for (const k of Object.keys(d)) delete d[k]; },
};

const $SET = {
  add(s, [x]) { s.add(x); },
  discard(s, [x]) { s.delete(x); },
  remove(s, [x]) {
    if (!s.delete(x)) throw $b.KeyError($repr(x));
  },
  union: (s, its) => new Set([...s, ...its.flatMap((it) => [...$iter(it)])]),
  intersection: (s, [it]) => new Set([...$iter(it)].filter((x) => s.has(x))),
  difference: (s, [it]) => {
    const other = new Set($iter(it));
    return new Set([...s].filter((x) => !other.has(x)));
  },
  update(s, its) { for (const it of its) for (const x of $iter(it)) s.add(x); },
  copy: (s) => new Set(s),
  clear(s) { s.clear(); },
};

function $m(o, name, args, kw) {
  if (o === null || o === undefined) throw $b.AttributeError(`'NoneType' object has no attribute '${name}'`);
  let shim;
  if (typeof o === "string") shim = $STR[name];
  else if (Array.isArray(o)) shim = $LIST[name];
  else if (o instanceof Set) shim = $SET[name];
  else if ($isPlain(o) && typeof o[name] !== "function") shim = $DICT[name];
  if (shim) return shim(o, args, kw || {});
  let f = o[name];
  if (f === undefined && name.includes("_")) f = o[$camel(name)];
  if (typeof f !== "function") throw $b.AttributeError(`'${$typename(o)}' object has no attribute '${name}'`);
  if (f.__jsig || f.__jkw || f.__jrpc) return $call(f, args, kw);
  return kw && Object.keys(kw).length ? f.apply(o, [...args, kw]) : f.apply(o, args);
}

// ---- stdlib modules available in browser code -------------------------------------------

const $modules = {
  math: {
    pi: Math.PI, e: Math.E, tau: 2 * Math.PI, inf: Infinity, nan: NaN,
    floor: Math.floor, ceil: Math.ceil, trunc: Math.trunc, sqrt: Math.sqrt, fabs: Math.abs,
    exp: Math.exp, log: (x, base) => (base === undefined ? Math.log(x) : Math.log(x) / Math.log(base)),
    log10: Math.log10, log2: Math.log2, pow: Math.pow, sin: Math.sin, cos: Math.cos, tan: Math.tan,
    asin: Math.asin, acos: Math.acos, atan: Math.atan, atan2: Math.atan2, hypot: Math.hypot,
    isnan: Number.isNaN, isinf: (x) => x === Infinity || x === -Infinity,
    radians: (d) => (d * Math.PI) / 180, degrees: (r) => (r * 180) / Math.PI,
  },
  random: {
    random: Math.random,
    uniform: (a, b) => a + Math.random() * (b - a),
    randint: (a, b) => a + Math.floor(Math.random() * (b - a + 1)),
    choice: (xs) => xs[Math.floor(Math.random() * xs.length)],
    shuffle(xs) {
      for (let i = xs.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [xs[i], xs[j]] = [xs[j], xs[i]];
      }
    },
  },
  json: {
    dumps: $fn((x, indent = null) => JSON.stringify(x, null, indent ?? undefined), ["obj"], null, ["indent"]),
    loads: (s) => JSON.parse(s),
  },
  time: { time: () => Date.now() / 1000 },
};
