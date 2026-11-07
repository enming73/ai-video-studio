import {test} from 'node:test';
import assert from 'node:assert/strict';
import {validateDraft, accessibleShot} from './guidedHelpers.js';

test('draft must match total and allowed shot durations', () => {
  const draft = {shots: [{prompt:'A forest scene', duration:8}, {prompt:'A river scene', duration:12}]};
  assert.equal(validateDraft(draft, 20, [8,12]), '');
  assert.notEqual(validateDraft(draft, 21, [8,12]), '');
  assert.notEqual(validateDraft(draft, 20, [10]), '');
});

test('unconfirmed earlier shot locks later unconfirmed shots but not past confirmed shots', () => {
  const shots = [{seq:1,confirmed:false},{seq:2,confirmed:true},{seq:3,confirmed:false}];
  assert.equal(accessibleShot(shots, 2), true);
  assert.equal(accessibleShot(shots, 3), false);
});
