(() => {
  const normalize = (value) => (value || "").replace(/\u00a0/g, " ").trim();

  const assistantNodes = Array.from(
    document.querySelectorAll('[data-message-author-role="assistant"]')
  ).filter((node) => normalize(node.innerText).length > 0);

  const node = assistantNodes.length ? assistantNodes[assistantNodes.length - 1] : null;
  const turn = node?.closest('[data-testid^="conversation-turn-"]')
    || node?.closest('[data-message-id]')
    || node?.closest('article')
    || node;

  const stopSelectors = [
    'button[data-testid="stop-button"]',
    'button[aria-label*="Stop"]',
    'button[aria-label*="stop"]',
    'button[aria-label*="Detener"]',
    'button[aria-label*="detener"]'
  ];

  const isGenerating = stopSelectors.some((selector) =>
    Array.from(document.querySelectorAll(selector)).some((button) => {
      const style = window.getComputedStyle(button);
      return style.display !== 'none' && style.visibility !== 'hidden';
    })
  );

  const messageId = turn?.getAttribute?.('data-message-id')
    || turn?.getAttribute?.('data-testid')
    || node?.getAttribute?.('data-message-id')
    || null;

  const text = normalize(node?.innerText);
  const requestIdMatch = text.match(/^request_id:\s*([^\s]+)$/mi);
  const resultTypeMatch = text.match(/^type:\s*(BLOCKING_CLARIFICATION|OBJECTIVE_COMPLETE|SUB_OBJECTIVE_COMPLETE|PARENT_OBJECTIVE_COMPLETE|OBJECTIVE_ALREADY_COMPLETE)$/mi);
  const hasPlanStart = /^<proposed_plan>\s*$/mi.test(text);
  const hasPlanEnd = /^<\/proposed_plan>\s*$/mi.test(text);

  let responseType = 'unknown';
  if (hasPlanStart && hasPlanEnd) responseType = 'plan';
  else if (resultTypeMatch) {
    const map = {
      BLOCKING_CLARIFICATION: 'blocking_clarification',
      OBJECTIVE_COMPLETE: 'objective_complete',
      SUB_OBJECTIVE_COMPLETE: 'sub_objective_complete',
      PARENT_OBJECTIVE_COMPLETE: 'parent_objective_complete',
      OBJECTIVE_ALREADY_COMPLETE: 'already_complete'
    };
    responseType = map[resultTypeMatch[1]] || 'unknown';
  }

  return {
    found: Boolean(node),
    messageId,
    text,
    textLength: text.length,
    isGenerating,
    requestId: requestIdMatch?.[1] || null,
    responseType,
    hasPlanStart,
    hasPlanEnd,
    url: location.href,
    title: document.title
  };
})()
