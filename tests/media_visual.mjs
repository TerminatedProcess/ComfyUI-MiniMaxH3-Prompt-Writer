import test from 'node:test';
import assert from 'node:assert/strict';
import {mediaVisualDescriptor, referenceCaption, referenceLabel} from '../web/media_visual.js';
test('Composer consumes staged visual representations without inventing reference identity',()=>{
  const asset={type:'video',status:'needs_edit',reference:null,contact_sheet_url:'sheet',contact_sheet_width:800,contact_sheet_height:600};
  assert.equal(mediaVisualDescriptor(asset).src,'sheet');
  assert.equal(referenceCaption(asset),'');
  assert.equal(referenceCaption({...asset,reference:'<Video 2>'}),'<Video 2>');
  assert.equal(mediaVisualDescriptor({...asset,contact_sheet_url:null}),null);
});

test('The studio shows S1, the prompt still carries <Picture 1>', () => {
  // Display only. <Picture 1> is the official H3 tag -- prompt_audit rejects a
  // prompt that drops it -- and the number is the SAME number, so the label
  // cannot drift from the tag when media is added or removed.
  assert.equal(referenceLabel('<Picture 1>'), 'S1');
  assert.equal(referenceLabel('<Picture 12>'), 'S12');
  assert.equal(referenceLabel('<Video 2>'), 'V2');
  assert.equal(referenceLabel('<Audio 3>'), 'A3');
  // Frame roles and anything unrecognised are passed through untouched.
  assert.equal(referenceLabel('First frame'), 'First frame');
  assert.equal(referenceLabel(''), '');
  assert.equal(referenceLabel(null), '');
});
