const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(path.join(__dirname, "..", "Model.js"), "utf8")
  .replace(/^\.pragma library\s*/, "");
const context = {};
vm.createContext(context);
vm.runInContext(source, context);

const noon = new Date(2026, 9, 28, 12, 0, 0).getTime();
const today = new Date(2026, 9, 28, 0, 1, 0).toISOString();
const yesterday = new Date(2026, 9, 27, 23, 59, 0).toISOString();

assert.strictEqual(context.daysSince(today, noon), 0);
assert.strictEqual(context.daysSince(yesterday, noon), 1);
assert.strictEqual(context.daysSince("not a date", noon), 0);
assert.strictEqual(context.daysSince(new Date(2026, 10, 1).toISOString(), noon), 0);

const one = [{ id: "a", title: "A", since: yesterday }];
const two = [one[0], { id: "b", title: "B", since: today }];
assert.strictEqual(context.barText(one, "", noon), "1");
assert.strictEqual(context.barText(one, "b", noon), "1");
assert.strictEqual(context.barText(two, "", noon), "∞");
assert.strictEqual(context.barText(two, "a", noon), "1");
assert.strictEqual(context.barText(two, "b", noon), "0");
assert.strictEqual(context.barText([], "", noon), "∞");

assert.strictEqual(context.dayLabel(0), "0 days");
assert.strictEqual(context.dayLabel(1), "1 day");
assert.strictEqual(context.dayLabel(2), "2 days");
assert.strictEqual(context.countLabel(1), "1 event");
assert.strictEqual(context.countLabel(3), "3 events");
assert.strictEqual(context.sinceLabel(yesterday), "27 Oct 2026");

assert.strictEqual(context.plain("<img src=x> A & B", 40), "img src=x A  B");
assert.strictEqual(context.plain("A\u202eB\u0001C", 40), "ABC");
assert.strictEqual(context.plain("abcdef", 3), "abc");

console.log("Model.js ok");
