// Python semantics for compiled Jongo code: truthiness, equality, operators,
// indexing, method calls with keyword arguments, builtins and exceptions.

const $hasOwn = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
const $isPlain = (o) => {
  if (o === null || typeof o !== "object") return false;
  const proto = Object.getPrototypeOf(o);
  return proto === Object.prototype || proto === null;
};
const $camel = (name) => name.replace(/_([a-z0-9])/g, (_, c) => c.toUpperCase());

// A tuple is a frozen array carrying a non-enumerable marker, so it costs nothing to read
// and still never compares equal to a list — as in Python.
function $tup(items) {
  const out = Array.isArray(items) ? items.slice() : [...$iter(items)];
  Object.defineProperty(out, "$tuple", { value: true });
  return Object.freeze(out);
}
const $isTup = (x) => Array.isArray(x) && x.$tuple === true;

// Strings are UTF-16 in JavaScript and code points in Python. The regex is a fast native
// scan, so only strings that actually contain astral characters pay for the conversion.
const $SURROGATE = /[\uD800-\uDBFF]/;
const $chars = (s) => ($SURROGATE.test(s) ? Array.from(s) : s);
const $cpLen = (s) => ($SURROGATE.test(s) ? Array.from(s).length : s.length);
const $cpSlice = (s, a, b) => ($SURROGATE.test(s) ? Array.from(s).slice(a, b).join("") : s.slice(a, b));

// What Python's str.isspace() accepts (and so strip(), split() and int()/float() skip).
// JavaScript's trim() differs: it also strips U+FEFF and misses \x1c-\x1f and \x85.
const $WS_CHARS = "\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const $WS_RE = new RegExp(`[${$WS_CHARS}]`);
const $WS_LEAD = new RegExp(`^[${$WS_CHARS}]+`);
const $WS_TRAIL = new RegExp(`[${$WS_CHARS}]+$`);
const $WS_ONLY = new RegExp(`^[${$WS_CHARS}]+$`);
const $pystrip = (s) => s.replace($WS_LEAD, "").replace($WS_TRAIL, "");
// int() and float() skip the same characters except \x1c-\x1f: CPython maps non-ASCII spaces
// to " " and then skips only C's ASCII whitespace.
const $NUM_WS = $WS_CHARS.replace("\\x1c-\\x1f ", " ");
const $NUM_STRIP = new RegExp(`^[${$NUM_WS}]+|[${$NUM_WS}]+$`, "g");
const $numstrip = (s) => s.replace($NUM_STRIP, "");

function $typename(x) {
  if (x === null || x === undefined) return "NoneType";
  if (typeof x === "string") return "str";
  if (typeof x === "number") return Number.isInteger(x) ? "int" : "float";
  if (typeof x === "boolean") return "bool";
  if (Array.isArray(x)) return x.$tuple ? "tuple" : "list";
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
  "AssertionError", "ZeroDivisionError", "NotImplementedError", "OverflowError",
]) {
  $b[name] = $defExc(name, $b.Exception);
}
const $NATIVE_EXC = new Map([
  [$b.TypeError, TypeError],
  [$b.RuntimeError, Error],
  [$b.IndexError, RangeError],
]);
// `user.name` on None compiles to a plain property read, so the browser throws its own
// TypeError. Python raises AttributeError there; match both, as the message says which.
const $NULL_ACCESS = /Cannot (?:read|set) propert(?:y|ies) of (?:null|undefined)|can't access property|is (?:null|undefined)$|(?:null|undefined) is not an object/;

function $isexc(e, T) {
  if (T === $b.Exception || T === $b.BaseException) return true;
  if (typeof T !== "function") return false;
  if (e instanceof T) return true;
  if (T === $b.AttributeError) {
    return e instanceof TypeError && !(e instanceof PyException) && $NULL_ACCESS.test(String(e.message));
  }
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
  if (typeof x === "number") return true; // only 0 is falsy; NaN is truthy in Python
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
    if ($isTup(a) !== $isTup(b)) return false; // (1, 2) != [1, 2]
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

// Python orders str by code point; JavaScript's < compares UTF-16 units, which differs only
// once astral characters are involved.
function $strcmp(a, b) {
  if (!$SURROGATE.test(a) && !$SURROGATE.test(b)) return a < b ? -1 : a > b ? 1 : 0;
  const x = Array.from(a, (c) => c.codePointAt(0));
  const y = Array.from(b, (c) => c.codePointAt(0));
  for (let i = 0; i < Math.min(x.length, y.length); i++) if (x[i] !== y[i]) return x[i] < y[i] ? -1 : 1;
  return x.length - y.length;
}

function $subset(a, b) {
  for (const x of a) if (!b.has(x)) return false;
  return true;
}

const $OPS = ["<", "<=", ">", ">="];

// a <op> b with Python's rules: numbers and strings compare as usual, lists and tuples
// lexicographically (by the first unequal item), sets by inclusion, and anything else —
// None, or a str against an int — is a TypeError rather than JavaScript's coercion.
function $rel(a, b, op) {
  if (typeof a === "boolean") a = +a;
  if (typeof b === "boolean") b = +b;
  const ta = typeof a;
  const tb = typeof b;
  let c;
  if (ta === "number" && tb === "number") {
    return op === 0 ? a < b : op === 1 ? a <= b : op === 2 ? a > b : a >= b;
  } else if (ta === "string" && tb === "string") {
    c = $strcmp(a, b);
  } else if (Array.isArray(a) && Array.isArray(b) && $isTup(a) === $isTup(b)) {
    const n = Math.min(a.length, b.length);
    for (let i = 0; i < n; i++) if (!$eq(a[i], b[i])) return $rel(a[i], b[i], op);
    c = a.length - b.length;
  } else if (a instanceof Set && b instanceof Set) {
    if (op === 0) return a.size < b.size && $subset(a, b);
    if (op === 1) return a.size <= b.size && $subset(a, b);
    if (op === 2) return a.size > b.size && $subset(b, a);
    return a.size >= b.size && $subset(b, a);
  } else {
    throw $b.TypeError(`'${$OPS[op]}' not supported between instances of '${$typename(a)}' and '${$typename(b)}'`);
  }
  return op === 0 ? c < 0 : op === 1 ? c <= 0 : op === 2 ? c > 0 : c >= 0;
}

const $lt = (a, b) => (typeof a === "number" && typeof b === "number" ? a < b : $rel(a, b, 0));
const $le = (a, b) => (typeof a === "number" && typeof b === "number" ? a <= b : $rel(a, b, 1));
const $gt = (a, b) => (typeof a === "number" && typeof b === "number" ? a > b : $rel(a, b, 2));
const $ge = (a, b) => (typeof a === "number" && typeof b === "number" ? a >= b : $rel(a, b, 3));

// Three-way comparison for sorted()/min()/max(), built on the same rules as <.
function $cmp(a, b) {
  if (typeof a === "number" && typeof b === "number") return a < b ? -1 : a > b ? 1 : 0;
  return $rel(a, b, 0) ? -1 : $rel(b, a, 0) ? 1 : 0;
}

// Python hashes True as 1 and False as 0, so a bool used as a set element or dict key
// is the same as the int. JS keeps them distinct (Set) / stringifies to "true" (object),
// so normalise bools to numbers wherever a value is used as a key/member.
function $key(x) {
  return typeof x === "boolean" ? +x : x;
}

// A dict key written with plain assignment would set the prototype when it is "__proto__".
function $setKey(o, k, v) {
  if (k === "__proto__") Object.defineProperty(o, k, { value: v, writable: true, enumerable: true, configurable: true });
  else o[k] = v;
}

// The value a set member or tuple hashes as: tuples and frozensets by their contents (so
// (1, 2) in {(1, 2)} holds), lists and dicts refused as in Python, objects by identity.
const $IDS = new WeakMap();
let $idSeq = 0;
function $hkey(x) {
  if (x === null || x === undefined) return "N";
  switch (typeof x) {
    case "boolean": return "n" + +x;
    case "number": return "n" + (x === 0 ? 0 : x);
    case "string": return "s" + JSON.stringify(x);
    case "object": case "function": break;
    default: return typeof x + String(x);
  }
  if ($isTup(x)) return "(" + x.map($hkey).join(",") + ")";
  if (x instanceof Set) return "{" + [...x].map($hkey).sort().join(",") + "}";
  if (Array.isArray(x) || $isPlain(x)) throw $b.TypeError(`unhashable type: '${$typename(x)}'`);
  let id = $IDS.get(x);
  if (id === undefined) $IDS.set(x, (id = ++$idSeq));
  return "o" + id;
}

// Python's set: members compare by value. Numbers, strings and None are their own keys, as in
// a JS Set; tuples and frozensets go through a canonical key. It is still a Set, so iteration
// keeps insertion order and `instanceof Set` holds everywhere.
class $PySet extends Set {
  constructor(items) {
    super();
    this.$k = null; // canonical key -> stored member, for tuple/frozenset/object members only
    if (items !== undefined && items !== null) for (const x of $iter(items)) this.add(x);
  }
  add(x) {
    x = $key(x);
    if (x === null || (typeof x !== "object" && typeof x !== "function")) return super.add(x);
    const k = $hkey(x);
    if (this.$k === null) this.$k = new Map();
    if (!this.$k.has(k)) {
      this.$k.set(k, x);
      super.add(x);
    }
    return this;
  }
  has(x) {
    x = $key(x);
    if (x === null || (typeof x !== "object" && typeof x !== "function")) return super.has(x);
    const k = $hkey(x);
    return this.$k !== null && this.$k.has(k);
  }
  delete(x) {
    x = $key(x);
    if (x === null || (typeof x !== "object" && typeof x !== "function")) return super.delete(x);
    const k = $hkey(x);
    if (this.$k === null || !this.$k.has(k)) return false;
    const stored = this.$k.get(k);
    this.$k.delete(k);
    return super.delete(stored);
  }
  clear() {
    super.clear();
    if (this.$k !== null) this.$k.clear();
  }
}

function $set(items) {
  return new $PySet(items);
}

function $in(x, c) {
  if (typeof c === "string") {
    if (typeof x !== "string") throw $b.TypeError(`'in <string>' requires string as left operand, not ${$typename(x)}`);
    return c.includes(x);
  }
  if (Array.isArray(c)) return c.some((y) => $eq(x, y));
  if (c instanceof Set || c instanceof Map) return c.has($key(x));
  if ($isPlain(c)) return $hasOwn(c, $key(x));
  if (c !== null && typeof c === "object") return $key(x) in c;
  throw $b.TypeError(`argument of type '${$typename(c)}' is not iterable`);
}

const $isNum = (x) => typeof x === "number" || typeof x === "boolean";

// Python's +, -, *: numbers with numbers, and the sequence cases; anything else is the
// TypeError Python raises, never JavaScript's coercion ("1,2" + 3, "a" - 1 is NaN).
function $add(a, b) {
  if ($isNum(a) && $isNum(b)) return a + b;
  if (typeof a === "string" && typeof b === "string") return a + b;
  if (Array.isArray(a) && Array.isArray(b) && $isTup(a) === $isTup(b)) return $isTup(a) ? $tup(a.concat(b)) : a.concat(b);
  if (typeof a === "string" || Array.isArray(a)) {
    throw $b.TypeError(`can only concatenate ${$typename(a)} (not "${$typename(b)}") to ${$typename(a)}`);
  }
  throw $b.TypeError(`unsupported operand type(s) for +: '${$typename(a)}' and '${$typename(b)}'`);
}

function $iadd(a, b) {
  if ($isTup(a)) return $add(a, b);  // tuples are immutable: += rebinds, as in Python
  if (Array.isArray(a)) {
    a.push(...$iter(b));
    return a;
  }
  return $add(a, b);
}

function $mul(a, b) {
  const [a0, b0] = [a, b];
  if (typeof a === "boolean") a = +a; // Python: True*[1]==[1], False*[1]==[]
  if (typeof b === "boolean") b = +b;
  if (typeof a === "number" && typeof b === "number") return a * b;
  if (typeof a === "number") [a, b] = [b, a];
  if (typeof a === "string" || Array.isArray(a)) {
    if (typeof b !== "number" || !Number.isInteger(b)) {
      throw $b.TypeError(`can't multiply sequence by non-int of type '${$typename(typeof a0 === "number" || typeof a0 === "boolean" ? a0 : b0)}'`);
    }
    if (typeof a === "string") return b > 0 ? a.repeat(b) : "";
    const out = [];
    for (let i = 0; i < b; i++) out.push(...a);
    return $isTup(a) ? $tup(out) : out;
  }
  throw $b.TypeError(`unsupported operand type(s) for *: '${$typename(a0)}' and '${$typename(b0)}'`);
}

// The in-place operators mutate a list, set or dict on the left (every alias sees it), as
// Python does; for numbers, strings and tuples they are the plain operator.
function $imul(a, b) {
  if (Array.isArray(a) && !$isTup(a)) {
    const n = typeof b === "boolean" ? +b : b;
    if (typeof n !== "number" || !Number.isInteger(n)) throw $b.TypeError(`can't multiply sequence by non-int of type '${$typename(b)}'`);
    const copy = a.slice();
    if (n <= 0) a.length = 0;
    for (let i = 1; i < n; i++) a.push(...copy);
    return a;
  }
  return $mul(a, b);
}

function $ior(a, b) {
  if (a instanceof Set && b instanceof Set) {
    for (const x of b) a.add(x);
    return a;
  }
  if ($isPlain(a) && $isPlain(b)) {
    for (const k of Object.keys(b)) $setKey(a, k, b[k]);
    return a;
  }
  return $bor(a, b);
}

function $isub(a, b) {
  if (a instanceof Set && b instanceof Set) {
    for (const x of b) a.delete(x);
    return a;
  }
  return $sub(a, b);
}

function $iand(a, b) {
  if (a instanceof Set && b instanceof Set) {
    for (const x of [...a]) if (!b.has(x)) a.delete(x);
    return a;
  }
  return $band(a, b);
}

function $ixor(a, b) {
  if (a instanceof Set && b instanceof Set) {
    for (const x of b) {
      if (a.has(x)) a.delete(x);
      else a.add(x);
    }
    return a;
  }
  return $bxor(a, b);
}

function $neg(x) {
  if (typeof x === "number" || typeof x === "boolean") return -x;
  throw $b.TypeError(`bad operand type for unary -: '${$typename(x)}'`);
}

function $pos(x) {
  if (typeof x === "number" || typeof x === "boolean") return +x;
  throw $b.TypeError(`bad operand type for unary +: '${$typename(x)}'`);
}

// ** in a call or a dict display takes a mapping (a dict here), never a list or a string.
function $mapping(x, call) {
  if ($isPlain(x)) return x;
  throw $b.TypeError(call ? `argument after ** must be a mapping, not ${$typename(x)}` : `'${$typename(x)}' object is not a mapping`);
}

function $num(x, op, other) {
  if (typeof x === "boolean") return +x;
  if (typeof x === "number") return x;
  throw $b.TypeError(`unsupported operand type(s) for ${op}: '${$typename(x)}' and '${$typename(other)}'`);
}

function $div(a, b) {
  const x = $num(a, "/", b);
  const y = $num(b, "/", a);
  if (y === 0) throw $b.ZeroDivisionError("division by zero");
  return x / y;
}

function $pow(a, b) {
  const x = $num(a, "** or pow()", b);
  const y = $num(b, "** or pow()", a);
  if (x === 0 && y < 0) throw $b.ZeroDivisionError("0.0 cannot be raised to a negative power");
  return x ** y;
}

// CPython's float_divmod, so // and % agree with Python for floats too (1 // 0.1 is 9.0,
// 0.1 % 1 is 0.1 exactly) and the remainder takes the divisor's sign.
function $divmod2(a, b, op) {
  const x = $num(a, op, b);
  const y = $num(b, op, a);
  if (y === 0) throw $b.ZeroDivisionError("integer division or modulo by zero");
  let mod = x % y;
  let div = (x - mod) / y;
  if (mod !== 0) {
    if (y < 0 !== mod < 0) {
      mod += y;
      div -= 1;
    }
  } else {
    mod = y < 0 ? -0 : 0;
  }
  let floordiv;
  if (div !== 0) {
    floordiv = Math.floor(div);
    if (div - floordiv > 0.5) floordiv += 1;
  } else {
    floordiv = x / y < 0 ? -0 : 0;
  }
  return [floordiv, mod];
}

function $floordiv(a, b) {
  return $divmod2(a, b, "//")[0];
}

function $mod(a, b) {
  if (typeof a === "string") return $percentFormat(a, b);
  return $divmod2(a, b, "%")[1];
}

// JS bitwise operators are 32-bit signed; use BigInt so ints up to the float64 range
// stay correct (1<<40, 0xFFFFFFFF & …) instead of wrapping/going negative.
function $bigint(x, op) {
  if (typeof x === "boolean") return x ? 1n : 0n;
  if (typeof x === "number" && Number.isInteger(x)) return BigInt(x);
  throw $b.TypeError(`unsupported operand type(s) for ${op}: '${$typename(x)}'`);
}

function $invert(x) {
  return Number(~$bigint(x, "~"));
}

function $bor(a, b) {
  if ($isPlain(a) && $isPlain(b)) return { ...a, ...b };
  if (a instanceof Set && b instanceof Set) return new $PySet([...a, ...b]);
  return Number($bigint(a, "|") | $bigint(b, "|"));
}

function $sub(a, b) {
  if ($isNum(a) && $isNum(b)) return a - b;
  if (a instanceof Set && b instanceof Set) return new $PySet([...a].filter((x) => !b.has(x)));
  throw $b.TypeError(`unsupported operand type(s) for -: '${$typename(a)}' and '${$typename(b)}'`);
}

function $band(a, b) {
  if (a instanceof Set && b instanceof Set) return new $PySet([...a].filter((x) => b.has(x)));
  return Number($bigint(a, "&") & $bigint(b, "&"));
}

function $bxor(a, b) {
  if (a instanceof Set && b instanceof Set) {
    return new $PySet([...a].filter((x) => !b.has(x)).concat([...b].filter((x) => !a.has(x))));
  }
  return Number($bigint(a, "^") ^ $bigint(b, "^"));
}

// A BigInt shift by a negative count shifts the other way; Python refuses it.
function $shiftCount(b, op) {
  const n = $bigint(b, op);
  if (n < 0n) throw $b.ValueError("negative shift count");
  return n;
}

function $lshift(a, b) {
  const x = $bigint(a, "<<");
  return Number(x << $shiftCount(b, "<<"));
}

function $rshift(a, b) {
  const x = $bigint(a, ">>");
  return Number(x >> $shiftCount(b, ">>"));
}

// An argument that must be an int (a bool counts as one), as Python's __index__ requires.
function $intArg(n) {
  if (typeof n === "boolean") return +n;
  if (typeof n === "number" && Number.isInteger(n)) return n;
  throw $b.TypeError(`'${$typename(n)}' object cannot be interpreted as an integer`);
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

// A list/str/tuple index: an int (a bool counts as one), as Python requires.
function $index(o, k) {
  if (typeof k === "boolean") return +k;
  if (typeof k === "number" && Number.isInteger(k)) return k;
  const what = typeof o === "string" ? "string" : $typename(o);
  throw $b.TypeError(`${what} indices must be integers or slices, not ${$typename(k)}`);
}

function $gi(o, k) {
  if (o === null || o === undefined) throw $b.TypeError("'NoneType' object is not subscriptable");
  if (Array.isArray(o) || typeof o === "string") {
    const n = $index(o, k);
    const s = typeof o === "string" ? $chars(o) : o;
    const i = n < 0 ? s.length + n : n;
    if (i < 0 || i >= s.length) throw $b.IndexError(`${$typename(o)} index out of range`);
    return s[i];
  } else if ($isPlain(o)) {
    const nk = $key(k);
    if (!$hasOwn(o, nk)) throw $b.KeyError($repr(k));
    return o[nk];
  } else if (o instanceof Map) {
    if (!o.has(k)) throw $b.KeyError($repr(k));
    return o.get(k);
  } else if ($noItems(o)) {
    throw $b.TypeError(`'${$typename(o)}' object is not subscriptable`);
  }
  return o[k];
}

// A number, bool or set has no items: o[k] would read a JavaScript property (5["valueOf"]).
const $noItems = (o) => typeof o === "number" || typeof o === "boolean" || o instanceof Set;

function $si(o, k, v) {
  if (o === null || o === undefined) throw $b.TypeError("'NoneType' object does not support item assignment");
  if ($isTup(o) || $noItems(o)) throw $b.TypeError(`'${$typename(o)}' object does not support item assignment`);
  if (Array.isArray(o)) {
    const n = $index(o, k);
    const i = n < 0 ? o.length + n : n;
    if (i < 0 || i >= o.length) throw $b.IndexError("list assignment index out of range");
    o[i] = v;
  } else if (o instanceof Map) {
    o.set(k, v);
  } else {
    $setKey(o, $isPlain(o) ? $key(k) : k, v);
  }
  return v;
}

function $del(o, k) {
  if ($isTup(o) || $noItems(o)) throw $b.TypeError(`'${$typename(o)}' object doesn't support item deletion`);
  if (Array.isArray(o)) {
    const n = $index(o, k);
    const i = n < 0 ? o.length + n : n;
    if (i < 0 || i >= o.length) throw $b.IndexError("list assignment index out of range");
    o.splice(i, 1);
  } else if (o instanceof Map) o.delete(k);
  else {
    const nk = $isPlain(o) ? $key(k) : k;
    if ($isPlain(o) && !$hasOwn(o, nk)) throw $b.KeyError($repr(k));
    delete o[nk];
  }
}

// A slice bound: an int (a bool counts as one) or None, as Python requires.
function $sliceIndex(i) {
  if (i === null || i === undefined || (typeof i === "number" && Number.isInteger(i))) return i;
  if (typeof i === "boolean") return +i;
  throw $b.TypeError("slice indices must be integers or None or have an __index__ method");
}

function $slice(source, start, stop, step) {
  const o = typeof source === "string" ? $chars(source) : source;
  const len = o.length;
  step = $sliceIndex(step);
  step = step === null || step === undefined ? 1 : step;
  if (step === 0) throw $b.ValueError("slice step cannot be zero");
  start = $sliceIndex(start);
  stop = $sliceIndex(stop);
  const norm = (i, dflt, lo, hi) => {
    if (i === null || i === undefined) return dflt;
    if (i < 0) i += len;
    return Math.max(lo, Math.min(hi, i));
  };
  let out;
  if (step === 1) {
    out = o.slice(norm(start, 0, 0, len), norm(stop, len, 0, len));
    if (typeof source === "string") return typeof out === "string" ? out : out.join("");
  } else {
    out = [];
    if (step > 0) {
      for (let i = norm(start, 0, 0, len); i < norm(stop, len, 0, len); i += step) out.push(o[i]);
    } else {
      for (let i = norm(start, len - 1, -1, len - 1); i > norm(stop, -1, -1, len - 1); i += step) out.push(o[i]);
    }
    if (typeof source === "string") return out.join("");
  }
  return $isTup(source) ? $tup(out) : out; // a slice of a tuple is a tuple
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
  if (sig.s) out.push($tup(args.slice(np))); // *args is a tuple
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

// Python's float repr: the shortest digits that round-trip (as JavaScript also picks), but
// in exponent form below 1e-4 and from 1e16 up, with a two-digit exponent. An integral value
// below 1e16 prints like an int (str(10 / 2) is "5": the documented integral-float choice).
function $numRepr(x) {
  if (!Number.isFinite(x)) return Number.isNaN(x) ? "nan" : x > 0 ? "inf" : "-inf";
  const a = Math.abs(x);
  if (a === 0 || (a >= 1e-4 && a < 1e16)) return String(x);
  const [mant, e] = x.toExponential().split("e");
  const exp = +e;
  return mant + "e" + (exp < 0 ? "-" : "+") + String(Math.abs(exp)).padStart(2, "0");
}

// Characters str.isprintable() rejects, which repr() escapes (space excepted).
const $NONPRINT = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]/u;
const $NEEDS_ESCAPE = /[\\\n\r\t'"\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]/u;

// CPython's quoting: single quotes unless the text has a ' and no ", then double quotes.
function $strRepr(s) {
  const q = s.includes("'") && !s.includes('"') ? '"' : "'";
  if (!$NEEDS_ESCAPE.test(s.replace(/ /g, ""))) return q + s + q;
  let out = q;
  for (const ch of s) {
    if (ch === q || ch === "\\") out += "\\" + ch;
    else if (ch === "\n") out += "\\n";
    else if (ch === "\r") out += "\\r";
    else if (ch === "\t") out += "\\t";
    else if (ch !== " " && $NONPRINT.test(ch)) {
      const c = ch.codePointAt(0);
      out += c < 0x100 ? "\\x" + c.toString(16).padStart(2, "0")
        : c < 0x10000 ? "\\u" + c.toString(16).padStart(4, "0") : "\\U" + c.toString(16).padStart(8, "0");
    } else out += ch;
  }
  return out + q;
}

function $repr(x) {
  if (x === null || x === undefined) return "None";
  if (x === true) return "True";
  if (x === false) return "False";
  if (typeof x === "number") return $numRepr(x);
  if (typeof x === "string") return $strRepr(x);
  if ($isTup(x)) return x.length === 1 ? "(" + $repr(x[0]) + ",)" : "(" + x.map($repr).join(", ") + ")";
  if (Array.isArray(x)) return "[" + x.map($repr).join(", ") + "]";
  if (x instanceof Set) return x.size ? "{" + [...x].map($repr).join(", ") + "}" : "set()";
  if ($isPlain(x)) return "{" + Object.entries(x).map(([k, v]) => `${$repr(k)}: ${$repr(v)}`).join(", ") + "}";
  if (x instanceof Error) return `${x.name}(${$repr(x.message)})`;
  if (typeof x === "function") return `<function ${x.name || "lambda"}>`;
  return String(x);
}

// ---- number formatting: exact decimal digits, rounded half-to-even like CPython ----------
// toFixed()/toExponential() round ties away from zero (f"{2.5:.0f}" would be "3"); CPython
// rounds the double's exact decimal value half-to-even ("2"). These helpers take the exact
// expansion and round it themselves.

// The exact decimal digits of a finite a >= 0: [integer digits, fraction digits]. toFixed
// is exact below 1e21 (it rounds the exact value, here far past any tie); above, a is an integer.
function $decimal(a, frac) {
  if (a >= 1e21) return [BigInt(a).toString(), ""];
  const s = a.toFixed(Math.min(frac, 100));
  const dot = s.indexOf(".");
  return dot < 0 ? [s, ""] : [s.slice(0, dot), s.slice(dot + 1)];
}

// Whether digits `keep` round up given the digits after them: above half, or exactly half
// with an odd last digit.
function $roundsUp(keep, rest) {
  const first = rest.charCodeAt(0) - 48;
  if (!(first >= 5)) return false;
  if (first > 5 || /[1-9]/.test(rest.slice(1))) return true;
  return keep.length > 0 && (keep.charCodeAt(keep.length - 1) - 48) % 2 === 1;
}

const $bump = (digits) => (BigInt(digits) + 1n).toString().padStart(digits.length, "0");

// f"{a:.{p}f}" for a finite a >= 0.
function $fixed(a, p) {
  const [ip, fp] = $decimal(a, p + 25);
  let keep = ip + fp.slice(0, p).padEnd(p, "0");
  if ($roundsUp(keep, fp.slice(p))) keep = $bump(keep);
  if (!p) return keep;
  return keep.slice(0, keep.length - p) + "." + keep.slice(keep.length - p);
}

// The significant digits of a finite a > 0 and its exponent: a = 0.ddd... x 10^(exp + 1).
function $sig(a) {
  const s = a.toPrecision(100);
  const ei = s.indexOf("e");
  const m = ei < 0 ? s : s.slice(0, ei);
  const e = ei < 0 ? 0 : +s.slice(ei + 1);
  const dot = m.indexOf(".");
  const ip = dot < 0 ? m : m.slice(0, dot);
  const digits = ip + (dot < 0 ? "" : m.slice(dot + 1));
  const lead = /^0*/.exec(digits)[0].length;
  return [digits.slice(lead), e + ip.length - 1 - lead];
}

// a rounded half-to-even to n significant digits: [n digits, exponent of the first one].
// The exponent is taken after rounding, so 99.5 to two digits is 1.0e+02.
function $sigRound(a, n) {
  if (a === 0) return ["0".repeat(n), 0];
  let [digits, exp] = $sig(a);
  let keep = digits.slice(0, n).padEnd(n, "0");
  if ($roundsUp(keep, digits.slice(n))) {
    keep = $bump(keep);
    if (keep.length > n) {
      keep = keep.slice(0, n);
      exp += 1;
    }
  }
  return [keep, exp];
}

const $expText = (exp) => "e" + (exp < 0 ? "-" : "+") + String(Math.abs(exp)).padStart(2, "0");
const $stripZeros = (s) => (s.includes(".") ? s.replace(/0+$/, "").replace(/\.$/, "") : s);

// f"{a:.{p}e}"
function $expFormat(a, p, alt) {
  const [keep, exp] = $sigRound(a, p + 1);
  return keep[0] + (p > 0 || alt ? "." : "") + keep.slice(1) + $expText(exp);
}

// f"{a:.{p}g}": p significant digits, exponent form when the rounded exponent is < -4 or
// >= p, trailing zeros removed unless `alt` (#). `dot0` is the no-type spec (f"{x:.3}"): fixed
// notation then needs a digit after the point, so the exponent form starts one place earlier.
function $gformat(a, p, alt, dot0) {
  if (p === 0) p = 1;
  const [keep, exp] = $sigRound(a, p);
  let s;
  if (exp < -4 || exp >= (dot0 ? p - 1 : p)) {
    s = keep[0] + (keep.length > 1 || alt ? "." : "") + keep.slice(1);
    if (!alt) s = $stripZeros(s);
    return s + $expText(exp);
  }
  if (exp >= 0) s = keep.slice(0, exp + 1) + (keep.length > exp + 1 || alt ? "." + keep.slice(exp + 1) : "");
  else s = "0." + "0".repeat(-exp - 1) + keep;
  if (!alt) s = $stripZeros(s);
  return dot0 && !s.includes(".") ? s + ".0" : s;
}

function $pad(text, width, align, fill = " ") {
  const pad = Math.max(0, width - $cpLen(text));
  if (align === "<") return text + fill.repeat(pad);
  if (align === "^") return fill.repeat(Math.floor(pad / 2)) + text + fill.repeat(Math.ceil(pad / 2));
  return fill.repeat(pad) + text;
}

const $NUMERIC_TYPES = "bcdeEfFgGnosxX%";

function $fmt(x, spec) {
  if (!spec) return $str(x);
  const m = /^(?:(.)?([<>^=]))?([+\- ])?(#)?(0)?(\d+)?([,_])?(?:\.(\d+))?([bcdeEfFgGnosxX%])?$/u.exec(spec);
  if (!m) throw $b.ValueError(`Invalid format specifier '${spec}'`);
  let [, fill = " ", align, sign, alt, zero, width, group, prec, type] = m;
  const p = prec === undefined ? undefined : +prec;
  let body;
  let signStr = "";
  let prefix = "";
  if (typeof x === "boolean") x = +x; // with a spec, a bool formats as its int, as in Python
  if (typeof x === "number") {
    let n = Math.abs(x);
    if (type === "%") n *= 100; // as CPython: multiply first (it can overflow to inf), then format "f"
    if (type === "n") type = Number.isInteger(x) && Math.abs(x) < 1e16 ? "d" : "g";
    if (!Number.isFinite(n) && !"bcdoxX".includes(type || "_")) {
      body = Number.isNaN(n) ? "nan" : "inf";
      if (type === "F" || type === "E" || type === "G") body = body.toUpperCase();
      if (type === "%") body += "%";
    } else {
      switch (type) {
        case "f": case "F": body = $fixed(n, p ?? 6) + (alt && p === 0 ? "." : ""); break;
        case "e": case "E": body = $expFormat(n, p ?? 6, alt); break;
        case "%": body = $fixed(n, p ?? 6) + (alt && p === 0 ? "." : "") + "%"; break;
        case "d": body = Number.isFinite(n) ? BigInt(Math.trunc(n)).toString() : String(n); break;
        case "x": body = Math.trunc(n).toString(16); break;
        case "X": body = Math.trunc(n).toString(16).toUpperCase(); break;
        case "b": body = Math.trunc(n).toString(2); break;
        case "o": body = Math.trunc(n).toString(8); break;
        case "c": body = String.fromCodePoint(Math.trunc(x)); break;
        case "g": case "G": body = $gformat(n, p === undefined ? 6 : p, alt); break;
        default:
          // no type: repr, or with a precision like "g" keeping a digit after the point ("1.0")
          body = p === undefined ? $numRepr(n) : $gformat(n, p, alt, true);
      }
      if (type === "E" || type === "G") body = body.toUpperCase();
    }
    if (alt) prefix = { x: "0x", X: "0X", o: "0o", b: "0b" }[type] || "";
    if (group) body = body.replace(/^(\d+)/, (d) => d.replace(/\B(?=(\d{3})+(?!\d))/g, group));
    if (type === "c") {
      align = align || "<"; // a char is aligned like a string and carries no sign
    } else {
      signStr = x < 0 ? "-" : sign === "+" ? "+" : sign === " " ? " " : "";
      if (zero && !align) {
        fill = "0";
        align = "=";
      }
      align = align || ">";
    }
  } else {
    if (type !== undefined && type !== "s" && typeof x === "string") {
      throw $b.ValueError(`Unknown format code '${type}' for object of type 'str'`);
    }
    body = $str(x);
    if (p !== undefined) body = $cpSlice(body, 0, p);
    align = align || "<";
  }
  const w = width === undefined ? 0 : +width;
  // "=" fills between the sign/prefix and the digits, so f"{255:#06x}" -> "0x00ff".
  if (align === "=") return signStr + prefix + fill.repeat(Math.max(0, w - $cpLen(body) - signStr.length - prefix.length)) + body;
  return $pad(signStr + prefix + body, w, align, fill);
}

// printf-style "%" formatting, translated to the equivalent format spec.
function $percentFormat(template, values) {
  const isTuple = $isTup(values);
  const mapping = !isTuple && ($isPlain(values) || Array.isArray(values)); // Python's PyMapping_Check
  const args = isTuple ? values : [values];
  let index = 0;
  const next = () => {
    if (index >= args.length) throw $b.TypeError("not enough arguments for format string");
    return args[index++];
  };
  const re = /%(?:\(([^)]*)\))?([-+ 0#]*)(\*|\d+)?(?:\.(\*|\d*))?[hlL]?([diouxXeEfFgGcrsa%]|[^]|$)/g;
  const out = template.replace(re, (all, key, flags, width, prec, type, offset) => {
    if (type === "") throw $b.ValueError("incomplete format");
    if (!"diouxXeEfFgGcrsa%".includes(type)) {
      throw $b.ValueError(`unsupported format character '${type}' (0x${type.codePointAt(0).toString(16)}) at index ${offset + all.length - 1}`);
    }
    if (type === "%") return "%";
    let left = flags.includes("-");
    if (width === "*") {
      width = $toInt(next());
      if (width < 0) [left, width] = [true, -width];
    }
    if (prec === "*") prec = $toInt(next());
    const w = width === undefined ? 0 : +width;
    const p = prec === undefined ? undefined : prec === "" ? 0 : +prec;
    let v = key !== undefined ? $gi(values, key) : next();
    if (type === "s" || type === "r" || type === "a") {
      let text = type === "s" ? $str(v) : $repr(v);
      if (p !== undefined) text = $cpSlice(text, 0, p);
      return $pad(text, w, left ? "<" : ">");
    }
    if (type === "c") {
      if (typeof v === "boolean") v = +v;
      if (typeof v === "number" && Number.isInteger(v)) {
        if (v < 0 || v > 0x10ffff) throw $b.OverflowError("%c arg not in range(0x110000)");
        v = String.fromCodePoint(v);
      }
      else if (typeof v !== "string" || $cpLen(v) !== 1) throw $b.TypeError("%c requires an int or a unicode character");
      return $pad(v, w, left ? "<" : ">");
    }
    if (typeof v === "boolean") v = +v;
    if (typeof v !== "number") throw $b.TypeError(`%${type} format: a real number is required, not ${$typename(v)}`);
    if ("oxX".includes(type) && !Number.isInteger(v)) throw $b.TypeError(`%${type} format: an integer is required, not float`);
    const conv = { i: "d", u: "d", d: "d" }[type] || type;
    const floaty = "eEfFgG".includes(type);
    const spec = (left ? "<" : "") + (flags.includes("+") ? "+" : flags.includes(" ") ? " " : "")
      + (flags.includes("#") && type !== "d" ? "#" : "") + (flags.includes("0") && !left ? "0" : "")
      + (w || "") + (floaty ? "." + (p ?? 6) : "") + conv;
    return $fmt(v, spec);
  });
  if (!mapping && index < args.length) throw $b.TypeError("not all arguments converted during string formatting");
  return out;
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

// Only None means "no key"; anything else must be callable (key=d.get, key=str.lower, ...).
function $keyfn(key) {
  if (key === undefined || key === null) return null;
  if (typeof key !== "function") throw $b.TypeError(`'${$typename(key)}' object is not callable`);
  return key;
}

function $sort(items, key, reverse) {
  const f = $keyfn(key);
  const decorated = items.map((x, i) => [f ? f(x) : x, i, x]);
  const sign = $t(reverse) ? -1 : 1;
  decorated.sort((a, b) => $cmp(a[0], b[0]) * sign || a[1] - b[1]);
  return decorated.map((d) => d[2]);
}

function $minmax(which) {
  return $fn(
    function (args, key, dflt) {
      const f = $keyfn(key);
      const items = args.length === 1 ? [...$iter(args[0])] : args;
      if (!items.length) {
        if (dflt !== undefined) return dflt;
        throw $b.ValueError(`${which}() arg is an empty sequence`);
      }
      let best = items[0];
      let bestKey = f ? f(best) : best;
      for (const item of items.slice(1)) {
        const k = f ? f(item) : item;
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

// Python reads any Unicode decimal digit (Arabic-Indic, Devanagari, fullwidth...) in int()
// and float(). Decimal digits come in runs of ten starting at zero, so a digit's value is its
// distance from the start of its run, mod 10.
const $ND = /\p{Nd}/u;
function $digitValue(c) {
  let cp = c.codePointAt(0);
  let n = 0;
  while (n < 100 && $ND.test(String.fromCodePoint(cp - 1))) {
    cp--;
    n++;
  }
  return n % 10;
}
const $asciiDigits = (s) => (/[^\x00-\x7f]/.test(s) ? s.replace(/\p{Nd}/gu, (c) => (c <= "9" ? c : String($digitValue(c)))) : s);

function $toInt(x, base) {
  if (typeof x === "boolean") return x ? 1 : 0;
  if (x === undefined) return 0;
  if (typeof x === "number") {
    if (!Number.isFinite(x)) {
      throw (Number.isNaN(x) ? $b.ValueError : $b.OverflowError)(`cannot convert float ${$numRepr(x)} to integer`);
    }
    return Math.trunc(x);
  }
  if (typeof x !== "string") {
    throw $b.TypeError(`int() argument must be a string, a bytes-like object or a real number, not '${$typename(x)}'`);
  }
  const b = base === undefined ? 10 : base;
  const fail = () => {
    throw $b.ValueError(`invalid literal for int() with base ${b}: ${$repr(x)}`);
  };
  let s = $numstrip($asciiDigits(x));
  let sign = 1;
  if (s[0] === "+" || s[0] === "-") {
    if (s[0] === "-") sign = -1;
    s = s.slice(1);
  }
  let radix = b;
  const low = s.toLowerCase();
  if (b === 0) {
    if (low.startsWith("0x")) (radix = 16), (s = s.slice(2));
    else if (low.startsWith("0o")) (radix = 8), (s = s.slice(2));
    else if (low.startsWith("0b")) (radix = 2), (s = s.slice(2));
    else radix = 10;
  } else if (b === 16 && low.startsWith("0x")) s = s.slice(2);
  else if (b === 8 && low.startsWith("0o")) s = s.slice(2);
  else if (b === 2 && low.startsWith("0b")) s = s.slice(2);
  if (radix < 2 || radix > 36) throw $b.ValueError("int() base must be >= 2 and <= 36, or 0");
  if (/^_|_$|__/.test(s)) fail(); // underscores only as single separators between digits
  s = s.replace(/_/g, "");
  if (s === "") fail();
  const digits = "0123456789abcdefghijklmnopqrstuvwxyz".slice(0, radix);
  if (!new RegExp(`^[${digits}]+$`, "i").test(s)) fail(); // reject trailing garbage like "1a"/"42px"
  const n = parseInt(s, radix);
  if (Number.isNaN(n)) fail();
  return sign * n;
}

// Python's float() grammar: digits with single "_" separators, an optional fraction and
// exponent, or inf/infinity/nan, around Python whitespace. Nothing else (no "", no "0x10").
const $FLOAT_RE = /^[+-]?(?:\d(?:_?\d)*(?:\.(?:\d(?:_?\d)*)?)?|\.\d(?:_?\d)*)(?:[eE][+-]?\d(?:_?\d)*)?$/;
function $toFloat(x = 0) {
  if (typeof x === "boolean") return x ? 1 : 0;
  if (typeof x === "number") return x;
  if (typeof x !== "string") {
    throw $b.TypeError(`float() argument must be a string or a real number, not '${$typename(x)}'`);
  }
  const s = $numstrip($asciiDigits(x));
  const low = s.toLowerCase();
  const body = low.replace(/^[+-]/, "");
  if (body === "inf" || body === "infinity") return low[0] === "-" ? -Infinity : Infinity;
  if (body === "nan") return NaN;
  if (!$FLOAT_RE.test(s)) throw $b.ValueError(`could not convert string to float: ${$repr(x)}`);
  return Number(s.replace(/_/g, ""));
}

// round(x, n) on the double's exact decimal value, half-to-even, as CPython does: round(2.675,
// 2) is 2.67 and round(1e22, 1) is 1e22. The rounded decimal is parsed back with Number(),
// which rounds correctly, so no digit is lost on the way.
function $round(x, n) {
  if (typeof x === "boolean") x = +x;
  if (typeof x !== "number") throw $b.TypeError(`type ${$typename(x)} doesn't define __round__ method`);
  if (n === undefined || n === null) {
    // round(x) is an int, so like int(x) it refuses what no int can be (round(x, n) doesn't).
    if (Number.isNaN(x)) throw $b.ValueError("cannot convert float NaN to integer");
    if (!Number.isFinite(x)) throw $b.OverflowError("cannot convert float infinity to integer");
    const r = Math.round(x);
    return Math.abs(x % 1) === 0.5 && r % 2 !== 0 ? r - 1 : r; // banker's rounding
  }
  if (typeof n === "boolean") n = +n;
  if (!Number.isInteger(n)) throw $b.TypeError(`'${$typename(n)}' object cannot be interpreted as an integer`);
  if (!Number.isFinite(x)) return x;
  const a = Math.abs(x);
  if (n >= 0 && a >= 2 ** 52) return x; // already a whole number, so exact at any n >= 0
  const [ip, fp] = $decimal(a, n >= 0 ? n + 25 : 60);
  let text;
  if (n >= 0) {
    let keep = ip + fp.slice(0, n).padEnd(n, "0");
    if ($roundsUp(keep, fp.slice(n))) keep = $bump(keep);
    text = n ? keep.slice(0, keep.length - n) + "." + keep.slice(keep.length - n) : keep;
  } else {
    const k = -n;
    let keep = ip.slice(0, Math.max(0, ip.length - k));
    const rest = ip.slice(Math.max(0, ip.length - k)).padStart(k, "0") + fp;
    if ($roundsUp(keep, rest)) keep = $bump(keep || "0");
    text = (keep || "0") + "0".repeat(k);
  }
  const result = Number(text);
  return x < 0 ? -result : result;
}

// sum() of numbers with Neumaier's compensated summation, as CPython 3.12+ does for floats,
// so sum([0.1] * 10) is 1.0. (CPython adds ints into a float total without the compensation;
// a JavaScript number can't say whether 1.0 was an int, so every number is compensated, which
// differs only when huge values cancel around an int.) Other items use Python's +.
function $sum(it, start = 0) {
  if (typeof start === "string") throw $b.TypeError("sum() can't sum strings [use ''.join(seq) instead]");
  let hi = typeof start === "boolean" ? +start : start;
  let c = 0;
  let numeric = typeof hi === "number";
  let total = hi;
  for (let x of $iter(it)) {
    if (numeric && (typeof x === "number" || typeof x === "boolean")) {
      x = +x;
      const t = hi + x;
      if (Math.abs(hi) >= Math.abs(x)) c += hi - t + x;
      else c += x - t + hi;
      hi = t;
      continue;
    }
    if (numeric) {
      total = c !== 0 && Number.isFinite(c) ? hi + c : hi;
      numeric = false;
    }
    if (typeof x === "string") throw $b.TypeError(`unsupported operand type(s) for +: '${$typename(total)}' and 'str'`);
    total = $add(total, x);
  }
  return numeric ? (c !== 0 && Number.isFinite(c) ? hi + c : hi) : total;
}

// pow(base, exp, mod) in BigInt: float64 products go wrong long before the inputs reach
// 2**53. A negative exponent means the modular inverse, as in Python 3.8+.
function $powmod(a, b, m) {
  let base = $bigint(a, "pow()");
  let exp = $bigint(b, "pow()");
  const mod = $bigint(m, "pow()");
  if (mod === 0n) throw $b.ValueError("pow() 3rd argument cannot be 0");
  const n = mod < 0n ? -mod : mod;
  base = ((base % n) + n) % n;
  if (exp < 0n) {
    let [r0, r1, s0, s1] = [base, n, 1n, 0n];
    while (r1 !== 0n) {
      const q = r0 / r1;
      [r0, r1] = [r1, r0 - q * r1];
      [s0, s1] = [s1, s0 - q * s1];
    }
    if (r0 !== 1n) throw $b.ValueError("base is not invertible for the given modulus");
    base = ((s0 % n) + n) % n;
    exp = -exp;
  }
  let result = 1n % n;
  while (exp > 0n) {
    if (exp & 1n) result = (result * base) % n;
    exp >>= 1n;
    base = (base * base) % n;
  }
  if (mod < 0n && result !== 0n) result += mod; // the result takes the modulus's sign
  return Number(result);
}

Object.assign($b, {
  len(x) {
    if (typeof x === "string") return $chars(x).length;
    if (Array.isArray(x)) return x.length;
    if (x instanceof Set || x instanceof Map) return x.size;
    if ($isPlain(x)) return Object.keys(x).length;
    if (x !== null && x !== undefined && typeof x.length === "number") return x.length;
    throw $b.TypeError(`object of type '${$typename(x)}' has no len()`);
  },
  str: (x = "") => $str(x),
  int: $toInt,
  float: $toFloat,
  bool: (x) => $t(x),
  list: (x) => (x === undefined ? [] : [...$iter(x)]),
  tuple: (x) => $tup(x === undefined ? [] : [...$iter(x)]),
  set: (x) => new $PySet(x),
  dict: $fn(
    function (x, kw) {
      const o = {};
      if (x !== undefined && x !== null) {
        if ($isPlain(x)) for (const k of Object.keys(x)) $setKey(o, k, x[k]);
        else for (const [k, v] of $iter(x)) $setKey(o, $key(k), v);
      }
      if (kw) for (const k of Object.keys(kw)) $setKey(o, k, kw[k]); // absent when called without keywords
      return o;
    },
    ["$iterable"], null, [], "kwargs"
  ),
  range(a, b, step = 1) {
    if (b === undefined) [a, b] = [0, a];
    const out = [];
    if (step > 0) for (let i = a; i < b; i += step) out.push(i);
    else if (step < 0) for (let i = a; i > b; i += step) out.push(i);
    else throw $b.ValueError("range() arg 3 must not be zero");
    return out;
  },
  enumerate: $fn((it, start = 0) => [...$iter(it)].map((x, i) => $tup([i + start, x])), ["iterable", "start"]),
  zip: $fn(
    function (its) {
      const arrays = its.map((x) => [...$iter(x)]);
      const n = arrays.length ? Math.min(...arrays.map((a) => a.length)) : 0;
      return Array.from({ length: n }, (_, i) => $tup(arrays.map((a) => a[i])));
    },
    [], "iterables"
  ),
  sorted: $fn((it, key = null, reverse = false) => $sort([...$iter(it)], key, reverse), ["iterable"], null, ["key", "reverse"]),
  reversed: (it) => [...$iter(it)].reverse(),
  sum: $fn($sum, ["iterable", "start"]),
  min: $minmax("min"),
  max: $minmax("max"),
  abs(x) {
    if (typeof x === "boolean") return +x;
    if (typeof x !== "number") throw $b.TypeError(`bad operand type for abs(): '${$typename(x)}'`);
    return Math.abs(x);
  },
  round: $fn($round, ["number", "ndigits"]),
  any: (it) => [...$iter(it)].some($t),
  all: (it) => [...$iter(it)].every($t),
  map: (f, ...its) => $b.zip(...its).map((args) => f(...args)),
  filter: (f, it) => [...$iter(it)].filter((x) => $t(f === null ? x : f(x))),
  print: $fn((args, sep = " ") => console.log(args.map($str).join(sep)), [], "args", ["sep", "end"]),
  isinstance(x, T) {
    if (Array.isArray(T)) return T.some((t) => $b.isinstance(x, t));
    switch (T) {
      case $b.str: return typeof x === "string";
      // bool is a subclass of int in Python (isinstance(True, int) is True).
      case $b.int: return typeof x === "boolean" || (typeof x === "number" && Number.isInteger(x));
      case $b.float: return typeof x === "number";
      case $b.bool: return typeof x === "boolean";
      case $b.list: return Array.isArray(x) && !x.$tuple;
      case $b.tuple: return $isTup(x);
      case $b.dict: return $isPlain(x);
      case $b.set: return x instanceof Set;
    }
    return typeof T === "function" && x instanceof T;
  },
  repr: $repr,
  chr(n) {
    n = $intArg(n);
    if (n > 2147483647 || n < -2147483648) throw $b.OverflowError("Python int too large to convert to C int");
    if (n < 0 || n > 0x10ffff) throw $b.ValueError("chr() arg not in range(0x110000)");
    return String.fromCodePoint(n);
  },
  ord(s) {
    if (typeof s !== "string") throw $b.TypeError(`ord() expected string of length 1, but ${$typename(s)} found`);
    const n = $cpLen(s);
    if (n !== 1) throw $b.TypeError(`ord() expected a character, but string of length ${n} found`);
    return s.codePointAt(0);
  },
  divmod: (a, b) => $tup($divmod2(a, b, "divmod()")),
  pow: $fn((base, exp, mod = null) => (mod === null || mod === undefined ? $pow(base, exp) : $powmod(base, exp, mod)),
    ["base", "exp", "mod"]),
  bin: (n) => (n < 0 ? "-0b" + (-n).toString(2) : "0b" + n.toString(2)),
  oct: (n) => (n < 0 ? "-0o" + (-n).toString(8) : "0o" + n.toString(8)),
  hex: (n) => (n < 0 ? "-0x" + (-n).toString(16) : "0x" + n.toString(16)),
  frozenset: (it) => new $PySet(it),
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
    if (left) s = s.replace($WS_LEAD, "");
    return right ? s.replace($WS_TRAIL, "") : s;
  }
  if (typeof chars !== "string") throw $b.TypeError(`strip arg must be None or str, not ${$typename(chars)}`);
  const set = new Set(chars); // by code point
  const cs = $chars(s);
  let a = 0;
  let b = cs.length;
  while (left && a < b && set.has(cs[a])) a++;
  while (right && b > a && set.has(cs[b - 1])) b--;
  return typeof cs === "string" ? cs.slice(a, b) : cs.slice(a, b).join("");
}

// Python's split()/rsplit() with no separator: runs of whitespace separate, leading and
// trailing whitespace is ignored, and after maxsplit splits the rest is kept as it is.
function $splitWs(s, max) {
  const out = [];
  const n = s.length;
  let i = 0;
  for (;;) {
    while (i < n && $WS_RE.test(s[i])) i++;
    if (i >= n) break;
    if (max >= 0 && out.length === max) {
      out.push(s.slice(i));
      break;
    }
    let j = i;
    while (j < n && !$WS_RE.test(s[j])) j++;
    out.push(s.slice(i, j));
    i = j;
  }
  return out;
}

function $rsplitWs(s, max) {
  const out = [];
  let j = s.length;
  for (;;) {
    while (j > 0 && $WS_RE.test(s[j - 1])) j--;
    if (j <= 0) break;
    if (max >= 0 && out.length === max) {
      out.push(s.slice(0, j));
      break;
    }
    let i = j;
    while (i > 0 && !$WS_RE.test(s[i - 1])) i--;
    out.push(s.slice(i, j));
    j = i;
  }
  return out.reverse();
}

function $sepArg(sep) {
  if (typeof sep !== "string") throw $b.TypeError(`must be str or None, not ${$typename(sep)}`);
  if (sep === "") throw $b.ValueError("empty separator");
  return sep;
}

function $strArg(x, what) {
  if (typeof x !== "string") throw $b.TypeError(`${what} arg must be str, not ${$typename(x)}`);
  return x;
}

// start/end arguments of find/count/startswith/index, normalised like slice bounds.
function $bounds(len, start, end) {
  let a = start === undefined || start === null ? 0 : start;
  let b = end === undefined || end === null ? len : end;
  if (a < 0) a = Math.max(0, a + len);
  if (b < 0) b = Math.max(0, b + len);
  return [a, Math.min(b, len)];
}

// s[start:end] in code points and its offset, or null when the range is empty-past-the-end
// (Python's find/startswith answer -1/False there even for an empty needle).
function $within(s, start, end) {
  const cs = $chars(s);
  const [a, b] = $bounds(cs.length, start, end);
  if (a > b) return null;
  if (a === 0 && b === cs.length) return [s, 0];
  return [typeof cs === "string" ? cs.slice(a, b) : cs.slice(a, b).join(""), a];
}

// Search results are UTF-16 offsets; Python's are code points.
const $cpIndex = (s, i) => (i < 0 ? -1 : $cpLen(s.slice(0, i)));

function $find(s, x, start, end, last) {
  $strArg(x, last ? "rfind" : "find");
  const r = $within(s, start, end);
  if (r === null) return -1;
  const i = last ? r[0].lastIndexOf(x) : r[0].indexOf(x);
  return i < 0 ? -1 : r[1] + $cpIndex(r[0], i);
}

function $affix(s, p, start, end, ends) {
  const r = $within(s, start, end);
  if (r === null) return false;
  const test = (x) => (ends ? r[0].endsWith($strArg(x, "endswith")) : r[0].startsWith($strArg(x, "startswith")));
  return $isTup(p) || Array.isArray(p) ? p.some(test) : test(p);
}

// Python's titlecase where it isn't the uppercase (sharp s -> Ss, the dz/lj/nj digraphs, ligatures, Georgian, Greek
// with ypogegrammeni); everything else titlecases to its uppercase.
const $TITLE = {
  "\u00df": "Ss", "\u01c4": "\u01c5", "\u01c5": "\u01c5", "\u01c6": "\u01c5", "\u01c7": "\u01c8", "\u01c8": "\u01c8",
  "\u01c9": "\u01c8", "\u01ca": "\u01cb", "\u01cb": "\u01cb", "\u01cc": "\u01cb", "\u01f1": "\u01f2", "\u01f2": "\u01f2",
  "\u01f3": "\u01f2", "\u0587": "\u0535\u0582", "\u1fb2": "\u1fba\u0345", "\u1fb4": "\u0386\u0345",
  "\u1fb7": "\u0391\u0342\u0345", "\u1fc2": "\u1fca\u0345", "\u1fc4": "\u0389\u0345", "\u1fc7": "\u0397\u0342\u0345",
  "\u1ff2": "\u1ffa\u0345", "\u1ff4": "\u038f\u0345", "\u1ff7": "\u03a9\u0342\u0345", "\ufb00": "Ff", "\ufb01": "Fi",
  "\ufb02": "Fl", "\ufb03": "Ffi", "\ufb04": "Ffl", "\ufb05": "St", "\ufb06": "St", "\ufb13": "\u0544\u0576",
  "\ufb14": "\u0544\u0565", "\ufb15": "\u0544\u056b", "\ufb16": "\u054e\u0576", "\ufb17": "\u0544\u056d",
};
function $titleChar(c) {
  if ($hasOwn($TITLE, c)) return $TITLE[c];
  const cp = c.codePointAt(0);
  if (cp >= 0x10d0 && cp <= 0x10ff) return c; // Georgian letters are their own titlecase
  if (cp >= 0x1f80 && cp <= 0x1faf) return String.fromCodePoint(cp | 8);
  if (cp === 0x1fb3 || cp === 0x1fc3 || cp === 0x1ff3) return String.fromCodePoint(cp + 9);
  if (cp === 0x1fbc || cp === 0x1fcc || cp === 0x1ffc) return c;
  return c.toUpperCase();
}

const $CASED = /\p{Cased}/u;
// The full case folds that differ from lowercasing (outside Cherokee and Greek extended).
const $FOLD = {
  "\u00b5": "\u03bc", "\u00df": "ss", "\u0149": "\u02bcn", "\u017f": "s", "\u01f0": "j\u030c", "\u0345": "\u03b9",
  "\u0390": "\u03b9\u0308\u0301", "\u03b0": "\u03c5\u0308\u0301", "\u03c2": "\u03c3", "\u03d0": "\u03b2",
  "\u03d1": "\u03b8", "\u03d5": "\u03c6", "\u03d6": "\u03c0", "\u03f0": "\u03ba", "\u03f1": "\u03c1",
  "\u03f5": "\u03b5", "\u0587": "\u0565\u0582", "\u1c80": "\u0432", "\u1c81": "\u0434", "\u1c82": "\u043e",
  "\u1c83": "\u0441", "\u1c84": "\u0442", "\u1c85": "\u0442", "\u1c86": "\u044a", "\u1c87": "\u0463",
  "\u1c88": "\ua64b", "\u1e96": "h\u0331", "\u1e97": "t\u0308", "\u1e98": "w\u030a", "\u1e99": "y\u030a",
  "\u1e9a": "a\u02be", "\u1e9b": "\u1e61", "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi",
  "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st", "\ufb13": "\u0574\u0576", "\ufb14": "\u0574\u0565",
  "\ufb15": "\u0574\u056b", "\ufb16": "\u057e\u0576", "\ufb17": "\u0574\u056d",
};
const $FOLD_RE = new RegExp("[" + Object.keys($FOLD).join("") + "]", "gu");

// The characters str.isdigit() accepts besides the decimal digits (superscripts, circled
// digits...), and the CJK numerals isnumeric() accepts besides category N.
const $DIGIT_RE = /^[\p{Nd}\u{b2}-\u{b3}\u{b9}\u{1369}-\u{1371}\u{19da}\u{2070}\u{2074}-\u{2079}\u{2080}-\u{2089}\u{2460}-\u{2468}\u{2474}-\u{247c}\u{2488}-\u{2490}\u{24ea}\u{24f5}-\u{24fd}\u{24ff}\u{2776}-\u{277e}\u{2780}-\u{2788}\u{278a}-\u{2792}\u{10a40}-\u{10a43}\u{10e60}-\u{10e68}\u{11052}-\u{1105a}\u{1f100}-\u{1f10a}]+$/u;
const $CJK_NUM = "\u{3405}\u{3483}\u{382a}\u{3b4d}\u{4e00}\u{4e03}\u{4e07}\u{4e09}\u{4e5d}\u{4e8c}\u{4e94}\u{4e96}\u{4ebf}-\u{4ec0}\u{4edf}\u{4ee8}\u{4f0d}\u{4f70}\u{5104}\u{5146}\u{5169}\u{516b}\u{516d}\u{5341}\u{5343}-\u{5345}\u{534c}\u{53c1}-\u{53c4}\u{56db}\u{58f1}\u{58f9}\u{5e7a}\u{5efe}-\u{5eff}\u{5f0c}-\u{5f0e}\u{5f10}\u{62fe}\u{634c}\u{67d2}\u{6f06}\u{7396}\u{767e}\u{8086}\u{842c}\u{8cae}\u{8cb3}\u{8d30}\u{9621}\u{9646}\u{964c}\u{9678}\u{96f6}\u{f96b}\u{f973}\u{f978}\u{f9b2}\u{f9d1}\u{f9d3}\u{f9fd}\u{20001}\u{20064}\u{200e2}\u{20121}\u{2092a}\u{20983}\u{2098c}\u{2099c}\u{20aea}\u{20afd}\u{20b19}\u{22390}\u{22998}\u{23b1b}\u{2626d}\u{2f890}";
const $NUMERIC_RE = new RegExp(`^[\\p{N}${$CJK_NUM}]+$`, "u");
const $ALNUM_RE = new RegExp(`^[\\p{L}\\p{N}${$CJK_NUM}]+$`, "u");

function $fillArg(c) {
  if (typeof c !== "string" || $cpLen(c) !== 1) throw $b.TypeError("The fill character must be exactly one character long");
  return c;
}

const $LINE_BREAK = /\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]/g;

const $STR = {
  upper: (s) => s.toUpperCase(),
  lower: (s) => s.toLowerCase(),
  strip: (s, [c]) => $stripChars(s, c, true, true),
  lstrip: (s, [c]) => $stripChars(s, c, true, false),
  rstrip: (s, [c]) => $stripChars(s, c, false, true),
  split(s, [sep = null, max = -1], kw) {
    sep = kw.sep !== undefined ? kw.sep : sep;
    max = kw.maxsplit !== undefined ? kw.maxsplit : max;
    if (sep === null || sep === undefined) return $splitWs(s, max);
    const parts = s.split($sepArg(sep));
    return max >= 0 && parts.length > max + 1 ? [...parts.slice(0, max), parts.slice(max).join(sep)] : parts;
  },
  rsplit(s, [sep = null, max = -1], kw) {
    sep = kw.sep !== undefined ? kw.sep : sep;
    max = kw.maxsplit !== undefined ? kw.maxsplit : max;
    if (sep === null || sep === undefined) return $rsplitWs(s, max);
    const parts = s.split($sepArg(sep));
    if (max < 0 || parts.length <= max + 1) return parts;
    return [parts.slice(0, parts.length - max).join(sep), ...parts.slice(parts.length - max)];
  },
  partition(s, [sep]) {
    const i = s.indexOf($sepArg(sep));
    return $tup(i < 0 ? [s, "", ""] : [s.slice(0, i), sep, s.slice(i + sep.length)]);
  },
  rpartition(s, [sep]) {
    const i = s.lastIndexOf($sepArg(sep));
    return $tup(i < 0 ? ["", "", s] : [s.slice(0, i), sep, s.slice(i + sep.length)]);
  },
  removeprefix: (s, [p]) => (p && s.startsWith(p) ? s.slice(p.length) : s),
  removesuffix: (s, [p]) => (p && s.endsWith(p) ? s.slice(0, s.length - p.length) : s),
  splitlines(s, [keepends = false], kw) {
    keepends = $t(kw.keepends !== undefined ? kw.keepends : keepends);
    const out = [];
    let last = 0;
    for (const m of s.matchAll($LINE_BREAK)) {
      out.push(s.slice(last, keepends ? m.index + m[0].length : m.index));
      last = m.index + m[0].length;
    }
    if (last < s.length) out.push(s.slice(last));
    return out;
  },
  join(s, [it]) {
    return [...$iter(it)]
      .map((x) => {
        if (typeof x !== "string") throw $b.TypeError(`sequence item: expected str instance, ${$typename(x)} found`);
        return x;
      })
      .join(s);
  },
  startswith: (s, [p, start, end]) => $affix(s, p, start, end, false),
  endswith: (s, [p, start, end]) => $affix(s, p, start, end, true),
  replace(s, [a, b, count = -1], kw) {
    count = kw.count !== undefined ? kw.count : count;
    $strArg(a, "replace");
    $strArg(b, "replace");
    if (a === "") {
      // insert between every code point and at both ends, at most `count` times
      const cs = Array.from(s);
      const n = count < 0 ? cs.length + 1 : Math.min(count, cs.length + 1);
      let out = "";
      for (let i = 0; i < cs.length; i++) out += (i < n ? b : "") + cs[i];
      return n > cs.length ? out + b : out;
    }
    if (count < 0) return s.split(a).join(b);
    const parts = s.split(a);
    return parts.slice(0, count + 1).join(b) + (parts.length > count + 1 ? a + parts.slice(count + 1).join(a) : "");
  },
  find: (s, [x, start, end]) => $find(s, x, start, end, false),
  rfind: (s, [x, start, end]) => $find(s, x, start, end, true),
  index(s, [x, start, end]) {
    const i = $find(s, x, start, end, false);
    if (i < 0) throw $b.ValueError("substring not found");
    return i;
  },
  rindex(s, [x, start, end]) {
    const i = $find(s, x, start, end, true);
    if (i < 0) throw $b.ValueError("substring not found");
    return i;
  },
  count(s, [x, start, end]) {
    $strArg(x, "count");
    const r = $within(s, start, end);
    if (r === null) return 0;
    return x === "" ? $cpLen(r[0]) + 1 : r[0].split(x).length - 1;
  },
  // Python: a cased character after another cased one is lowercased, any other is titlecased.
  title(s) {
    let out = "";
    let prevCased = false;
    for (const c of s) {
      out += prevCased ? c.toLowerCase() : $titleChar(c);
      prevCased = $CASED.test(c);
    }
    return out;
  },
  capitalize(s) {
    if (!s) return s;
    const first = String.fromCodePoint(s.codePointAt(0));
    return $titleChar(first) + s.slice(first.length).toLowerCase();
  },
  swapcase: (s) => s.replace(/\p{L}/gu, (c) => (c === c.toLowerCase() ? c.toUpperCase() : c.toLowerCase())),
  casefold: (s) => s.toLowerCase().replace($FOLD_RE, (c) => $FOLD[c]),
  isdigit: (s) => $DIGIT_RE.test(s),
  isdecimal: (s) => /^\p{Nd}+$/u.test(s),
  isnumeric: (s) => $NUMERIC_RE.test(s),
  isalpha: (s) => /^\p{L}+$/u.test(s),
  isalnum: (s) => $ALNUM_RE.test(s),
  isspace: (s) => $WS_ONLY.test(s),
  isupper: (s) => /\p{Lu}/u.test(s) && s === s.toUpperCase(),
  islower: (s) => /\p{Ll}/u.test(s) && s === s.toLowerCase(),
  format: (s, args, kw) => $formatBraces(s, args, kw),
  zfill(s, [w]) {
    const n = w - $cpLen(s);
    if (n <= 0) return s;
    return s[0] === "-" || s[0] === "+" ? s[0] + "0".repeat(n) + s.slice(1) : "0".repeat(n) + s;
  },
  expandtabs(s, [tabsize = 8], kw) {
    const size = $intArg(kw.tabsize !== undefined ? kw.tabsize : tabsize);
    let out = "";
    let column = 0;
    for (const c of s) {
      if (c === "\t") {
        if (size > 0) {
          const n = size - (column % size);
          out += " ".repeat(n);
          column += n;
        }
      } else {
        out += c;
        column = c === "\n" || c === "\r" ? 0 : column + 1;
      }
    }
    return out;
  },
  ljust: (s, [w, c = " "]) => s + $fillArg(c).repeat(Math.max(0, w - $cpLen(s))),
  rjust: (s, [w, c = " "]) => $fillArg(c).repeat(Math.max(0, w - $cpLen(s))) + s,
  // CPython puts the odd padding character on the left when the width is odd.
  center(s, [w, c = " "]) {
    $fillArg(c);
    const marg = w - $cpLen(s);
    if (marg <= 0) return s;
    const left = Math.floor(marg / 2) + (marg & w & 1);
    return c.repeat(left) + s + c.repeat(marg - left);
  },
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
  index(a, [x, start, end]) {
    const [lo, hi] = $bounds(a.length, start, end);
    for (let i = lo; i < hi; i++) if ($eq(x, a[i])) return i;
    throw $b.ValueError(`${$repr(x)} is not in ${$isTup(a) ? "tuple" : "list"}`);
  },
  count: (a, [x]) => a.filter((y) => $eq(x, y)).length,
  sort(a, args, kw) { a.splice(0, a.length, ...$sort(a, kw.key, kw.reverse)); },
  reverse(a) { a.reverse(); },
  copy: (a) => a.slice(),
  clear(a) { a.length = 0; },
};

const $DICT = {
  get: (d, [k, dflt = null]) => ($hasOwn(d, $key(k)) ? d[$key(k)] : dflt),
  keys: (d) => Object.keys(d),
  values: (d) => Object.values(d),
  items: (d) => Object.entries(d).map($tup),
  pop(d, args) {
    const k = $key(args[0]);
    if ($hasOwn(d, k)) {
      const v = d[k];
      delete d[k];
      return v;
    }
    if (args.length > 1) return args[1];
    throw $b.KeyError($repr(args[0]));
  },
  update(d, [o], kw) {
    if (o !== undefined && o !== null) {
      if ($isPlain(o)) for (const k of Object.keys(o)) $setKey(d, k, o[k]);
      else for (const [k, v] of $iter(o)) $setKey(d, $key(k), v);
    }
    for (const k of Object.keys(kw)) $setKey(d, k, kw[k]);
  },
  setdefault(d, [k, v = null]) {
    const nk = $key(k);
    if (!$hasOwn(d, nk)) $setKey(d, nk, v);
    return d[nk];
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
  union: (s, its) => new $PySet([...s, ...its.flatMap((it) => [...$iter(it)])]),
  intersection(s, its) {
    let out = [...s];
    for (const it of its) {
      const other = new $PySet(it);
      out = out.filter((x) => other.has(x));
    }
    return new $PySet(out);
  },
  difference(s, its) {
    let out = [...s];
    for (const it of its) {
      const other = new $PySet(it);
      out = out.filter((x) => !other.has(x));
    }
    return new $PySet(out);
  },
  symmetric_difference: (s, [it]) => $bxor(s, new $PySet(it)),
  issubset: (s, [it]) => $subset(s, new $PySet(it)),
  issuperset: (s, [it]) => $subset(new $PySet(it), s),
  isdisjoint: (s, [it]) => [...$iter(it)].every((x) => !s.has(x)),
  update(s, its) { for (const it of its) for (const x of $iter(it)) s.add(x); },
  copy: (s) => new $PySet(s),
  clear(s) { s.clear(); },
};

const $TUPLE_METHODS = new Set(["index", "count"]);

// The runtime's own method for o.name (str/list/tuple/set/dict), if there is one. A dict's own
// key of that name wins for attribute reads (todo.count), as it always has.
function $shim(o, name, reading) {
  if (typeof o === "string") return $hasOwn($STR, name) ? $STR[name] : undefined;
  if (Array.isArray(o)) {
    if (o.$tuple && !$TUPLE_METHODS.has(name)) return undefined;
    return $hasOwn($LIST, name) ? $LIST[name] : undefined;
  }
  if (o instanceof Set) return $hasOwn($SET, name) ? $SET[name] : undefined;
  if ($isPlain(o) && (reading ? !$hasOwn(o, name) : typeof o[name] !== "function")) {
    return $hasOwn($DICT, name) ? $DICT[name] : undefined;
  }
  return undefined;
}

function $m(o, name, args, kw) {
  if (o === null || o === undefined) throw $b.AttributeError(`'NoneType' object has no attribute '${name}'`);
  if ($isTup(o) && !$TUPLE_METHODS.has(name)) throw $b.AttributeError(`'tuple' object has no attribute '${name}'`);
  const shim = $shim(o, name, false);
  if (shim) return shim(o, args, kw || {});
  let f = o[name];
  if (f === undefined && name.includes("_")) f = o[$camel(name)];
  if (typeof f !== "function") throw $b.AttributeError(`'${$typename(o)}' object has no attribute '${name}'`);
  if (f.__jsig || f.__jkw || f.__jrpc) return $call(f, args, kw);
  return kw && Object.keys(kw).length ? f.apply(o, [...args, kw]) : f.apply(o, args);
}

// A method read without calling it, as Python's bound methods: key=scores.get, map(s.strip, ...).
function $bm(o, name) {
  if (o === null || o === undefined) throw $b.AttributeError(`'NoneType' object has no attribute '${name}'`);
  if (!$shim(o, name, true)) return $ga(o, name);
  const f = function (...args) {
    const kw = args.length && args[args.length - 1] instanceof KW ? args.pop().o : undefined;
    return $m(o, name, args, kw);
  };
  f.__jkw = true;
  return f;
}

// str.lower, dict.get, list.append...: the method of a builtin type, taking the object first.
const $unbound = new Map();
function $um(name) {
  let f = $unbound.get(name);
  if (f === undefined) {
    f = function (self, ...args) {
      const kw = args.length && args[args.length - 1] instanceof KW ? args.pop().o : undefined;
      return $m(self, name, args, kw);
    };
    f.__jkw = true;
    $unbound.set(name, f);
  }
  return f;
}

// ---- stdlib modules available in browser code -------------------------------------------

// jongo.forms.matches(pattern, text): the whole value must match, as re.fullmatch does.
// Patterns arrive already rewritten to the syntax Python and JavaScript share.
const $patterns = new Map();
function $matches(pattern, text) {
  let re = $patterns.get(pattern);
  if (re === undefined) {
    re = new RegExp("^(?:" + pattern + ")$", "u");
    $patterns.set(pattern, re);
  }
  return typeof text === "string" && re.test(text);
}

// ---- json --------------------------------------------------------------------------------

const $JSONDecodeError = $defExc("JSONDecodeError", $b.ValueError);

function $jsonString(s, ascii) {
  const re = $t(ascii) ? /[\\"\x00-\x1f\x7f-\uffff]/g : /[\\"\x00-\x1f]/g;
  const esc = { '"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f" };
  return '"' + s.replace(re, (c) => esc[c] || "\\u" + c.charCodeAt(0).toString(16).padStart(4, "0")) + '"';
}

// json.dumps with Python's output: ", " and ": " separators (or "," with an indent), \uXXXX
// escapes for non-ASCII text by default, float repr for numbers, NaN/Infinity allowed.
function $jsonDumps(obj, indent, separators, sortKeys, ensureAscii, allowNan, dflt) {
  const plain = indent === null || indent === undefined;
  const [itemSep, keySep] = separators !== null && separators !== undefined ? [...$iter(separators)]
    : plain ? [", ", ": "] : [",", ": "];
  const unit = plain ? null : typeof indent === "number" ? " ".repeat(Math.max(0, indent)) : String(indent);
  const open = new Set();
  const enc = (v, level) => {
    if (v === null || v === undefined) return "null";
    if (v === true) return "true";
    if (v === false) return "false";
    if (typeof v === "number") {
      if (Number.isFinite(v)) return $numRepr(v);
      if (!$t(allowNan)) throw $b.ValueError(`Out of range float values are not JSON compliant: ${$numRepr(v)}`);
      return Number.isNaN(v) ? "NaN" : v > 0 ? "Infinity" : "-Infinity";
    }
    if (typeof v === "string") return $jsonString(v, ensureAscii);
    if (Array.isArray(v) || $isPlain(v)) {
      if (open.has(v)) throw $b.ValueError("Circular reference detected");
      open.add(v);
      const nl = unit === null ? "" : "\n" + unit.repeat(level + 1);
      const end = unit === null ? "" : "\n" + unit.repeat(level);
      let body;
      if (Array.isArray(v)) {
        body = v.length ? "[" + nl + v.map((x) => enc(x, level + 1)).join(itemSep + nl) + end + "]" : "[]";
      } else {
        const keys = Object.keys(v);
        if ($t(sortKeys)) keys.sort($strcmp);
        body = keys.length
          ? "{" + nl + keys.map((k) => $jsonString(k, ensureAscii) + keySep + enc(v[k], level + 1)).join(itemSep + nl) + end + "}"
          : "{}";
      }
      open.delete(v);
      return body;
    }
    if (typeof dflt === "function") return enc(dflt(v), level);
    throw $b.TypeError(`Object of type ${$typename(v)} is not JSON serializable`);
  };
  return enc(obj, 0);
}

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
    dumps: $fn(
      (obj, skipkeys, ensureAscii = true, checkCircular, allowNan = true, cls, indent = null, separators = null,
        dflt = null, sortKeys = false) => $jsonDumps(obj, indent, separators, sortKeys, ensureAscii, allowNan, dflt),
      ["obj"], null,
      ["skipkeys", "ensure_ascii", "check_circular", "allow_nan", "cls", "indent", "separators", "default", "sort_keys"]
    ),
    loads: $fn(function loads(s) {
      if (typeof s !== "string") throw $b.TypeError(`the JSON object must be str, bytes or bytearray, not ${$typename(s)}`);
      try {
        return JSON.parse(s);
      } catch (err) {
        if (!(err instanceof SyntaxError)) throw err;
        const exc = $JSONDecodeError(err.message);
        exc.msg = err.message;
        exc.doc = s;
        throw exc; // a ValueError, as in Python, so `except ValueError` catches it
      }
    }, ["s"]),
    JSONDecodeError: $JSONDecodeError,
  },
  time: { time: () => Date.now() / 1000 },
};
