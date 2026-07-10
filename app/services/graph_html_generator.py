"""
generate_graph_html.py
----------------------
Reads the GraphRAG knowledge store (entities.json, relationships.json,
community_map.json, communities/*.md) and generates a self-contained
interactive HTML knowledge graph visualisation using D3.js.

Usage:
    python scripts/generate_graph_html.py
    python scripts/generate_graph_html.py --out graph/knowledge_graph.html
    python scripts/generate_graph_html.py --out graph/knowledge_graph.html --title "RFP Graph"

Output: a single .html file — open in any browser, no server needed.
"""

import json
import argparse
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT            = Path(__file__).parent.parent.parent
GRAPH_DIR       = ROOT / "graph"
ENTITIES_FILE   = GRAPH_DIR / "entities.json"
RELATIONS_FILE  = GRAPH_DIR / "relationships.json"
COMMUNITY_FILE  = GRAPH_DIR / "community_map.json"
COMMUNITIES_DIR = GRAPH_DIR / "communities"
DEFAULT_OUT     = GRAPH_DIR / "knowledge_graph.html"

# ── Colour palettes ───────────────────────────────────────────────────────────
# Up to 16 communities — extend if needed
COMMUNITY_PALETTE = [
    "#FF7B72", "#79C0FF", "#FFA657", "#A5D6FF",
    "#D2A8FF", "#56D364", "#8B949E", "#F0B429",
    "#FF9E7A", "#39D353", "#58A6FF", "#FFD060",
    "#FF6E96", "#BC8CFF", "#E3B341", "#89DCFF",
]

TYPE_PALETTE = {
    "client":               "#FF7B72",
    "service_provider":     "#FFA657",
    "service":              "#A5D6FF",
    "investor":             "#FFD700",
    "standard":             "#79C0FF",
    "regulator":            "#F78166",
    "location":             "#56D364",
    "concept":              "#D2A8FF",
    "lender":               "#FF9E7A",
    "financial_instrument": "#FFD060",
    "acquisition_target":   "#FF6E96",
    "technology":           "#39D353",
    "exchange":             "#58A6FF",
    "deliverable":          "#BC8CFF",
}
DEFAULT_TYPE_COLOR = "#8B949E"


# ── Data loading ──────────────────────────────────────────────────────────────

def load_data():
    entities      = json.loads(ENTITIES_FILE.read_text(encoding="utf-8"))
    relationships = json.loads(RELATIONS_FILE.read_text(encoding="utf-8"))
    community_map = json.loads(COMMUNITY_FILE.read_text(encoding="utf-8"))
    return entities, relationships, community_map


def build_graph_data(entities, relationships, community_map):
    """
    Produce the node/edge JSON that D3 will consume.
    Nodes carry community id (string), type, label, aliases, source_docs,
    attributes.  Edges carry source/target ids and label.
    """
    communities   = community_map.get("communities", {})
    node_to_comm  = community_map.get("node_to_community", {})

    # Build community label map: comm_id -> short descriptive name
    comm_labels = {}
    for cid, comm in communities.items():
        entity_names = [e.get("name", "") for e in comm.get("entities", [])[:3]]
        comm_labels[str(cid)] = ", ".join(entity_names) if entity_names else f"Community {cid}"

    # Load community summary previews (first non-header line)
    comm_summaries = {}
    if COMMUNITIES_DIR.exists():
        for md_file in sorted(COMMUNITIES_DIR.glob("community_*.md")):
            cid = md_file.stem.split("_")[1].lstrip("0") or "0"
            lines = [l.strip() for l in md_file.read_text(encoding="utf-8").splitlines()
                     if l.strip() and not l.startswith("#")]
            comm_summaries[cid] = lines[0][:200] if lines else ""

    # Build nodes
    nodes = []
    entity_by_id = {}
    for e in entities:
        eid  = e.get("id") or e.get("name", "").lower().replace(" ", "_")
        comm = str(node_to_comm.get(eid, "0"))
        node = {
            "id":          eid,
            "label":       e.get("name", eid),
            "type":        e.get("type", "unknown"),
            "community":   comm,
            "aliases":     e.get("aliases", []),
            "source_docs": e.get("source_docs", []),
            "attributes":  e.get("attributes", {}),
        }
        nodes.append(node)
        entity_by_id[eid] = node

    # Build edges (skip self-loops; normalise to string ids)
    edges = []
    seen  = set()
    for r in relationships:
        src = r.get("source") or r.get("source_id", "")
        tgt = r.get("target") or r.get("target_id", "")
        rel = r.get("relation_type") or r.get("label") or r.get("relationship_type", "")
        doc = r.get("source_doc", "")
        page= r.get("page", "")
        if not src or not tgt or src == tgt:
            continue
        key = f"{src}|{tgt}|{rel}"
        if key in seen:
            continue
        seen.add(key)
        edges.append({
            "source": src,
            "target": tgt,
            "label":  rel,
            "doc":    doc,
            "page":   page,
        })

    # Build community palette map
    all_comms = sorted({n["community"] for n in nodes}, key=lambda x: int(x) if x.isdigit() else 0)
    comm_colors = {cid: COMMUNITY_PALETTE[i % len(COMMUNITY_PALETTE)]
                   for i, cid in enumerate(all_comms)}

    return {
        "nodes":         nodes,
        "edges":         edges,
        "community_labels":   comm_labels,
        "community_colors":   comm_colors,
        "community_summaries": comm_summaries,
        "type_colors":   TYPE_PALETTE,
        "stats": {
            "nodes":       len(nodes),
            "edges":       len(edges),
            "communities": len(all_comms),
        },
    }


# ── HTML template ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>{title}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<style>
* {{ box-sizing:border-box; margin:0; padding:0; }}
body {{ background:#0D1117; color:#E6EDF3; font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; display:flex; flex-direction:column; height:100vh; overflow:hidden; }}

header {{ background:#161B22; border-bottom:1px solid #21262D; padding:10px 18px; display:flex; align-items:center; gap:16px; flex-shrink:0; }}
header h1 {{ font-size:15px; font-weight:600; }}
header span {{ font-size:12px; color:#8B949E; }}
.stats {{ display:flex; gap:14px; margin-left:auto; }}
.stat {{ font-size:11px; color:#8B949E; }}
.stat b {{ color:#58A6FF; }}

.controls {{ background:#161B22; border-bottom:1px solid #21262D; padding:8px 18px; display:flex; align-items:center; gap:12px; flex-shrink:0; flex-wrap:wrap; }}
.controls label {{ font-size:11px; color:#8B949E; }}
select, input[type=range] {{ background:#0D1117; border:1px solid #30363D; color:#E6EDF3; padding:3px 8px; border-radius:4px; font-size:11px; cursor:pointer; }}
.btn {{ background:#21262D; border:1px solid #30363D; color:#8B949E; padding:4px 10px; border-radius:4px; font-size:11px; cursor:pointer; }}
.btn:hover {{ background:#30363D; color:#E6EDF3; }}
.search-wrap input[type=text] {{ background:#0D1117; border:1px solid #30363D; color:#E6EDF3; padding:4px 10px; border-radius:4px; font-size:11px; width:180px; }}
.search-wrap input[type=text]:focus {{ outline:none; border-color:#58A6FF; }}

.main {{ display:flex; flex:1; overflow:hidden; }}
#graph {{ flex:1; overflow:hidden; }}
svg {{ width:100%; height:100%; }}

.node circle {{ stroke-width:2; cursor:pointer; }}
.node text {{ font-size:10px; fill:#C9D1D9; pointer-events:none; text-shadow:0 1px 3px #0D1117,0 -1px 3px #0D1117; }}
.node.dimmed circle {{ opacity:0.1; }}
.node.dimmed text {{ opacity:0.08; }}
.node.highlighted circle {{ stroke-width:3; stroke:#fff !important; }}
.link {{ stroke-opacity:0.4; }}
.link.dimmed {{ stroke-opacity:0.04; }}
.link.highlighted {{ stroke-opacity:1; stroke-width:2.5; }}
.edge-label {{ font-size:8.5px; fill:#8B949E; pointer-events:none; }}

.panel {{ width:280px; flex-shrink:0; background:#161B22; border-left:1px solid #21262D; overflow-y:auto; display:flex; flex-direction:column; }}
.panel-header {{ padding:12px 14px 8px; border-bottom:1px solid #21262D; font-size:12px; font-weight:600; color:#8B949E; text-transform:uppercase; letter-spacing:.05em; }}
.panel-body {{ padding:12px 14px; flex:1; }}
.panel-empty {{ color:#484F58; font-size:12px; margin-top:8px; }}
.node-title {{ font-size:13px; font-weight:600; color:#E6EDF3; margin-bottom:4px; line-height:1.4; }}
.node-type {{ display:inline-block; padding:1px 7px; border-radius:10px; font-size:10px; font-weight:600; margin-bottom:10px; }}
.node-section {{ margin-bottom:10px; }}
.node-section h4 {{ font-size:10px; color:#484F58; text-transform:uppercase; letter-spacing:.05em; margin-bottom:4px; }}
.node-section p, .node-section li {{ font-size:11px; color:#8B949E; line-height:1.6; }}
.node-section ul {{ padding-left:14px; }}
.rel-item {{ font-size:10.5px; color:#8B949E; padding:3px 0; border-bottom:1px solid #1A1F26; }}
.rel-item .rt {{ color:#58A6FF; font-weight:600; }}
.rel-item .rc {{ color:#C9D1D9; }}
.rel-item .rd {{ color:#484F58; font-size:9.5px; }}
.comm-badge {{ display:inline-block; padding:1px 7px; border-radius:10px; font-size:9px; font-weight:700; margin-top:4px; }}

.legend {{ padding:10px 14px; border-top:1px solid #21262D; }}
.legend h4 {{ font-size:10px; color:#484F58; text-transform:uppercase; letter-spacing:.05em; margin-bottom:6px; }}
.legend-item {{ display:flex; align-items:center; gap:6px; margin-bottom:3px; font-size:10px; color:#8B949E; cursor:pointer; }}
.legend-dot {{ width:10px; height:10px; border-radius:50%; flex-shrink:0; }}

#tooltip {{ position:absolute; background:#161B22; border:1px solid #30363D; border-radius:6px; padding:8px 10px; font-size:11px; color:#E6EDF3; pointer-events:none; display:none; max-width:220px; line-height:1.5; z-index:100; }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <span>GraphRAG Knowledge Graph</span>
  <div class="stats">
    <div class="stat"><b id="sn">{stats_nodes}</b> entities</div>
    <div class="stat"><b id="se">{stats_edges}</b> relationships</div>
    <div class="stat"><b id="sc">{stats_comms}</b> communities</div>
  </div>
</header>
<div class="controls">
  <label>Community: <select id="fc"><option value="all">All</option></select></label>
  <label>Type: <select id="ft"><option value="all">All types</option></select></label>
  <div class="search-wrap"><input type="text" id="search" placeholder="Search entities…"></div>
  <label>Spread: <input type="range" id="strength" min="50" max="500" value="180"></label>
  <button class="btn" id="btn-reset">Reset</button>
  <button class="btn" id="btn-labels">Hide labels</button>
</div>
<div class="main">
  <div id="graph"><svg id="svg"></svg></div>
  <div class="panel">
    <div class="panel-header">Entity Details</div>
    <div class="panel-body" id="pb"><div class="panel-empty">Click a node to inspect it</div></div>
    <div class="legend">
      <h4>Entity Types</h4><div id="tleg"></div>
      <h4 style="margin-top:10px">Communities</h4><div id="cleg"></div>
    </div>
  </div>
</div>
<div id="tooltip"></div>

<script>
// ── Embedded graph data ───────────────────────────────────────────────────────
const GRAPH = {graph_data_json};
const CC    = GRAPH.community_colors;
const TC    = GRAPH.type_colors;
const CL    = GRAPH.community_labels;
const CS    = GRAPH.community_summaries || {{}};
const DEFAULT_TC = "#8B949E";

// ── Degree map for node sizing ────────────────────────────────────────────────
const deg = {{}};
GRAPH.edges.forEach(e => {{ deg[e.source]=(deg[e.source]||0)+1; deg[e.target]=(deg[e.target]||0)+1; }});
const maxDeg = Math.max(1, ...Object.values(deg));
const nodeR  = n => 5 + ((deg[n.id]||0)/maxDeg)*14;

// ── SVG / zoom setup ─────────────────────────────────────────────────────────
const svgEl = document.getElementById('svg');
const cont  = document.getElementById('graph');
let W = cont.clientWidth, H = cont.clientHeight;
const svg = d3.select(svgEl);
const g   = svg.append('g');
svg.call(d3.zoom().scaleExtent([0.1,5]).on('zoom', e => g.attr('transform', e.transform)));

// Arrow marker
svg.append('defs').append('marker')
  .attr('id','arr').attr('viewBox','0 -4 8 8').attr('refX',20).attr('refY',0)
  .attr('markerWidth',6).attr('markerHeight',6).attr('orient','auto')
  .append('path').attr('d','M0,-4L8,0L0,4').attr('fill','#30363D');

// ── Simulation ────────────────────────────────────────────────────────────────
const nodes = GRAPH.nodes.map(d => ({{...d}}));
const edges = GRAPH.edges.map(d => ({{...d}}));

const sim = d3.forceSimulation(nodes)
  .force('link', d3.forceLink(edges).id(d=>d.id).distance(+document.getElementById('strength').value))
  .force('charge', d3.forceManyBody().strength(-250))
  .force('center', d3.forceCenter(W/2, H/2))
  .force('collision', d3.forceCollide().radius(d=>nodeR(d)+8));

// ── Render ────────────────────────────────────────────────────────────────────
const linkSel = g.append('g').selectAll('line').data(edges).join('line')
  .attr('class','link')
  .attr('stroke', d => CC[d.source.community] || CC[d.target?.community] || '#30363D')
  .attr('stroke-width',1)
  .attr('marker-end','url(#arr)');

const elabSel = g.append('g').selectAll('text').data(edges).join('text')
  .attr('class','edge-label').text(d=>d.label);

const nodeSel = g.append('g').selectAll('g').data(nodes).join('g')
  .attr('class','node')
  .call(d3.drag()
    .on('start',(e,d)=>{{ if(!e.active) sim.alphaTarget(.3).restart(); d.fx=d.x; d.fy=d.y; }})
    .on('drag', (e,d)=>{{ d.fx=e.x; d.fy=e.y; }})
    .on('end',  (e,d)=>{{ if(!e.active) sim.alphaTarget(0); d.fx=null; d.fy=null; }})
  )
  .on('click', (e,d)=>{{ e.stopPropagation(); selectNode(d); }})
  .on('mouseover',(e,d)=>showTip(e,d))
  .on('mousemove', e=>moveTip(e))
  .on('mouseout', hideTip);

nodeSel.append('circle')
  .attr('r', d=>nodeR(d))
  .attr('fill', d=>CC[d.community]||'#8B949E')
  .attr('stroke', d=>d3.color(CC[d.community]||'#8B949E').darker(.6));

nodeSel.append('text')
  .attr('dy', d=>-nodeR(d)-3)
  .attr('text-anchor','middle')
  .text(d=>d.label.length>28 ? d.label.slice(0,26)+'…' : d.label);

sim.on('tick', ()=>{{
  linkSel.attr('x1',d=>d.source.x).attr('y1',d=>d.source.y)
         .attr('x2',d=>d.target.x).attr('y2',d=>d.target.y);
  elabSel.attr('x',d=>(d.source.x+d.target.x)/2).attr('y',d=>(d.source.y+d.target.y)/2);
  nodeSel.attr('transform',d=>`translate(${{d.x}},${{d.y}})`);
}});

// ── Tooltip ───────────────────────────────────────────────────────────────────
const tip = document.getElementById('tooltip');
function showTip(e,d){{
  tip.innerHTML=`<b>${{d.label}}</b><br><span style="color:#8B949E;font-size:10px">${{d.type}} · Community ${{d.community}}</span>`;
  tip.style.display='block'; moveTip(e);
}}
function moveTip(e){{ tip.style.left=(e.pageX+12)+'px'; tip.style.top=(e.pageY-10)+'px'; }}
function hideTip(){{ tip.style.display='none'; }}

// ── Panel ─────────────────────────────────────────────────────────────────────
function selectNode(d){{
  const col  = CC[d.community]||'#8B949E';
  const tcol = TC[d.type]||DEFAULT_TC;
  const rels = edges.filter(e=>{{
    const s=typeof e.source==='object'?e.source.id:e.source;
    const t=typeof e.target==='object'?e.target.id:e.target;
    return s===d.id||t===d.id;
  }});
  const attrs = Object.entries(d.attributes||{{}}).filter(([,v])=>v!==null&&v!=='');
  const pb = document.getElementById('pb');
  pb.innerHTML = `
    <div class="node-title">${{d.label}}</div>
    <span class="node-type" style="background:${{tcol}}22;color:${{tcol}}">${{d.type}}</span>
    <div style="font-size:11px;color:#8B949E;margin-bottom:10px">
      Community <span class="comm-badge" style="background:${{col}}22;color:${{col}}">${{d.community}} — ${{(CL[d.community]||'').slice(0,40)}}</span>
      ${{CS[d.community]?`<br><span style="font-size:10px;color:#484F58;margin-top:4px;display:block">${{CS[d.community].slice(0,120)}}…</span>`:''}}
    </div>
    ${{d.aliases?.length?`<div class="node-section"><h4>Aliases</h4><p>${{d.aliases.join(', ')}}</p></div>`:''}}
    ${{d.source_docs?.length?`<div class="node-section"><h4>Source Documents</h4><ul>${{d.source_docs.map(s=>`<li>${{s}}</li>`).join('')}}</ul></div>`:''}}
    ${{attrs.length?`<div class="node-section"><h4>Attributes</h4>${{attrs.map(([k,v])=>`<p><b>${{k}}:</b> ${{v}}</p>`).join('')}}</div>`:''}}
    <div class="node-section"><h4>Relationships (${{rels.length}})</h4>
      ${{rels.map(r=>{{
        const s=typeof r.source==='object'?r.source.id:r.source;
        const t=typeof r.target==='object'?r.target.id:r.target;
        const other=s===d.id?t:s, dir=s===d.id?'→':'←';
        return `<div class="rel-item">${{dir}} <span class="rt">${{r.label}}</span> <span class="rc">${{other.replace(/_/g,' ')}}</span><br><span class="rd">${{r.doc}}, p.${{r.page}}</span></div>`;
      }}).join('')}}
    </div>`;
  highlight(d.id);
}}

function highlight(id){{
  const ci=new Set([id]);
  const ce=new Set();
  edges.forEach((e,i)=>{{
    const s=typeof e.source==='object'?e.source.id:e.source;
    const t=typeof e.target==='object'?e.target.id:e.target;
    if(s===id||t===id){{ ci.add(s); ci.add(t); ce.add(i); }}
  }});
  nodeSel.classed('dimmed',d=>!ci.has(d.id)).classed('highlighted',d=>d.id===id);
  linkSel.classed('dimmed',(_,i)=>!ce.has(i)).classed('highlighted',(_,i)=>ce.has(i));
}}

svg.on('click',()=>{{
  nodeSel.classed('dimmed',false).classed('highlighted',false);
  linkSel.classed('dimmed',false).classed('highlighted',false);
  document.getElementById('pb').innerHTML='<div class="panel-empty">Click a node to inspect it</div>';
}});

// ── Legends ───────────────────────────────────────────────────────────────────
const tleg=document.getElementById('tleg');
const typeCounts={{}};
nodes.forEach(n=>typeCounts[n.type]=(typeCounts[n.type]||0)+1);
Object.entries(TC).forEach(([t,c])=>{{
  if(!typeCounts[t]) return;
  const el=document.createElement('div'); el.className='legend-item';
  el.innerHTML=`<div class="legend-dot" style="background:${{c}}"></div>${{t}} <span style="color:#484F58;margin-left:auto">${{typeCounts[t]}}</span>`;
  tleg.appendChild(el);
}});

const cleg=document.getElementById('cleg');
const commCounts={{}};
nodes.forEach(n=>commCounts[n.community]=(commCounts[n.community]||0)+1);
Object.entries(CC).forEach(([cid,c])=>{{
  if(!commCounts[cid]) return;
  const el=document.createElement('div'); el.className='legend-item';
  const short=(CL[cid]||'').split(',')[0].trim().slice(0,25);
  el.innerHTML=`<div class="legend-dot" style="background:${{c}}"></div>C${{cid}}: ${{short}} <span style="color:#484F58;margin-left:auto">${{commCounts[cid]}}</span>`;
  el.onclick=()=>{{ document.getElementById('fc').value=cid; applyFilters(); }};
  cleg.appendChild(el);
}});

// ── Filters ───────────────────────────────────────────────────────────────────
const fc=document.getElementById('fc');
const ft=document.getElementById('ft');
Object.entries(CC).filter(([cid])=>commCounts[cid]).forEach(([cid])=>{{
  const o=document.createElement('option'); o.value=cid;
  o.text=`C${{cid}}: ${{(CL[cid]||'').split(',')[0].trim().slice(0,30)}}`;
  fc.appendChild(o);
}});
[...new Set(nodes.map(n=>n.type))].sort().forEach(t=>{{
  const o=document.createElement('option'); o.value=t; o.text=t; ft.appendChild(o);
}});

function applyFilters(){{
  const comm=fc.value, type=ft.value, q=document.getElementById('search').value.toLowerCase();
  const visIds=new Set(nodes.filter(n=>{{
    if(comm!=='all'&&n.community!==comm) return false;
    if(type!=='all'&&n.type!==type)     return false;
    if(q&&!n.label.toLowerCase().includes(q)&&!n.id.includes(q)) return false;
    return true;
  }}).map(n=>n.id));
  nodeSel.style('display',d=>visIds.has(d.id)?null:'none');
  const show=e=>{{
    const s=typeof e.source==='object'?e.source.id:e.source;
    const t=typeof e.target==='object'?e.target.id:e.target;
    return visIds.has(s)&&visIds.has(t)?null:'none';
  }};
  linkSel.style('display',show);
  elabSel.style('display',show);
}}
fc.addEventListener('change',applyFilters);
ft.addEventListener('change',applyFilters);
document.getElementById('search').addEventListener('input',applyFilters);

// ── Controls ──────────────────────────────────────────────────────────────────
document.getElementById('strength').addEventListener('input',function(){{
  sim.force('link').distance(+this.value); sim.alpha(.3).restart();
}});
document.getElementById('btn-reset').addEventListener('click',()=>{{
  svg.transition().duration(400).call(d3.zoom().transform,d3.zoomIdentity);
  fc.value='all'; ft.value='all'; document.getElementById('search').value=''; applyFilters();
}});
let showLabels=true;
document.getElementById('btn-labels').addEventListener('click',function(){{
  showLabels=!showLabels;
  nodeSel.selectAll('text').style('display',showLabels?null:'none');
  elabSel.style('display',showLabels?null:'none');
  this.textContent=showLabels?'Hide labels':'Show labels';
}});
window.addEventListener('resize',()=>{{
  W=cont.clientWidth; H=cont.clientHeight;
  sim.force('center',d3.forceCenter(W/2,H/2)).alpha(.1).restart();
}});
</script>
</body>
</html>
"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate interactive knowledge graph HTML from GraphRAG data")
    parser.add_argument("--out",   default=str(DEFAULT_OUT), help="Output HTML path")
    parser.add_argument("--title", default="RFP Knowledge Graph Explorer", help="Page title")
    args = parser.parse_args()

    print("Loading graph data...")
    entities, relationships, community_map = load_data()

    print(f"  Entities     : {len(entities)}")
    print(f"  Relationships: {len(relationships)}")

    graph_data = build_graph_data(entities, relationships, community_map)

    print(f"  Nodes        : {graph_data['stats']['nodes']}")
    print(f"  Edges        : {graph_data['stats']['edges']}")
    print(f"  Communities  : {graph_data['stats']['communities']}")

    html = HTML_TEMPLATE.format(
        title       = args.title,
        stats_nodes = graph_data["stats"]["nodes"],
        stats_edges = graph_data["stats"]["edges"],
        stats_comms = graph_data["stats"]["communities"],
        graph_data_json = json.dumps(graph_data, ensure_ascii=False, indent=None),
    )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    print(f"\nDone! Saved to: {out}")
    print("Open the file in any browser — no server required.")


if __name__ == "__main__":
    main()
