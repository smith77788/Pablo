var K;
function mk() {
  function* g(self) { K = arguments; self = yield 1; yield 2; }
  var it = g(0);
  it.next();
  it.next(it);   // self = it: generator kept alive only by its own frame
}
mk();
var junk = [];
for (var i = 0; i < 200000; i++) junk.push({a: i, b: [i]});  // trigger GC
var o = K[0];  // dangling reference to the (possibly) freed generator
print("no crash, o = " + o);
