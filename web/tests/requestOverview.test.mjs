import assert from "node:assert/strict";
import test from "node:test";
import { parseRequestOverview, parseRepetitionOverview, parseRepetitionOverviews } from "../src/requestOverview.ts";
const snapshot = (input, records) => ({request_kind: "llm", input, segment_id_map: {"1": "SEG-1", "2": "SEG-2"}, response_content: records.map(JSON.stringify).join("\n")});
test("repetition overview resolves shared translations and context by occurrence ID", () => {
  const input = {source: "promise", files: {"2": "chapter.txt"}, translations: ["come", "go"], context: {"1": "before", "2": "after"},
    occurrences: [{id: "1", file: 2, part: "document", position: 4, variant: 0, previous: ["1"], next: ["2"]},
      {id: "2", file: 2, part: "document", position: 9, variant: 1}, {id: "3", variant: 1}]};
  const value = parseRepetitionOverview({input, response_content: JSON.stringify({groups: [{members: ["2", "1"], target: "1", apply: ["2"], reason: "same event"}], keep: [], uncertain: ["3"]})});
  assert.equal(value.invalid, false);
  assert.deepEqual(value.groups, [{members: ["2", "1"], reason: "same event"}]);
  assert.equal(value.occurrences[1].result, "come");
  assert.equal(value.occurrences[0].result, null);
  assert.equal(value.occurrences[0].kept, true);
  assert.equal(value.occurrences[0].filename, "chapter.txt");
  assert.deepEqual(value.occurrences[0].previous, ["before"]);
  assert.deepEqual(value.occurrences[0].next, ["after"]);
  assert.equal(value.occurrences[2].uncertain, true);
  assert.equal(parseRepetitionOverview({input}).occurrences[0].result, null);
  assert.equal(parseRepetitionOverview({input, response_content: "invalid"}).invalid, true);
});
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
test("pairs entity explanations by entity and resolves only their own citations", () => {
  const input = {entities: [
    {entity:"alice",members:["alice","ally"],current_items:[{text:"旧说明"}],fragments:[{id:"K1",text:"船长"}]},
    {entity:"bob",members:["bob"],current_items:[],fragments:[{id:"K2",text:"船员"}]},
  ]};
  const alice = {type:"entity_knowledge",entity:"alice",items:[{text:"船长",refs:["K1"]}],reason:"整理"};
  const bob = {type:"entity_knowledge",entity:"bob",items:[],reason:"无有效说明"};
  const value = parseRequestOverview(snapshot(input,[bob,alice,{type:"end"}]));
  assert.equal(value.entities[0].result.items[0].text,"船长");
  assert.equal(value.entities[0].fragments[0].id,"K1");
  assert.deepEqual(value.entities[1].result.items,[]);
  assert.deepEqual(value.issues,[]);
  for (const output of [[alice,alice,bob], [alice], [alice,bob,{...bob,entity:"unknown"}],
    [{...alice,items:[{text:"错误引用",refs:["K2"]}]},bob],
    [{...alice,items:[{text:"错误格式",refs:"K1"}]},bob]]) {
    const bad = parseRequestOverview(snapshot(input,[...output,{type:"end"}]));
    assert.ok(bad.issues.length);
  }
  const pending = parseRequestOverview({request_kind:"llm",input});
  assert.equal(pending.entities[0].result,null);
  assert.deepEqual(pending.issues,[]);
});

test("batched repetition overview scopes occurrence IDs and shares context", () => {
  const occurrence = (id, variant) => ({id, file: 1, part: "document", position: 0, variant, previous: ["1"]});
  const input = {files: {"1": "chapter.txt"}, context: {"1": "shared context"}, repetitions: [
    {id: "a", source: "first", translations: ["A", "B"], occurrences: [occurrence("1", 0), occurrence("2", 1)]},
    {id: "b", source: "second", translations: ["C", "D"], occurrences: [occurrence("1", 0), occurrence("2", 1)]},
  ]};
  const result = id => ({id, groups: [{members: ["1", "2"], target: "1", apply: ["2"], reason: id}], keep: [], uncertain: []});
  const values = parseRepetitionOverviews({input, response_content: JSON.stringify({repetitions: [result("b"), result("a")]})});
  assert.deepEqual(values.map(value => value.id), ["a", "b"]);
  assert.equal(values[0].occurrences[1].result, "A");
  assert.equal(values[1].occurrences[1].result, "C");
  assert.equal(values[1].occurrences[0].kept, true);
  assert.deepEqual(values[1].occurrences[0].previous, ["shared context"]);
  assert.equal(values[1].occurrences[0].filename, "chapter.txt");
  const missing = parseRepetitionOverviews({input, response_content: JSON.stringify({repetitions: [result("a")]})});
  assert.equal(missing[0].invalid, false);
  assert.equal(missing[1].invalid, true);
  assert.equal(missing[1].occurrences[1].result, null);
  assert.equal(parseRepetitionOverviews({input, response_content: "invalid"})[0].invalid, true);
});
