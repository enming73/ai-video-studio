import {test} from 'node:test';
import assert from 'node:assert/strict';
import {validateDraft, accessibleShot, castComplete, frameVideoReady} from './guidedHelpers.js';

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

test('character stage requires confirmed roles and explicit shot cast', () => {
  const roles = {characters:[{id:'a',confirmed:true}],empty_cast_confirmed:false};
  assert.equal(castComplete(true, roles, {shots:[{character_ids:['a']}]}), true);
  assert.equal(castComplete(true, roles, {shots:[{}]}), false);
  assert.equal(castComplete(true, {...roles,characters:[{id:'a',confirmed:false}]}, {shots:[{character_ids:['a']}]}), false);
  assert.equal(castComplete(true, {characters:[],empty_cast_confirmed:true}, {shots:[{character_ids:[]}]}), true);
});

test('video generation requires confirmed frame and selected model', () => {
  assert.equal(frameVideoReady(true, true, {confirmed_frame_id:'f'}, {model:'m'}), true);
  assert.equal(frameVideoReady(true, true, {}, {model:'m'}), false);
  assert.equal(frameVideoReady(true, false, {confirmed_frame_id:'f'}, {model:'m'}), false);
});
