import { describe, it } from 'node:test';
import assert from 'node:assert/strict';

import { humanMessage, jobFailure, looksHuman } from '../src/errors.js';

// The keys come back as themselves, so a test says which sentence was chosen.
const t = (key) => key;

describe('what a runtime says is never what an editor reads', () => {
  const machine = [
    'Firebase: Error (auth/too-many-requests).',
    'ConnectError: ',
    "TypeError: 'NoneType' object is not subscriptable",
    'FAILED_PRECONDITION: Current state: UPDATING',
    '502 Bad Gateway',
    'Traceback (most recent call last):\n  File "x.py", line 3',
    '{"error": {"code": 9, "message": "no"}}',
    'at renderDetailsBody (app.js:1620:11)',
    'Error waiting for Updating Service: Error code 9',
    '7 PERMISSION_DENIED: Missing or insufficient permissions.',
  ];
  for (const text of machine) {
    it(`refuses ${JSON.stringify(text.slice(0, 40))}`, () => {
      assert.equal(looksHuman(text), false);
    });
  }

  const human = [
    'This event has already started; it can no longer be rescheduled.',
    'This match has no source video yet. Prepare playback first.',
    'YouTube refused the stored refresh token. Reconnect the channel in Settings.',
    'The event must end after it starts.',
  ];
  for (const text of human) {
    it(`keeps ${JSON.stringify(text.slice(0, 40))}`, () => {
      assert.equal(looksHuman(text), true);
    });
  }

  it('refuses a bare token and a path', () => {
    assert.equal(looksHuman('ECONNRESET'), false);
    assert.equal(looksHuman('/api/jobs/abc/playback'), false);
  });
});


describe('the sentence shown for a failure', () => {
  it('answers a Firebase code with its own sentence', () => {
    const err = Object.assign(new Error('Firebase: Error (auth/too-many-requests).'),
      { code: 'auth/too-many-requests' });
    assert.equal(humanMessage(err, { t }), 'auth.tooMany');
  });

  it('never lets a Firebase message through as a fallback', () => {
    const err = Object.assign(new Error('Firebase: Error (auth/internal-error).'),
      { code: 'auth/internal-error' });
    assert.equal(humanMessage(err, { t, fallback: 'auth.failed' }), 'auth.failed');
  });

  it('quotes a 4xx the API explained, because those are written for this screen', () => {
    const err = Object.assign(new Error('x'),
      { status: 409, detail: 'This event has already started; it can no longer be rescheduled.' });
    assert.match(humanMessage(err, { t }), /already started/);
  });

  it('never quotes infrastructure, whatever status carried it', () => {
    const err = Object.assign(new Error('x'),
      { status: 502, detail: 'upstream connect error or disconnect/reset before headers' });
    assert.equal(humanMessage(err, { t }), 'error.desk');
  });

  it('quotes a 502 whose detail was written for this screen', () => {
    // Publishing answers 502 with YouTube's own refusal, and that sentence is
    // the most useful thing available — it says what to do about it.
    const err = Object.assign(new Error('x'), {
      status: 502,
      detail: 'YouTube refused the stored refresh token. It has been revoked or has '
        + 'expired; reconnect the channel in Settings.',
    });
    assert.match(humanMessage(err, { t }), /reconnect the channel/i);
  });

  it('answers a status it knows with that status sentence', () => {
    assert.equal(humanMessage({ status: 404 }, { t }), 'error.gone');
    assert.equal(humanMessage({ status: 429 }, { t }), 'error.busy');
    assert.equal(humanMessage({ status: 403 }, { t }), 'error.notAllowed');
  });

  it('reads a failed fetch as being offline', () => {
    assert.equal(humanMessage(new TypeError('Failed to fetch'), { t }), 'error.offline');
  });

  it('falls back to what was being attempted', () => {
    assert.equal(humanMessage(new Error('kaboom'), { t, fallback: 'live.editFailed' }),
      'live.editFailed');
  });

  it('survives being handed nothing at all', () => {
    assert.equal(humanMessage(undefined, { t }), 'error.generic');
    assert.equal(humanMessage(null, { t, fallback: 'error.desk' }), 'error.desk');
  });
});


describe('the reason beside a failed job', () => {
  it('keeps a stage that explained itself', () => {
    const reason = 'The analysis stage produced no moments. Re-run it; if it happens again '
      + 'the segment analysis is failing rather than the match being quiet.';
    assert.match(jobFailure(reason, { t }), /produced no moments/);
  });

  it('replaces one that died of an exception', () => {
    assert.equal(jobFailure('ConnectError: ', { t }), 'error.runFailed');
    assert.equal(jobFailure('RuntimeError: gRPC deadline exceeded', { t }), 'error.runFailed');
  });

  it('replaces nothing at all', () => {
    assert.equal(jobFailure('', { t }), 'error.runFailed');
  });
});
