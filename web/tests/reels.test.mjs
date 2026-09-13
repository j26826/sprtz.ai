/**
 * The arithmetic of a reel.
 *
 * Four of these guard failures that are silent on screen. A key that collides
 * puts two different moments in one slot, and across a cross-event reel that
 * is not hypothetical — ids are only unique within a match. A total that
 * disagrees with the cuts misreports what is about to be published. A nudge
 * that inverts an edge produces a cut with no duration, which renders as
 * nothing rather than as an error. And `nextCut` is the one comparison
 * standing between "stop at the end of the reel" and either looping the last
 * cut for ever or dropping it.
 */

import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';

import {
  CROP_ASPECTS, MIN_CUT_MS, cropBand, focusFrom, isPickedIn, matchCount, moveCut,
  MAX_SENT_TAGS, hashLine, hashList, msClock, nextCut, nudge, parsePickKey, pastEnd,
  pickKey, reelLength, splitTags, tagLine, togglePicked, withHashtags,
} from '../src/reels.js';

const cut = (jobId, startMs, endMs) => ({ jobId, momentId: `${jobId}-m`, startMs, endMs });

describe('addressing a picked moment', () => {
  it('carries the match, because ids are only unique within one', () => {
    assert.notEqual(pickKey('job-a', 'm1'), pickKey('job-b', 'm1'));
  });

  it('survives a moment id containing a colon', () => {
    // The delegated click handler splits its own attributes on colons, which
    // is exactly why this separator is not one.
    const back = parsePickKey(pickKey('job-a', 'cdi:gp:m01'));
    assert.deepEqual(back, { jobId: 'job-a', momentId: 'cdi:gp:m01' });
  });

  it('knows what is already picked', () => {
    const pick = [{ jobId: 'job-a', momentId: 'm1' }];
    assert.equal(isPickedIn(pick, 'job-a', 'm1'), true);
    assert.equal(isPickedIn(pick, 'job-b', 'm1'), false);
    assert.equal(isPickedIn([], 'job-a', 'm1'), false);
  });
});

describe('picking and unpicking', () => {
  const a = { jobId: 'j', momentId: 'a' };
  const b = { jobId: 'j', momentId: 'b' };

  it('adds one that is not there', () => {
    assert.deepEqual(togglePicked([], a), [a]);
  });

  it('removes one that is', () => {
    assert.deepEqual(togglePicked([a, b], a), [b]);
  });

  it('does not mutate the list it was given', () => {
    const before = [a];
    togglePicked(before, b);
    assert.equal(before.length, 1);
  });

  it('puts a re-picked moment at the end, since the order is the running order', () => {
    assert.deepEqual(togglePicked(togglePicked([a, b], a), a), [b, a]);
  });

  it('tells the same moment in two matches apart', () => {
    const inA = { jobId: 'job-a', momentId: 'm1' };
    const inB = { jobId: 'job-b', momentId: 'm1' };
    assert.equal(togglePicked([inA], inB).length, 2);
  });
});

describe('how long a reel runs', () => {
  it('is the sum of the cuts, not the span they cover', () => {
    // Two 5s cuts an hour apart are ten seconds of video, not an hour.
    assert.equal(reelLength([cut('j', 0, 5000), cut('j', 3600000, 3605000)]), 10000);
  });

  it('is zero for no cuts', () => {
    assert.equal(reelLength([]), 0);
    assert.equal(reelLength(undefined), 0);
  });

  it('never counts an inverted cut as negative time', () => {
    assert.equal(reelLength([cut('j', 5000, 1000)]), 0);
  });

  it('counts the matches a reel draws on', () => {
    assert.equal(matchCount([cut('a', 0, 1), cut('b', 0, 1), cut('a', 2, 3)]), 2);
    assert.equal(matchCount([]), 0);
  });
});

describe('reading a cut point', () => {
  it('shows the milliseconds, because that is what a nudge moves', () => {
    assert.equal(msClock(83900), '1:23.900');
  });

  it('pads so the digits line up down a column', () => {
    assert.equal(msClock(61001), '1:01.001');
    assert.equal(msClock(0), '0:00.000');
  });

  it('does not go negative', () => {
    assert.equal(msClock(-500), '0:00.000');
  });

  it('keeps counting minutes past an hour rather than wrapping', () => {
    // A competition day is eight hours long; 1:05:00 shown as 5:00 would be
    // a cut point two hours from where it is.
    assert.equal(msClock(3900000), '65:00.000');
  });
});

describe('nudging an edge', () => {
  const c = cut('j', 10000, 20000);

  it('moves the edge asked for and leaves the other alone', () => {
    assert.deepEqual(nudge(c, 'start', -100).startMs, 9900);
    assert.deepEqual(nudge(c, 'start', -100).endMs, 20000);
    assert.deepEqual(nudge(c, 'end', 100).endMs, 20100);
  });

  it('clamps at zero rather than going before the recording started', () => {
    assert.equal(nudge(cut('j', 200, 5000), 'start', -1000).startMs, 0);
  });

  it('stops rather than inverting when an edge is pushed past the other', () => {
    // A control held at its limit should stop, not start failing.
    assert.equal(nudge(c, 'start', 999999).startMs, 20000 - MIN_CUT_MS);
    assert.equal(nudge(c, 'end', -999999).endMs, 10000 + MIN_CUT_MS);
  });

  it('never produces a cut with no duration', () => {
    const squashed = nudge(nudge(c, 'start', 999999), 'end', -999999);
    assert.ok(squashed.endMs - squashed.startMs >= MIN_CUT_MS);
  });
});

describe('reordering', () => {
  const cuts = [cut('a', 0, 1), cut('b', 0, 1), cut('c', 0, 1)];

  it('swaps with the neighbour', () => {
    assert.deepEqual(moveCut(cuts, 0, 1).map((x) => x.jobId), ['b', 'a', 'c']);
    assert.deepEqual(moveCut(cuts, 2, -1).map((x) => x.jobId), ['a', 'c', 'b']);
  });

  it('is a no-op off either end rather than an error', () => {
    assert.deepEqual(moveCut(cuts, 0, -1).map((x) => x.jobId), ['a', 'b', 'c']);
    assert.deepEqual(moveCut(cuts, 2, 1).map((x) => x.jobId), ['a', 'b', 'c']);
  });

  it('does not mutate the list it was given', () => {
    const before = [...cuts];
    moveCut(before, 0, 1);
    assert.deepEqual(before.map((x) => x.jobId), ['a', 'b', 'c']);
  });
});

describe('running from one cut to the next', () => {
  const cuts = [cut('j', 1000, 2000), cut('j', 9000, 9500)];

  it('advances while there is another cut', () => {
    assert.equal(nextCut(cuts, 0), 1);
  });

  it('stops at the last one rather than looping it', () => {
    assert.equal(nextCut(cuts, 1), null);
  });

  it('knows when the playhead has run past the cut it is in', () => {
    // Seconds in, because that is what a video element reports; compared in
    // milliseconds, because that is what a cut is stored in.
    assert.equal(pastEnd(cuts[0], 1.999), false);
    assert.equal(pastEnd(cuts[0], 2.0), true);
    assert.equal(pastEnd(null, 99), false);
  });
});

describe('the crop window drawn on the frame', () => {
  it('is the same fraction the encoder will actually cut', () => {
    // The guide and the crop are two copies of one sum. If they drift, the
    // preview lies about its own output — 9:16 out of 16:9 is (9/16)/(16/9).
    assert.ok(Math.abs(cropBand('9:16').width - 0.3164) < 0.001);
    assert.ok(Math.abs(cropBand('4:5').width - 0.45) < 0.001);
    assert.ok(Math.abs(cropBand('1:1').width - 0.5625) < 0.001);
  });

  it('centres by default', () => {
    const b = cropBand('9:16');
    assert.ok(Math.abs(b.left - (1 - b.width) / 2) < 1e-9);
  });

  it('never leaves the frame at either extreme', () => {
    for (const a of Object.keys(CROP_ASPECTS)) {
      for (const f of [0, 0.25, 0.5, 0.75, 1]) {
        const b = cropBand(a, f);
        assert.ok(b.left >= -1e-9, `${a}@${f} left`);
        assert.ok(b.left + b.width <= 1 + 1e-9, `${a}@${f} right`);
      }
    }
  });

  it('brings an out-of-range focus back rather than drawing off the frame', () => {
    assert.equal(cropBand('9:16', -3).left, 0);
    assert.ok(Math.abs(cropBand('9:16', 9).left - (1 - cropBand('9:16').width)) < 1e-9);
  });

  it('is null for a shape it does not know', () => {
    assert.equal(cropBand('21:9'), null);
  });

  it('turns a pointer position into the focus that centres the window there', () => {
    // Dragging to the middle must mean the middle, or the guide jumps away
    // from the cursor on the first move.
    assert.ok(Math.abs(focusFrom(0.5, '9:16') - 0.5) < 1e-9);
    assert.equal(focusFrom(0, '9:16'), 0);
    assert.equal(focusFrom(1, '9:16'), 1);
  });

  it('round-trips: a focus drawn, then read back from its own centre', () => {
    for (const f of [0, 0.2, 0.5, 0.8, 1]) {
      const b = cropBand('4:5', f);
      assert.ok(Math.abs(focusFrom(b.left + b.width / 2, '4:5') - f) < 1e-9);
    }
  });
});

describe('the copy that goes out with a reel', () => {
  it('puts hashtags in the description, where YouTube reads them', () => {
    // Sent as their own field they are silently dropped, which loses half the
    // reach someone was counting on and says nothing about it.
    const out = withHashtags('Two rounds worth watching.', ['Dressage', 'Falkenstein']);
    assert.ok(out.startsWith('Two rounds worth watching.'));
    assert.ok(out.includes('#Dressage #Falkenstein'));
  });

  it('accepts either a typed line or a list, since the field holds one and the writer returns the other', () => {
    assert.equal(withHashtags('x', '#a #b'), withHashtags('x', ['a', 'b']));
    assert.equal(withHashtags('x', 'a, b'), withHashtags('x', ['a', 'b']));
  });

  it('does not double the hash someone already typed', () => {
    assert.ok(!withHashtags('x', ['##Dressage']).includes('##'));
  });

  it('drops a repeat however it was capitalised', () => {
    assert.equal(withHashtags('x', ['Dressage', 'dressage']).match(/#/g).length, 1);
  });

  it('leaves the description alone when there are none', () => {
    assert.equal(withHashtags('Just this.', []), 'Just this.');
    assert.equal(withHashtags('Just this.', ''), 'Just this.');
  });

  it('survives an empty description without leading blank lines', () => {
    assert.equal(withHashtags('', ['a']), '#a');
  });

  it('splits a typed keyword line on commas or newlines', () => {
    assert.deepEqual(splitTags('Dressage, Falkenstein\nPirouette'),
      ['Dressage', 'Falkenstein', 'Pirouette']);
  });

  it('drops blanks and repeats rather than sending them', () => {
    assert.deepEqual(splitTags('a, , a, A , b'), ['a', 'b']);
  });

  it('stops at what YouTube will take', () => {
    const many = Array.from({ length: 40 }, (_, i) => `tag${i}`).join(', ');
    assert.equal(splitTags(many).length, MAX_SENT_TAGS);
  });

  it('accepts a list as readily as a line', () => {
    assert.deepEqual(splitTags(['a', 'b']), ['a', 'b']);
    assert.deepEqual(splitTags(null), []);
  });
});

describe('a field whose value is a list until someone types in it', () => {
  // This is the shape of a bug that already happened. The copy writer fills
  // these with lists; the first keystroke replaces the list with a string.
  // Rendering assumed the list, so the panel threw mid-render — and because
  // it threw during a re-render, the symptom was every later button doing
  // nothing, which points nowhere near a keywords field.
  it('shows a list as a line', () => {
    assert.equal(tagLine(['Dressage', 'Falkenstein']), 'Dressage, Falkenstein');
    assert.equal(hashLine(['Dressage', 'Falkenstein']), '#Dressage #Falkenstein');
  });

  it('shows typed text unchanged, rather than throwing on it', () => {
    assert.equal(tagLine('Dressage, half typed'), 'Dressage, half typed');
    assert.equal(hashLine('#Dressage #half'), '#Dressage #half');
  });

  it('survives nothing at all', () => {
    for (const empty of [undefined, null, '', []]) {
      assert.equal(tagLine(empty), '');
      assert.equal(hashLine(empty), '');
      assert.deepEqual(hashList(empty), []);
    }
  });

  it('does not double a hash already in the list', () => {
    assert.equal(hashLine(['#Dressage']), '#Dressage');
  });

  it('reads hashtags back out of either shape', () => {
    assert.deepEqual(hashList('#a #b'), ['a', 'b']);
    assert.deepEqual(hashList(['a', 'b']), ['a', 'b']);
    assert.deepEqual(hashList('a, a, A'), ['a']);
  });
});
