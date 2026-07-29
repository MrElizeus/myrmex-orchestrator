# Playwright protocol

The primary orchestrator does not call Playwright directly. It builds a validated `myrmex.frontier-exchange/v1` and delegates one bounded exchange to `myrmex-frontier`.

The transport follows `browser-transport.md` and must:

- use the existing authenticated Playwright profile/conversation;
- send through accessible UI controls;
- verify the unique request ID in the latest user turn;
- actively wait with real timers;
- isolate only the newest assistant turn with the role-scoped DOM helper or accessibility snapshot;
- require stopped generation and two stable reads;
- return raw assistant text plus transport evidence;
- reload at most once and open at most one failover chat when authorized;
- never edit the repository, call Engram, delegate, or enter credentials.
