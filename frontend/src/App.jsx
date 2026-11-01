import { useEffect } from 'react';
import { startStudio } from './studio.js';

export default function App() {
  useEffect(() => startStudio(), []);
  return (
    <div className="app">
      <header className="top">
        <div className="brand"><i></i><span>Modelic<b>AI</b> Studio</span></div>
        <div className="crumb">models / <strong id="crumbName">—</strong></div>
        <span id="status" className="pill">idle</span>
        <div className="spacer"></div>
        <select id="themeSel" className="theme-sel" title="Colour theme" defaultValue="auto">
          <option value="auto">Auto (day / night)</option>
          <optgroup label="Night"><option value="graphite">Graphite</option><option value="midnight">Midnight</option><option value="lagoon">Lagoon (turquoise)</option><option value="crimson">Crimson (red)</option><option value="forest">Forest (green)</option></optgroup>
          <optgroup label="Day"><option value="daylight">Daylight</option><option value="paper">Paper</option><option value="ocean">Ocean (blue)</option><option value="blossom">Blossom (pink)</option><option value="sunflower">Sunflower (yellow)</option><option value="eink">E-ink (grey paper)</option></optgroup>
        </select>
        <button className="btn" id="btnCheck" title="Save & check model (Ctrl+S)">Check <span className="kbd">⌘S</span></button>
        <button className="btn primary" id="btnSim" title="Simulate (Ctrl+Enter)">▶ Simulate <span className="kbd">⌘↵</span></button>
      </header>
    
      <aside className="left">
        <div id="leftGrip" className="grip" title="Drag to resize · double-click to reset"></div>
        <div className="sec">
          <h3>Model library <span><button className="btn" style={{height: '20px', padding: '0 6px', fontSize: '11px', marginRight: '4px'}} id="btnNew" title="Start a new model in a fresh conversation">+ New</button><button className="btn" style={{height: '20px', padding: '0 6px', fontSize: '11px'}} id="btnRefresh">↻</button></span></h3>
          <div className="lib" id="lib"></div>
        </div>
        <div className="chat">
          <h3 style={{margin: '0', padding: '10px 12px 6px', fontSize: '11px', fontWeight: '600', letterSpacing: '.08em', textTransform: 'uppercase', color: 'var(--muted)'}}>Modi · Modelling assistant</h3>
          <div className="msgs" id="msgs">
            <div className="msg bot">Describe a physical system and I'll write the Modelica model, compile it with OpenModelica (fixing errors on my own), pick solver settings, and simulate it.</div>
          </div>
          <div className="suggest" id="suggest">
            <button>RC low-pass filter, 1 kΩ / 10 µF, step input</button>
            <button>Mass-spring-damper, m=1 kg, k=100 N/m, d=2 Ns/m</button>
            <button>DC motor with PI speed control</button>
          </div>
          <div className="composer">
            <textarea id="prompt" placeholder="Ask Modi, e.g. model a double pendulum and simulate it for 10 s…" />
            <div className="row"><span>Enter to send · Shift+Enter for a new line</span><button className="btn primary" id="btnSend" style={{height: '24px'}}>Send</button></div>
          </div>
        </div>
      </aside>
    
      <main className="center">
        <section className="ws">
          <div className="tabs" id="tabs">
            <button className="tab on" data-v="diagram">Diagram</button>
            <button className="tab" data-v="code">Modelica</button>
            <button className="tab" data-v="split">Split</button>
            <button className="tab" data-v="console">Console</button>
            <div className="tools"><button className="btn" id="btnCanvas" title="Diagram canvas: follow the theme, or always light paper (like OMEdit)">Canvas: theme</button><button className="btn" id="btnFit">Fit view</button></div>
          </div>
          <div className="views" id="views">
            <div className="view v-diagram on" data-v="diagram">
              <svg id="diagram"></svg>
              <div className="livebar off" id="liveBar">
                <button className="btn" id="livePlay" title="Play the simulation on the diagram">▶</button>
                <input type="range" id="liveSlider" min="0" max="0" defaultValue="0" />
                <span id="liveTime">t = 0 s</span>
                <select id="liveSpeed" title="Playback length" defaultValue="6"><option value="3">3 s</option><option value="6">6 s</option><option value="12">12 s</option><option value="24">24 s</option></select>
              </div>
              <div className="livehint" id="liveHint" style={{display: 'none'}}></div>
              <div className="empty" id="diagEmpty">No diagram yet<br /><small>Ask Modi for a model, or open one from the library</small></div>
            </div>
            <div className="view v-code" data-v="code"><div id="editor" style={{width: '100%', height: '100%'}}></div></div>
            <div className="view v-console" data-v="console"><pre id="console"></pre></div>
          </div>
        </section>
        <div className="gut" id="gut" title="Drag to resize"></div>
        <section className="results">
          <div className="vars">
            <input type="search" id="vsearch" placeholder="Filter variables…" />
            <div className="vlist" id="vlist"><div className="hint">Simulate to browse variables.</div></div>
          </div>
          <div className="plotwrap">
            <div className="plotbar">
              <span id="runInfo">No results</span><div className="spacer"></div>
              <button className="btn" id="btnPin" title="Keep this run as a dashed overlay to compare against the next run">Pin run for compare</button>
              <button className="btn" id="btnClearPin">Clear pins</button>
              <button className="btn" id="btnCsv">Export CSV</button>
            </div>
            <div id="plot"></div>
          </div>
        </section>
      </main>
    
      <aside className="right">
        <div className="sec">
          <h3>Simulation setup</h3>
          <div className="props" id="simsetup">
            <div className="prow"><label>Start time [s]</label><input id="sStart" defaultValue="0" /></div>
            <div className="prow"><label>Stop time [s]</label><input id="sStop" defaultValue="0.01" /></div>
            <div className="prow"><label>Intervals</label><input id="sN" defaultValue="500" /></div>
            <div className="prow"><label>Solver</label><select id="sMethod">
              <option>dassl</option><option>ida</option><option>cvode</option><option>euler</option><option>rungekutta</option><option>impeuler</option><option>gbode</option>
            </select></div>
          </div>
        </div>
        <div className="sec" style={{border: '0'}}>
          <h3>Parameters <span id="dirtyCount" style={{textTransform: 'none', letterSpacing: '0', color: 'var(--accent)'}}></span></h3>
          <div className="hint">Edited values are applied as run-time overrides, no recompile needed. Click a component in the diagram to jump to it.</div>
          <div className="props" id="params"><div className="hint" style={{padding: '0'}}>Open a model to edit its parameters.</div></div>
          <div style={{padding: '0 12px 16px', display: 'flex', gap: '6px'}}>
            <button className="btn" id="btnResetParams">Reset</button>
            <button className="btn" id="btnBake" title="Write edited values into the Modelica source">Write into model</button>
          </div>
        </div>
      </aside>
    
      <footer className="foot">
        <span id="footModel">no model</span>
        <span className="log" id="footLog">Ready.</span>
        <span>ModelicAI Studio · by Anurag Dogra</span>
      </footer>
    </div>
  );
}
