import {test} from 'node:test';
import assert from 'node:assert/strict';
import {validateDraft, shotReady, castComplete, frameVideoReady} from './guidedHelpers.js';

test('draft must match total and allowed shot durations', () => {
  const draft = {shots: [{prompt:'A forest scene', duration:8}, {prompt:'A river scene', duration:12}]};
  assert.equal(validateDraft(draft, 20, [8,12]), '');
  assert.notEqual(validateDraft(draft, 21, [8,12]), '');
  assert.notEqual(validateDraft(draft, 20, [10]), '');
});

test('a shot is ready when both shared boundaries are confirmed', () => {
  const boundaries = [{id:'b0',confirmed:true},{id:'b1',confirmed:false},{id:'b2',confirmed:true}];
  const shot = {start_boundary_id:'b0', end_boundary_id:'b1'};
  assert.equal(shotReady(shot, boundaries), false);
  assert.equal(shotReady({...shot, end_boundary_id:'b2'}, boundaries), true);
});

test('a shot is ready without a boundary stage', () => {
  assert.equal(shotReady({}, null), true);
});

test('character stage requires confirmed roles and explicit shot cast', () => {
  const roles = {characters:[{id:'a',confirmed:true}],empty_cast_confirmed:false};
  assert.equal(castComplete(true, roles, {shots:[{character_ids:['a']}]}), true);
  assert.equal(castComplete(true, roles, {shots:[{}]}), false);
  assert.equal(castComplete(true, {...roles,characters:[{id:'a',confirmed:false}]}, {shots:[{character_ids:['a']}]}), false);
  assert.equal(castComplete(true, {characters:[],empty_cast_confirmed:true}, {shots:[{character_ids:[]}]}), true);
});

test('video generation requires both boundaries confirmed and selected model', () => {
  const boundaries = [{id:'b0',confirmed:true},{id:'b1',confirmed:true}];
  const shot = {start_boundary_id:'b0', end_boundary_id:'b1'};
  assert.equal(frameVideoReady(true, true, shot, {model:'m'}, boundaries), true);
  assert.equal(frameVideoReady(true, true, {...shot, end_boundary_id:'b1x'}, {model:'m'}, boundaries), false);
  assert.equal(frameVideoReady(true, true, shot, null, boundaries), false);
  assert.equal(frameVideoReady(true, false, shot, {model:'m'}, boundaries), false);
});
