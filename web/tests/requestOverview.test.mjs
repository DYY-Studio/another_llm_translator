import assert from "node:assert/strict";
import test from "node:test";
import { parseRequestOverview } from "../src/requestOverview.ts";
const snapshot = (input, records) => ({request_kind: "llm", input, segment_id_map: {"1": "SEG-1", "2": "SEG-2"}, response_content: records.map(JSON.stringify).join("\n")});
test("pairs segments by ID, never output order, and flags duplicates", () => {
  const input = {segments: [{id:"1", source:"one"}, {id:"2", source:"two", current_text:"old"}]};
  const value = parseRequestOverview(snapshot(input, [{type:"segment",id:"2", status:"suggested",suggested_text:"new"}, {type:"segment",id:"1",translation:"一"}, {type:"end"}]));
  assert.equal(value.pairs[0].result.translation, "一");
  assert.equal(value.pairs[1].result.suggested_text, "new");
  assert.equal(value.pairs[0].segmentId, "SEG-1");
  const duplicate = parseRequestOverview(snapshot(input, [{type:"segment",id:"1",translation:"a"}, {type:"segment",id:"1",translation:"b"}, {type:"end"}]));
  assert.equal(duplicate.pairs[0].result, null);
  assert.ok(duplicate.issues.length);
});
test("combined request uses translation sources once and separates terms and summary", () => {
  const value = parseRequestOverview(snapshot({segments:[{id:"1",source:"Alice"}],source_segments:[{id:"1",text:"Alice"}]}, [{type:"segment",id:"1",translation:"爱丽丝"},{type:"term",source:"Alice",preferred_translation:"爱丽丝"},{type:"summary",refs:["1"],text:"arrived"},{type:"end"}]));
  assert.equal(value.pairs.length,1);
  assert.deepEqual(value.sources,[]);
  assert.equal(value.terms.length,1);
  assert.equal(value.summaries.length,1);
});
test("incomplete JSONL preserves valid records and reports incomplete results", () => {
  const value = parseRequestOverview({request_kind:"llm",input:{source_segments:["Alice"]},response_content:'{"type":"term","source":"Alice"}\n{"type":'});
  assert.equal(value.terms.length,1);
  assert.ok(value.issues.length);
  assert.equal(value.complete,false);
});
