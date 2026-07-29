#!/usr/bin/env node
"use strict";

const fs = require("fs");
const vm = require("vm");
const path = require("path");

const root = path.resolve(__dirname, "..");
const source = fs.readFileSync(
  path.join(root, "skills/myrmex-frontier-delegation/assets/dom/latest-assistant-message.js"),
  "utf8"
);

function makeNode(text, id) {
  const turn = {
    getAttribute(name) {
      if (name === "data-message-id") return id;
      if (name === "data-testid") return `conversation-turn-${id}`;
      return null;
    },
  };
  return {
    innerText: text,
    closest() { return turn; },
    getAttribute() { return null; },
  };
}

function evaluate(messages, generating = false) {
  const stop = { visible: generating };
  const document = {
    title: "Frontier test",
    querySelectorAll(selector) {
      if (selector === '[data-message-author-role="assistant"]') return messages;
      if (selector.startsWith("button") || selector.includes("stop-button")) return generating ? [stop] : [];
      return [];
    },
  };
  const context = {
    document,
    location: { href: "https://chatgpt.com/c/test" },
    window: {
      getComputedStyle(node) {
        return node.visible ? { display: "block", visibility: "visible" } : { display: "none", visibility: "hidden" };
      },
    },
  };
  return vm.runInNewContext(source, context);
}

let result = evaluate([
  makeNode("FRONTIER_RESULT\nrequest_id: old\ntype: OBJECTIVE_COMPLETE", "old"),
  makeNode("<proposed_plan>\nrequest_id: req-123\nbase_sha: abc123\n</proposed_plan>", "new"),
]);
if (result.messageId !== "new" || result.requestId !== "req-123" || result.responseType !== "plan") {
  throw new Error(`latest plan extraction failed: ${JSON.stringify(result)}`);
}

result = evaluate([
  makeNode("FRONTIER_RESULT\nrequest_id: req-456\ntype: SUB_OBJECTIVE_COMPLETE", "sub"),
]);
if (result.responseType !== "sub_objective_complete") {
  throw new Error(`exact completion parsing failed: ${JSON.stringify(result)}`);
}

result = evaluate([
  makeNode("FRONTIER_RESULT\nrequest_id: req-789\ntype: PARENT_OBJECTIVE_COMPLETE", "parent"),
], true);
if (!result.isGenerating || result.responseType !== "parent_objective_complete") {
  throw new Error(`generation/type extraction failed: ${JSON.stringify(result)}`);
}

console.log("frontier DOM helper test: PASS");
