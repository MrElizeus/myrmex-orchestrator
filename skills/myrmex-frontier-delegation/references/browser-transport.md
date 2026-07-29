# Playwright frontier transport

## Required environment

- OpenCode Playwright MCP enabled.
- A persistent browser profile with the frontier service already authenticated.
- Only one active browser process using that profile. A persistent Playwright profile is single-writer; concurrent clients can conflict.
- `myrmex-frontier` allowed to call `playwright_*` tools and denied repository mutation.

## Exchange request

Use `assets/schemas/frontier-exchange.schema.json`.

Minimum fields:

```yaml
schema: myrmex.frontier-exchange/v1
action: send_and_wait
run_id:
request_id:
chat_url:
outbound_message:
timeout_seconds: 900
allow_reload: true
allow_failover: true | false
failover_handoff:
```

## Sending

1. Use `browser_tabs` to select or create the correct tab.
2. Use `browser_navigate` only when needed.
3. Use `browser_snapshot`; locate the current prompt textbox by accessible role/name.
4. Use `browser_type` with `submit: true`.
5. Confirm the unique request ID appears in the latest user turn.

Do not fill credentials. Do not use raw DOM text insertion unless accessibility interaction is unavailable and the exchange explicitly allows the fallback.

## Reading

Use `browser_evaluate` with `assets/dom/latest-assistant-message.js`, or an equivalent selector scoped to `[data-message-author-role="assistant"]`.

Never use:

```javascript
document.body.innerText.includes(...)
```

because prompts and older messages contain the same markers.

A response is acceptable only when:

- it is the newest assistant turn;
- generation has stopped;
- its non-empty text is identical in two consecutive observations;
- it contains the expected request ID;
- its type parses exactly.

## Exact type parsing

Evaluate specific tokens before general tokens. Do not use substring matching where `SUB_OBJECTIVE_COMPLETE` can match `OBJECTIVE_COMPLETE`.

Plan:

```text
<proposed_plan>
request_id: ...
...
</proposed_plan>
```

Other results begin with:

```text
FRONTIER_RESULT
request_id: ...
type: BLOCKING_CLARIFICATION | OBJECTIVE_COMPLETE | SUB_OBJECTIVE_COMPLETE | PARENT_OBJECTIVE_COMPLETE | OBJECTIVE_ALREADY_COMPLETE
```

## Waiting and recovery

Intervals: 5, 15, 30, 45, 60 seconds, then 60 seconds.

At timeout:

1. Save the observation in the transport result.
2. Reload once if allowed.
3. Re-send only if the latest user turns prove the outbound request is absent.
4. If still failed and failover is allowed, create one new chat and send the supplied recovery handoff.
5. Otherwise return `timeout` or `blocked`; do not invent a response.
