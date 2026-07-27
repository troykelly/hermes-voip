import assert from 'node:assert/strict';
import { partitionWave } from './partition.mjs';
import { evaluateDone, nextDryCycles, isMaterial } from './done-gate.mjs';
let n=0; const ok=(m)=>{n++;console.log('  ok',m)};
const CFG={knownLanes:['api','db','web','i18n','ui'],singleWriterLanes:['api','db'],scopedLanes:['ui'],maxWave:4};

// partition
let r=partitionWave([{number:3,lane:'api',blockedBy:[]},{number:1,lane:'api',blockedBy:[]}],CFG);
assert.deepEqual(r.wave,[1]); assert.match(r.deferred[0].reason,/single-writer/); ok('single-writer lane admits one, lowest number first');

r=partitionWave([{number:1,lane:'web',blockedBy:[]},{number:2,lane:'web',blockedBy:[]},{number:3,lane:'i18n',blockedBy:[]}],CFG);
assert.deepEqual(r.wave,[1,2,3]); ok('unbounded lanes run concurrently');

r=partitionWave([{number:1,lane:'api',blockedBy:[{number:9,satisfied:false}]}],CFG);
assert.equal(r.wave.length,0); assert.match(r.deferred[0].reason,/blocked by unmerged 9/); ok('unmet blocker defers');

r=partitionWave([{number:1,lane:'totally-new',blockedBy:[]},{number:2,lane:'totally-new',blockedBy:[]}],CFG);
assert.deepEqual(r.wave,[1]); ok('unknown lane coerces to single-writer (fail safe)');

r=partitionWave([{number:1,lane:'ui',scope:'a',blockedBy:[]},{number:2,lane:'ui',scope:'b',blockedBy:[]},{number:3,lane:'ui',scope:'a',blockedBy:[]}],CFG);
assert.deepEqual(r.wave,[1,2]); ok('scoped lane: one per scope');

r=partitionWave([{number:1,lane:'ui',blockedBy:[]},{number:2,lane:'ui',blockedBy:[]}],CFG);
assert.deepEqual(r.wave,[1]); ok('scoped lane without scope treated as single-writer');

r=partitionWave([1,2,3,4,5,6].map(i=>({number:i,lane:'web',blockedBy:[]})),CFG);
assert.equal(r.wave.length,4); assert.match(r.deferred[0].reason,/wave cap 4/); ok('wave cap enforced, remainder deferred not dropped');

r=partitionWave('nope',CFG); assert.equal(r.wave.length,0); assert.ok(r.error); ok('non-array input refuses to dispatch');
r=partitionWave([{number:'x',lane:'web'},{number:1,lane:'web',blockedBy:[]}],CFG);
assert.deepEqual(r.wave,[1]); ok('malformed candidate skipped, not crashed');

// done-gate
assert.equal(evaluateDone({buildableOpenIssues:0,openOrchestratorPRs:0,consecutiveDryCycles:2}).done,true); ok('clean + K=2 => DONE');
assert.equal(evaluateDone({buildableOpenIssues:1,openOrchestratorPRs:0,consecutiveDryCycles:9}).done,false); ok('work remaining => NOT done');
assert.equal(evaluateDone({buildableOpenIssues:0,openOrchestratorPRs:1,consecutiveDryCycles:9}).done,false); ok('open PR => NOT done');
assert.equal(evaluateDone({buildableOpenIssues:0,openOrchestratorPRs:0,consecutiveDryCycles:1}).done,false); ok('K not reached => NOT done');
assert.equal(evaluateDone({buildableOpenIssues:0,openOrchestratorPRs:0,consecutiveDryCycles:1,requiredDryCycles:1}).done,false); ok('K floored at 2, cannot be argued down');
assert.equal(evaluateDone({}).done,false); ok('missing input fails SAFE');
assert.equal(evaluateDone({buildableOpenIssues:null,openOrchestratorPRs:0,consecutiveDryCycles:5}).done,false); ok('invalid count fails SAFE');
let p=evaluateDone({buildableOpenIssues:0,openOrchestratorPRs:0,consecutiveDryCycles:3,blockedOnHuman:2});
assert.equal(p.done,true); assert.equal(p.parked,true); ok('blocked-on-human => DONE but parked');

// materiality
assert.equal(nextDryCycles(2,[{kind:'docs-nit',confirmed:true}]),3); ok('sub-material finding does not reset K');
assert.equal(nextDryCycles(2,[{kind:'defect',confirmed:true}]),0); ok('material finding resets K');
assert.equal(nextDryCycles(2,[{kind:'defect',confirmed:false}]),3); ok('unconfirmed finding does not reset K');
assert.equal(nextDryCycles(2,null),2); ok('unevaluable findings neither advance nor reset');
assert.equal(isMaterial({kind:'security',confirmed:true}),true); ok('security is material');
console.log(`\n${n}/${n} passed`);
