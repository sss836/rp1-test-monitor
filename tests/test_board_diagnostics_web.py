"""Browser report rendering is read-only, robot-scoped and text-only."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="Node用于板端报告DOM验证")
def test_board_history_empty_error_simulated_and_late_response():
    source = Path(__file__).parents[1] / "src/rp1_monitor/web/operations.js"
    script = r"""
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const code=fs.readFileSync(process.argv[1],'utf8');
function element(){return {textContent:'',children:[],replaceChildren(){this.children=[]},append(...items){this.children.push(...items)}};}
const nodes={},pending=[],calls=[];
const ctx={selected:'A',revision:0,el:id=>nodes[id]||(nodes[id]=element()),document:{createElement:element},
 base:id=>`/robots/${id}`,request:url=>{calls.push(url);return new Promise((resolve,reject)=>pending.push({resolve:d=>resolve({json:async()=>d}),reject}));}};
vm.createContext(ctx);vm.runInContext(code.slice(code.indexOf('  async function refreshBoard('),code.indexOf('  async function perform(')),ctx);
const sample={source_kind:'simulated',created_at:'2026-10-26T00:00:00Z',board_diagnostics:{tui_version:'1.0.0',outcome:'incomplete',results:[{id:'RP007',result:'needs_review',message:'synthetic',plugin:{documentation:{title:'<img onerror=alert(1)>'}}}]}};
(async()=>{
 let done=ctx.refreshBoard('A',0);pending.shift().resolve({latest:null});await done;
 assert.match(nodes['ops-board-status'].textContent,/暂无板端报告/);assert.match(nodes['ops-board-detail'].textContent,/不能视为检查通过/);
 done=ctx.refreshBoard('A',0);pending.shift().resolve({latest:sample});await done;
 assert.match(nodes['ops-board-status'].textContent,/模拟数据/);assert.match(nodes['ops-board-status'].textContent,/未完整完成/);
 assert.equal(nodes['ops-board-results'].children[0].children[0].textContent,'RP007 · <img onerror=alert(1)>');
 assert.equal(nodes['ops-board-results'].children[0].children[1].textContent,'待人工复核');
 done=ctx.refreshBoard('A',0);pending.shift().reject(new Error('network unavailable'));await done;
 assert.match(nodes['ops-board-status'].textContent,/可刷新重试/);
 done=ctx.refreshBoard('A',0);ctx.selected='B';ctx.revision=1;
 nodes['ops-board-status'].textContent='B current';pending.shift().resolve({latest:sample});await done;
 assert.equal(nodes['ops-board-status'].textContent,'B current');
 assert(calls.every(url=>url.endsWith('/board-diagnostics')));assert(!code.includes("/board-diagnostics/runs"));
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    subprocess.run(["node", "-e", script, str(source)], check=True, capture_output=True, text=True)
