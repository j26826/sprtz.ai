/**
 * What of a conversation is on screen.
 *
 * The transcript used to grow for the whole session, and on a competition day
 * that is a screenful of cards above the one being read — every one of them
 * re-rendering on every Firestore write while an analysis runs. The editor is
 * reading the current answer; the rest is history, and history belongs in the
 * session rather than in the viewport.
 *
 * Its own module, with no imports, for the same reason as cards.js: app.js
 * cannot be loaded outside a browser, and the thing this gets wrong is silent.
 * Every card and every click handler addresses `state.msgs[i]`, so a slice
 * that renumbered its messages would wire each button to the wrong one — a
 * Details that opens somebody else's moment, a pager that pages another card.
 */

/**
 * The turn on screen: the last thing asked, and everything answering it.
 *
 * @param {{who: string}[]} msgs   The whole conversation, oldest first.
 * @returns {[object, number][]}   Each message with its index in `msgs`.
 */
export function currentTurn(msgs) {
  const numbered = (msgs || []).map((m, i) => [m, i]);
  for (let i = numbered.length - 1; i >= 0; i -= 1) {
    if (numbered[i][0]?.who === 'you') return numbered.slice(i);
  }
  // Nothing has been asked yet, which is the opener and nothing else.
  return numbered;
}
