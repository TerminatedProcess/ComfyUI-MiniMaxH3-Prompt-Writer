import test from 'node:test';
import assert from 'node:assert/strict';
import {
  MAX_SUBJECTS,
  assetsForSubject,
  canJoinSubject,
  nextSubjectIndex,
  openBoxesAfterMove,
  remappedNames,
  reorderedGroups,
  subjectTone,
  subjectIndices,
  subjectLetter,
  unassignedAssets,
} from '../web/media_subjects.js';

const picture = (id, subject = null) => ({ id, type: 'image', subject });

test('Boxes come from the pictures in them, plus the empty ones the user opened', () => {
  const assets = [picture('a', 1), picture('b', 1), picture('c', 3), picture('d')];
  assert.deepEqual(subjectIndices(assets), [1, 3]);
  // An empty box exists only in memory until a picture lands in it.
  assert.deepEqual(subjectIndices(assets, [2]), [1, 2, 3]);
  assert.deepEqual(subjectIndices([], []), []);
});

test('Letters follow position, so deleting the middle box does not renumber anything', () => {
  const indices = subjectIndices([picture('a', 1), picture('b', 3)]);
  assert.equal(subjectLetter(indices, 1), 'A');
  assert.equal(subjectLetter(indices, 3), 'B');
  assert.equal(subjectLetter(indices, 2), '');
});

test('Add subject fills the lowest free slot and stops at the ceiling', () => {
  assert.equal(nextSubjectIndex([]), 1);
  assert.equal(nextSubjectIndex([1, 3]), 2);
  assert.equal(nextSubjectIndex([1, 2, 3, 4, 5, 6]), null);
  assert.equal(MAX_SUBJECTS, 6);
});

test('A picture belongs to one box, or to the scene', () => {
  const assets = [picture('a', 1), picture('b', 2), picture('c')];
  assert.deepEqual(assetsForSubject(assets, 1).map((item) => item.id), ['a']);
  assert.deepEqual(unassignedAssets(assets).map((item) => item.id), ['c']);
});

test('A stored subject outside the ceiling reads as ungrouped rather than drawing a seventh box', () => {
  const assets = [picture('a', 9), picture('b', 0), picture('c', 1.5)];
  assert.deepEqual(subjectIndices(assets), []);
  assert.deepEqual(unassignedAssets(assets).map((item) => item.id), ['a', 'b', 'c']);
});

test('Only a picture can define a person: video and audio stay scene references', () => {
  assert.equal(canJoinSubject({ type: 'image' }), true);
  assert.equal(canJoinSubject({ type: 'video' }), false);
  assert.equal(canJoinSubject({ type: 'audio' }), false);
  assert.equal(canJoinSubject(null), false);
});

test('An empty box never takes a letter, so the studio and the prompt agree on who is who', () => {
  // Boxes 1 and 3 filled, box 2 opened and still empty. The writer is told
  // about groups, and an empty box is not one -- lettering it would have the
  // studio calling a person Subject C while the document called her B.
  const assets = [picture('a', 1), picture('b', 3)];
  const lettered = subjectIndices(assets);
  assert.deepEqual(subjectIndices(assets, [2]), [1, 2, 3]);
  assert.equal(subjectLetter(lettered, 1), 'A');
  assert.equal(subjectLetter(lettered, 3), 'B');
  assert.equal(subjectLetter(lettered, 2), '');
});

test('Dragging the last picture out of a box leaves the box open', () => {
  // Boxes 1, 2, 3 with one picture each; box 2's picture goes back to the
  // tray. Closing box 2 here would turn Subject C into Subject B mid-task,
  // and the user was re-sorting, not closing anything.
  const after = [picture('a', 1), picture('b', null), picture('c', 3)];
  const open = openBoxesAfterMove([], { assets: after, into: null, from: 2 });
  assert.deepEqual(open, [2]);
  assert.deepEqual(subjectIndices(after, open), [1, 2, 3]);
});

test('A box a picture lands in stops being an empty one', () => {
  const after = [picture('a', 2)];
  assert.deepEqual(openBoxesAfterMove([2], { assets: after, into: 2, from: null }), []);
});

test('A box that still holds pictures is not reopened as empty', () => {
  const after = [picture('a', 1), picture('b', 1)];
  assert.deepEqual(openBoxesAfterMove([], { assets: after, into: null, from: 1 }), []);
});

test('The remove button closes the box rather than keeping it open', () => {
  const after = [picture('a', null)];
  assert.deepEqual(openBoxesAfterMove([2], { assets: after, into: null, from: null }), [2]);
  assert.deepEqual(openBoxesAfterMove([2], { assets: after, into: 2, from: null }), []);
});

test('Colours follow box position, so Subject A always wears the first one', () => {
  const indices = subjectIndices([picture('a', 1), picture('b', 3)]);
  assert.equal(subjectTone(indices, 1), 'subject-1');
  assert.equal(subjectTone(indices, 3), 'subject-2');
  assert.equal(subjectTone(indices, 2), '');
});

test('Reordering boxes produces the whole grouping, in the new order', () => {
  // Box order is person order, so this has to be sent as the complete desired
  // state -- applied as single moves, two boxes would briefly share an index
  // and merge into one person.
  const assets = [picture('a', 1), picture('b', 2), picture('c', 2), picture('d', 3)];
  const indices = subjectIndices(assets);
  assert.deepEqual(reorderedGroups(assets, indices, 3, 1), [['d'], ['a'], ['b', 'c']]);
  assert.deepEqual(reorderedGroups(assets, indices, 1, 3), [['b', 'c'], ['d'], ['a']]);
});

test('A box reorder that changes nothing is not sent', () => {
  const assets = [picture('a', 1), picture('b', 2)];
  const indices = subjectIndices(assets);
  assert.equal(reorderedGroups(assets, indices, 1, 1), null);
  assert.equal(reorderedGroups(assets, indices, 1, 9), null);
  assert.equal(reorderedGroups(assets, indices, null, 2), null);
});

test('An empty box is not part of the order it cannot be written to', () => {
  // Only filled boxes exist on the server, so an empty one between two filled
  // ones must not take a slot in the grouping that gets sent.
  const assets = [picture('a', 1), picture('b', 3)];
  assert.deepEqual(reorderedGroups(assets, subjectIndices(assets, [2]), 3, 1), [['b'], ['a']]);
});

test('A name follows its box when the boxes are reordered', () => {
  // Liz is box 1, Bob is box 2. Drag Bob above Liz. Without this, the box now
  // holding Bob's pictures is labelled Liz -- and the next build describes him
  // by her name, which is the exact failure subjects exist to prevent.
  const names = { 1: 'Liz', 2: 'Bob' };
  assert.deepEqual(remappedNames([1, 2], [2, 1], names), { 1: 'Bob', 2: 'Liz' });
});

test('Releasing a box takes its name with it and shifts the rest up', () => {
  const names = { 1: 'Liz', 2: 'Bob', 3: 'Ada' };
  assert.deepEqual(remappedNames([1, 2, 3], [2, 3], names), { 1: 'Bob', 2: 'Ada' });
  assert.deepEqual(remappedNames([1, 2, 3], [1, 3], names), { 1: 'Liz', 2: 'Ada' });
});

test('A new box starts without a name instead of inheriting one', () => {
  assert.deepEqual(remappedNames([1], [1, 4], { 1: 'Liz' }), { 1: 'Liz' });
  assert.deepEqual(remappedNames([], [1], {}), {});
});

test('Unnamed people do not leave blanks in the roll', () => {
  assert.deepEqual(remappedNames([1, 2], [2, 1], { 2: 'Bob' }), { 1: 'Bob' });
  assert.deepEqual(remappedNames([1, 2], [2, 1], { 1: '   ' }), {});
});
