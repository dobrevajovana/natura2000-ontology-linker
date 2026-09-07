"use strict";

/* =============================================================================
 * BioSample Geo & Knowledge Graph Explorer — frontend
 *
 * Sections:
 *   1. STATE      - central mutable state + filter model
 *   2. DOM        - small DOM helpers (safe text insertion, no innerHTML)
 *   3. API        - fetch wrappers, debouncing, abort handling
 *   4. FILTERS    - control-panel wiring, facets/legend population
 *   5. MAP        - Leaflet map, clustering, draw/select modes
 *   6. GRAPH      - Cytoscape graph, incremental updates, styling
 *   7. DETAIL     - sample / node detail side panel
 *   8. STATS      - stats strip
 *   9. LAYOUT     - pane split / full-height toggle + splitter drag
 *  10. INIT       - bootstrap
 * ===========================================================================*/

/* ------------------------------------------------------------------------- *
 * 1. STATE
 * ------------------------------------------------------------------------- */

const STATE = {
  filters: {
    search: "",
    country: "",
    region: "",
    precision: new Set(),   // empty set == "all"
    ontology: new Set(),
    archive: new Set(),
    // Default to 0.7: below this OLS4 matches are mostly incidental string
    // overlap, and showing them buries the matches that mean something.
    min_confidence: 0.7,
    has_coords: null,       // null | true | false
    show_archives: false,   // archive hubs are uniform noise in the layout
    bbox: null,             // [minLat, minLon, maxLat, maxLon]
    center: null,           // [lat, lon]
    radius_km: null,
  },
  legend: {
    node_kinds: [],
    edge_kinds: [],
    ontology_sources: {},
    archive_sources: {},
  },
  facets: {
    countries: [],
    regions: [],
    precisions: [],
    ontologies: [],
    archives: [],
  },
  serverLoaded: {
    samples_loaded: true,
    graph_loaded: true,
  },
  selectMode: null, // null | "box" | "radius"
};

const DEFAULT_KIND_STYLE = {
  sample: { color: "#1565c0", shape: "ellipse" },
  taxon: { color: "#2e7d32", shape: "triangle" },
  environment: { color: "#00796b", shape: "hexagon" },
  place: { color: "#ef6c00", shape: "rectangle" },
  archive: { color: "#455a64", shape: "round-rectangle" },
  project: { color: "#37474f", shape: "diamond" },
  attribute: { color: "#6a1b9a", shape: "ellipse" },
};
const FALLBACK_KIND_STYLE = { color: "#9e9e9e", shape: "ellipse" };

/* ------------------------------------------------------------------------- *
 * 2. DOM helpers
 * ------------------------------------------------------------------------- */

function el(tag, attrs, children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
  }
  (children || []).forEach((c) => {
    if (c === null || c === undefined) return;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  });
  return node;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function fmt(v) {
  if (v === null || v === undefined || v === "") return "—";
  return String(v);
}

/* ------------------------------------------------------------------------- *
 * 3. API
 * ------------------------------------------------------------------------- */

const API = (() => {
  let currentController = null;

  function buildParams(filters) {
    const p = new URLSearchParams();
    if (filters.search) p.set("search", filters.search);
    if (filters.country) p.set("country", filters.country);
    if (filters.region) p.set("region", filters.region);
    if (filters.precision.size) p.set("precision", [...filters.precision].join(","));
    if (filters.ontology.size) p.set("ontology", [...filters.ontology].join(","));
    if (filters.archive.size) p.set("archive", [...filters.archive].join(","));
    if (filters.min_confidence > 0) p.set("min_confidence", String(filters.min_confidence));
    if (filters.has_coords === true) p.set("has_coords", "1");
    if (filters.has_coords === false) p.set("has_coords", "0");
    if (filters.show_archives) p.set("show_archives", "1");
    if (filters.bbox) p.set("bbox", filters.bbox.join(","));
    if (filters.center && filters.radius_km != null) {
      p.set("center", filters.center.join(","));
      p.set("radius_km", String(filters.radius_km));
    }
    return p;
  }

  async function getJSON(path, params, signal) {
    const qs = params ? `?${params.toString()}` : "";
    const res = await fetch(path + qs, { signal });
    if (!res.ok) {
      throw new Error(`${path} -> HTTP ${res.status}`);
    }
    return res.json();
  }

  function abortInFlight() {
    if (currentController) currentController.abort();
    currentController = new AbortController();
    return currentController.signal;
  }

  return {
    abortInFlight,
    buildParams,
    facets: (signal) => getJSON("/api/facets", null, signal),
    legend: (signal) => getJSON("/api/legend", null, signal),
    stats: (filters, signal) => getJSON("/api/stats", buildParams(filters), signal),
    geojson: (filters, signal) => getJSON("/api/geojson", buildParams(filters), signal),
    graph: (filters, signal) => getJSON("/api/graph", buildParams(filters), signal),
    sample: (accession, signal) => getJSON(`/api/sample/${encodeURIComponent(accession)}`, null, signal),
    reload: () => fetch("/api/reload", { method: "POST" }).then((r) => r.json()),
  };
})();

function debounce(fn, wait) {
  let t = null;
  return (...args) => {
    if (t) clearTimeout(t);
    t = setTimeout(() => fn(...args), wait);
  };
}

/* ------------------------------------------------------------------------- *
 * 9. LAYOUT (defined early, used by init)
 * ------------------------------------------------------------------------- */

const LAYOUT = (() => {
  function setMode(mode) {
    const container = document.getElementById("split-container");
    container.classList.remove("mode-split", "mode-map", "mode-graph");
    container.classList.add(`mode-${mode}`);
    document.querySelectorAll(".pane-toggle-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.pane === mode);
    });
    // Panes may have been hidden/resized; nudge map + graph to recompute size.
    // resize() alone leaves the graph at its old zoom in a corner of the new
    // pane, so it must be re-fitted to the space it now has.
    setTimeout(() => {
      if (MAP.instance) MAP.instance.invalidateSize();
      if (GRAPH.cy) {
        GRAPH.cy.resize();
        GRAPH.cy.fit(undefined, 30);
        GRAPH.refreshLabels();
      }
    }, 60);
  }

  function initToggle() {
    document.querySelectorAll(".pane-toggle-btn").forEach((btn) => {
      btn.addEventListener("click", () => setMode(btn.dataset.pane));
    });
  }

  function initSplitter() {
    const splitter = document.getElementById("splitter");
    const container = document.getElementById("split-container");
    const mapPane = document.getElementById("map-pane");
    let dragging = false;

    splitter.addEventListener("mousedown", (e) => {
      dragging = true;
      e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      const rect = container.getBoundingClientRect();
      let ratio = (e.clientY - rect.top) / rect.height;
      ratio = Math.min(0.85, Math.max(0.15, ratio));
      mapPane.style.flex = `1 1 ${ratio * 100}%`;
      document.getElementById("graph-pane").style.flex = `1 1 ${(1 - ratio) * 100}%`;
    });
    window.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      if (MAP.instance) MAP.instance.invalidateSize();
      if (GRAPH.cy) GRAPH.cy.resize();
    });
  }

  return { init: () => { initToggle(); initSplitter(); }, setMode };
})();

/* ------------------------------------------------------------------------- *
 * 5. MAP
 * ------------------------------------------------------------------------- */

const MAP = (() => {
  let instance = null;
  let clusterGroup = null;
  let selectionLayer = null; // FeatureGroup holding the current selection shape
  let drawHandler = null;
  const markersByAccession = new Map();

  function precisionStyle(precision) {
    switch (precision) {
      case "coordinates":
        return { radius: 7, color: "#2e7d32", fillColor: "#2e7d32", fillOpacity: 0.85, weight: 2 };
      case "locality":
        return { radius: 7, color: "#2e7d32", fillColor: "#2e7d32", fillOpacity: 0.05, weight: 2 };
      case "country":
        return { radius: 6, color: "#9e9e9e", fillColor: "#9e9e9e", fillOpacity: 0.15, weight: 1.5, dashArray: "3,3" };
      default:
        return { radius: 5, color: "#bdbdbd", fillColor: "#bdbdbd", fillOpacity: 0.1, weight: 1, dashArray: "1,3" };
    }
  }

  function init() {
    instance = L.map("map", { zoomControl: true }).setView([20, 0], 2);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "&copy; OpenStreetMap contributors",
      maxZoom: 19,
    }).addTo(instance);

    clusterGroup = L.markerClusterGroup();
    instance.addLayer(clusterGroup);

    selectionLayer = new L.FeatureGroup();
    instance.addLayer(selectionLayer);

    instance.on(L.Draw.Event.CREATED, onShapeCreated);
  }

  function onShapeCreated(e) {
    selectionLayer.clearLayers();
    selectionLayer.addLayer(e.layer);

    if (e.layerType === "rectangle") {
      const b = e.layer.getBounds();
      STATE.filters.bbox = [b.getSouth(), b.getWest(), b.getNorth(), b.getEast()];
      STATE.filters.center = null;
      STATE.filters.radius_km = null;
      updateSelectionSummary(`Box: ${b.getSouth().toFixed(2)},${b.getWest().toFixed(2)} to ${b.getNorth().toFixed(2)},${b.getEast().toFixed(2)}`);
    } else if (e.layerType === "circle") {
      const c = e.layer.getLatLng();
      const radiusKm = e.layer.getRadius() / 1000;
      STATE.filters.center = [c.lat, c.lng];
      STATE.filters.radius_km = radiusKm;
      STATE.filters.bbox = null;
      updateSelectionSummary(`Radius: ${radiusKm.toFixed(1)} km around ${c.lat.toFixed(2)},${c.lng.toFixed(2)}`);
    }
    exitSelectMode();
    FILTERS.scheduleRefresh(0);
  }

  function updateSelectionSummary(text) {
    document.getElementById("selection-summary").textContent = text;
  }

  function clearSelection() {
    selectionLayer.clearLayers();
    STATE.filters.bbox = null;
    STATE.filters.center = null;
    STATE.filters.radius_km = null;
    updateSelectionSummary("");
    FILTERS.scheduleRefresh(0);
  }

  function enterSelectMode(mode) {
    exitSelectMode();
    STATE.selectMode = mode;
    document.querySelectorAll(".btn-toggle").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
    if (mode === "box") {
      drawHandler = new L.Draw.Rectangle(instance, { shapeOptions: { color: "#1565c0" } });
    } else if (mode === "radius") {
      drawHandler = new L.Draw.Circle(instance, { shapeOptions: { color: "#1565c0" } });
    }
    if (drawHandler) drawHandler.enable();
  }

  function exitSelectMode() {
    if (drawHandler) {
      drawHandler.disable();
      drawHandler = null;
    }
    STATE.selectMode = null;
    document.querySelectorAll(".btn-toggle").forEach((b) => b.classList.remove("active"));
  }

  function renderGeojson(fc) {
    clusterGroup.clearLayers();
    markersByAccession.clear();

    const features = (fc && fc.features) || [];
    document.getElementById("map-empty").hidden = features.length > 0;

    features.forEach((f) => {
      const [lon, lat] = f.geometry.coordinates; // GeoJSON is lon-first
      const props = f.properties || {};
      const style = precisionStyle(props.precision);
      const marker = L.circleMarker([lat, lon], style);
      const popupNode = buildPopup(props);
      marker.bindPopup(popupNode);
      marker.on("click", () => DETAIL.openSample(props.accession));
      clusterGroup.addLayer(marker);
      if (props.accession) markersByAccession.set(props.accession, marker);
    });
  }

  function buildPopup(props) {
    const wrap = el("div", { class: "map-popup" });
    wrap.appendChild(el("strong", { text: fmt(props.accession) }));
    wrap.appendChild(el("br"));
    wrap.appendChild(el("span", { text: fmt(props.title) }));
    wrap.appendChild(el("br"));
    wrap.appendChild(el("em", { text: fmt(props.organism) }));
    return wrap;
  }

  function panToAccession(accession) {
    const marker = markersByAccession.get(accession);
    if (!marker) return;
    instance.setView(marker.getLatLng(), Math.max(instance.getZoom(), 6));
    marker.openPopup();
  }

  return {
    get instance() { return instance; },
    init,
    renderGeojson,
    enterSelectMode,
    exitSelectMode,
    clearSelection,
    panToAccession,
    setLoading: (v) => { document.getElementById("map-loading").hidden = !v; },
  };
})();

/* ------------------------------------------------------------------------- *
 * 6. GRAPH
 * ------------------------------------------------------------------------- */

const GRAPH = (() => {
  let cy = null;
  let applyLabelVisibility = () => {};

  /** Coalesce a burst of events into one call on the next animation frame. */
  function debounceRaf(fn) {
    let queued = false;
    return () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        fn();
      });
    };
  }

  /** Node diameter: samples get a fixed size, everything else grows with degree. */
  function sizeFor(n) {
    if (n.data("kind") === "sample") return 26;
    const deg = n.degree(false);
    return Math.min(64, 18 + Math.sqrt(deg) * 7);
  }

  function nodeStyleFor(data) {
    const props = data.props || {};
    const src = props.source || props.ontology_source || "";
    if (STATE.legend.ontology_sources[src]) {
      return { color: STATE.legend.ontology_sources[src].color, shape: "diamond" };
    }
    if (data.kind === "archive" && STATE.legend.archive_sources[src]) {
      return { color: STATE.legend.archive_sources[src].color, shape: "round-rectangle" };
    }
    return DEFAULT_KIND_STYLE[data.kind] || FALLBACK_KIND_STYLE;
  }

  function init() {
    cy = cytoscape({
      container: document.getElementById("cy"),
      style: [
        {
          selector: "node",
          style: {
            "background-color": (n) => nodeStyleFor(n.data()).color,
            shape: (n) => nodeStyleFor(n.data()).shape,
            // Labels are driven by a scratch flag rather than data(label) so
            // zoom level and hover can turn them off; drawing 700 captions at
            // once is what made the graph unreadable.
            label: (n) => (n.scratch("_showLabel") ? n.data("label") : ""),
            "font-size": 11,
            "font-weight": (n) => (n.data("kind") === "sample" ? 400 : 500),
            color: "#37474f",
            "text-valign": "bottom",
            "text-halign": "center",
            "text-margin-y": 3,
            "text-wrap": "ellipsis",
            "text-max-width": 110,
            "text-background-color": "#fff",
            "text-background-opacity": 0.75,
            "text-background-padding": 2,
            "text-background-shape": "roundrectangle",
            // Hubs earn their size: a taxon shared by 35 samples should read as
            // more important than a leaf.
            width: (n) => sizeFor(n),
            height: (n) => sizeFor(n),
            "border-width": 1,
            "border-color": "#00000033",
            "z-index": (n) => (n.data("kind") === "sample" ? 2 : 1),
          },
        },
        {
          selector: "node:selected",
          style: {
            "border-width": 4,
            "border-color": "#1565c0",
            "text-background-color": "#1565c0",
            "text-background-opacity": 1,
            color: "#fff",
          },
        },
        { selector: "node.faded", style: { opacity: 0.12, "text-opacity": 0 } },
        { selector: "node.hl", style: { "border-width": 3, "border-color": "#1565c0" } },
        {
          selector: "edge",
          style: {
            width: 1,
            opacity: 0.45,
            "line-color": "#90a4ae",
            "target-arrow-color": "#90a4ae",
            "target-arrow-shape": "triangle",
            "arrow-scale": 0.7,
            "curve-style": "haystack",
            "haystack-radius": 0,
            // No edge labels by default. With ~2500 edges every caption was
            // drawn at once, which buried the nodes underneath a wall of text.
            label: "",
          },
        },
        { selector: "edge.faded", style: { opacity: 0.04 } },
        {
          selector: "edge.hl",
          style: {
            width: 2.5,
            opacity: 1,
            "line-color": "#1565c0",
            "target-arrow-color": "#1565c0",
            "curve-style": "bezier",
            label: (e) => e.data("label") || e.data("kind").replace(/_/g, " "),
            "font-size": 9,
            color: "#1565c0",
            "text-background-color": "#fff",
            "text-background-opacity": 0.9,
            "text-background-padding": 2,
            "text-rotation": "autorotate",
          },
        },
      ],
      layout: { name: "grid" },
      // 0.2 was so slow the wheel scrolled the page instead of zooming.
      wheelSensitivity: 0.6,
      minZoom: 0.05,
      maxZoom: 4,
    });

    // Reveal captions only when there is room for them: always for the big
    // hubs, and for everything else once the user has zoomed in.
    applyLabelVisibility = () => {
      const z = cy.zoom();
      cy.batch(() => {
        cy.nodes().forEach((n) => {
          const deg = n.degree(false);
          const show = z > 1.1 || (z > 0.55 && deg >= 4) || deg >= 12 || n.selected();
          n.scratch("_showLabel", show);
        });
      });
      cy.style().update();
    };
    cy.on("zoom", debounceRaf(() => applyLabelVisibility()));

    // Hover isolates a node's neighbourhood, which is the only practical way to
    // read a dense region without filtering it down first.
    cy.on("mouseover", "node", (evt) => {
      const n = evt.target;
      const nb = n.closedNeighborhood();
      cy.elements().not(nb).addClass("faded");
      nb.removeClass("faded");
      nb.edges().addClass("hl");
      n.addClass("hl");
    });
    cy.on("mouseout", "node", () => {
      cy.elements().removeClass("faded hl");
    });

    cy.on("tap", "node", (evt) => {
      const data = evt.target.data();
      if (data.kind === "sample") {
        const accession = (data.props && data.props.accession) || data.id;
        DETAIL.openSample(accession);
        MAP.panToAccession(accession);
      } else {
        DETAIL.openNode(data);
      }
    });
  }

  function update(elementsResponse) {
    const newNodes = (elementsResponse.nodes || []).map((n) => n.data);
    const newEdges = (elementsResponse.edges || []).map((e) => e.data);

    document.getElementById("graph-empty").hidden = newNodes.length > 0;

    const newNodeIds = new Set(newNodes.map((n) => n.id));
    const existingIds = new Set(cy.nodes().map((n) => n.id()));

    let changed = false;

    // Remove nodes (and their edges) that are no longer present.
    cy.nodes().forEach((n) => {
      if (!newNodeIds.has(n.id())) {
        n.remove();
        changed = true;
      }
    });

    // Add / update nodes.
    const toAdd = [];
    newNodes.forEach((data) => {
      if (existingIds.has(data.id)) {
        cy.getElementById(data.id).data(data);
      } else {
        toAdd.push({ data });
        changed = true;
      }
    });

    // Edges: drop stale, add new. Cytoscape's own add() already refuses an
    // edge referencing a missing endpoint, but the API prunes those first.
    const newEdgeIds = new Set(newEdges.map((e) => e.id));
    cy.edges().forEach((e) => {
      if (!newEdgeIds.has(e.id())) {
        e.remove();
        changed = true;
      }
    });
    const existingEdgeIds = new Set(cy.edges().map((e) => e.id()));
    const edgesToAdd = [];
    newEdges.forEach((data) => {
      if (!existingEdgeIds.has(data.id)) {
        edgesToAdd.push({ data });
        changed = true;
      }
    });

    if (toAdd.length) cy.add(toAdd);
    if (edgesToAdd.length) {
      // Only add edges whose endpoints actually exist in the graph now.
      const liveIds = new Set(cy.nodes().map((n) => n.id()));
      cy.add(edgesToAdd.filter((e) => liveIds.has(e.data.source) && liveIds.has(e.data.target)));
    }

    if (changed) {
      const n = cy.nodes().length;
      // Stock cose packs everything into one dense ball. Pushing repulsion up
      // and edge length out separates the per-country clusters enough to read;
      // the iteration count scales down on big graphs to keep it responsive.
      const layout = cy.layout({
        name: "cose",
        animate: false,
        fit: true,
        padding: 40,
        randomize: true,
        // Repulsion high and gravity low: gravity is what was crushing the
        // whole graph back into a single ball however far apart cose pushed it.
        nodeRepulsion: 45000,
        idealEdgeLength: n > 400 ? 140 : 90,
        edgeElasticity: 40,
        gravity: 4,
        gravityRange: 6,
        nestingFactor: 1.2,
        componentSpacing: 220,
        nodeOverlap: 28,
        numIter: n > 500 ? 1200 : 2000,
        coolingFactor: 0.97,
        initialTemp: 260,
      });
      layout.one("layoutstop", () => applyLabelVisibility());
      layout.run();
    } else {
      applyLabelVisibility();
    }

    updateScaleHint();
  }

  /** Tell the user what makes a very large graph readable, instead of showing dust. */
  function updateScaleHint() {
    const hint = document.getElementById("graph-scale-hint");
    if (!hint) return;
    const n = cy.nodes().length;
    if (n <= 180) {
      hint.hidden = true;
      return;
    }
    hint.textContent =
      `${n} nodes, ${cy.edges().length} edges. Select an area on the map, ` +
      `pick a country, or raise the confidence slider to narrow it down — ` +
      `zoom in for labels, hover a node to isolate its links.`;
    hint.hidden = false;
  }

  return {
    get cy() { return cy; },
    init,
    update,
    refreshLabels: () => applyLabelVisibility(),
    setLoading: (v) => { document.getElementById("graph-loading").hidden = !v; },
  };
})();

/* ------------------------------------------------------------------------- *
 * 7. DETAIL PANEL
 * ------------------------------------------------------------------------- */

const DETAIL = (() => {
  function open() {
    document.getElementById("detail-panel").hidden = false;
  }
  function close() {
    document.getElementById("detail-panel").hidden = true;
  }

  function sourceTag(source, dict) {
    const info = dict[source];
    const label = info ? info.label : source;
    const color = info ? info.color : "#666";
    return el("span", { class: "detail-tag", style: `background:${color}`, text: label });
  }

  async function openSample(accession) {
    if (!accession) return;
    open();
    const content = document.getElementById("detail-content");
    clear(content);
    content.appendChild(el("p", { text: "Loading…" }));
    try {
      const signal = API.abortInFlight();
      const resp = await API.sample(accession, signal);
      if (resp.error) {
        clear(content);
        content.appendChild(el("p", { text: `Sample not found: ${accession}` }));
        return;
      }
      renderSample(resp.sample, resp.graph);
    } catch (err) {
      if (err.name === "AbortError") return;
      clear(content);
      content.appendChild(el("p", { text: `Failed to load sample: ${err.message}` }));
    }
  }

  function renderSample(sample, graph) {
    const content = document.getElementById("detail-content");
    clear(content);
    const loc = sample.location || {};

    content.appendChild(el("h2", { text: sample.accession }));
    content.appendChild(el("p", { text: sample.title || "(no title)" }));

    const table = el("table", {}, [
      row("Organism", sample.organism),
      row("Collection date", sample.collection_date),
      row("Country", loc.country),
      row("Region", loc.region),
      row("Precision", loc.precision),
      row("Coordinates", loc.has_coords ? `${loc.lat}, ${loc.lon}` : "—"),
    ]);
    content.appendChild(table);

    if (sample.source_url) {
      const link = el("a", {
        class: "detail-link",
        href: sample.source_url,
        target: "_blank",
        rel: "noopener noreferrer",
        text: "Source record",
      });
      content.appendChild(el("p", {}, [link]));
    }

    if (sample.ontology_terms && sample.ontology_terms.length) {
      content.appendChild(el("h2", { text: `Ontology terms (${sample.ontology_terms.length})` }));
      const wrap = el("div", {});
      sample.ontology_terms.forEach((t) => {
        const tag = sourceTag(t.source, STATE.legend.ontology_sources);
        tag.title = `${t.curie || t.uri} — ${t.label || ""} (confidence ${t.confidence})`;
        wrap.appendChild(tag);
      });
      content.appendChild(wrap);
    }

    if (sample.archives && sample.archives.length) {
      content.appendChild(el("h2", { text: "Archives" }));
      const wrap = el("div", {});
      sample.archives.forEach((a) => {
        const link = el("a", { class: "detail-link", href: a.url, target: "_blank", rel: "noopener noreferrer" });
        const tag = sourceTag(a.source, STATE.legend.archive_sources);
        link.appendChild(tag);
        link.appendChild(document.createTextNode(" " + a.accession));
        wrap.appendChild(link);
        wrap.appendChild(el("br"));
      });
      content.appendChild(wrap);
    }

    if (sample.attributes && Object.keys(sample.attributes).length) {
      content.appendChild(el("h2", { text: "Attributes" }));
      const attrTable = el("table", {});
      Object.entries(sample.attributes).forEach(([k, v]) => attrTable.appendChild(row(k, v)));
      content.appendChild(attrTable);
    }

    if (graph && graph.nodes && graph.nodes.length) {
      content.appendChild(el("h2", { text: `Linked graph nodes (${graph.nodes.length})` }));
      const list = el("ul", {});
      graph.nodes.forEach((n) => {
        const data = n.data || n;
        if (data.id === sample.accession) return;
        list.appendChild(el("li", { text: `${data.label} (${data.kind})` }));
      });
      content.appendChild(list);
    }
  }

  function openNode(data) {
    open();
    const content = document.getElementById("detail-content");
    clear(content);
    content.appendChild(el("h2", { text: data.label || data.id }));
    content.appendChild(el("p", { text: `Kind: ${data.kind}` }));

    const props = data.props || {};
    if (Object.keys(props).length) {
      const table = el("table", {});
      Object.entries(props).forEach(([k, v]) => table.appendChild(row(k, v)));
      content.appendChild(table);
    }

    if (GRAPH.cy) {
      const cyNode = GRAPH.cy.getElementById(data.id);
      if (cyNode && cyNode.length) {
        const neighbours = cyNode.neighborhood("node");
        if (neighbours.length) {
          content.appendChild(el("h2", { text: `Connected (${neighbours.length})` }));
          const list = el("ul", {});
          neighbours.forEach((n) => {
            const nd = n.data();
            list.appendChild(el("li", { text: `${nd.label} (${nd.kind})` }));
          });
          content.appendChild(list);
        }
      }
    }
  }

  function row(label, value) {
    return el("tr", {}, [el("td", { text: label }), el("td", { text: fmt(value) })]);
  }

  document.getElementById("detail-close").addEventListener("click", close);

  return { openSample, openNode, close };
})();

/* ------------------------------------------------------------------------- *
 * 8. STATS
 * ------------------------------------------------------------------------- */

const STATS = {
  render(stats) {
    document.getElementById("stat-samples").textContent = stats.samples;
    document.getElementById("stat-coords").textContent = stats.with_coords;
    document.getElementById("stat-countries").textContent = stats.countries;
    document.getElementById("stat-terms").textContent = stats.ontology_terms;
    document.getElementById("stat-nodes").textContent = stats.graph_nodes;
    document.getElementById("stat-edges").textContent = stats.graph_edges;

    STATE.serverLoaded.samples_loaded = stats.samples_loaded;
    STATE.serverLoaded.graph_loaded = stats.graph_loaded;
    document.getElementById("pipeline-banner").hidden = !!stats.samples_loaded;
  },
};

/* ------------------------------------------------------------------------- *
 * 4. FILTERS
 * ------------------------------------------------------------------------- */

const FILTERS = (() => {
  function populateSelect(select, values, placeholder) {
    clear(select);
    select.appendChild(el("option", { value: "", text: placeholder }));
    values.forEach((v) => select.appendChild(el("option", { value: v, text: v })));
  }

  function populateChecks(container, values, colorLookup) {
    clear(container);
    values.forEach((v) => {
      const id = `chk-${container.id}-${v}`;
      const label = el("label", { for: id });
      const input = el("input", { type: "checkbox", id, value: v });
      label.appendChild(input);
      if (colorLookup && colorLookup[v]) {
        label.appendChild(el("span", { class: "check-swatch", style: `background:${colorLookup[v].color}` }));
        label.appendChild(document.createTextNode(" " + colorLookup[v].label));
      } else {
        label.appendChild(document.createTextNode(" " + v));
      }
      container.appendChild(label);
    });
  }

  function populateLegendNodeKinds() {
    const container = document.getElementById("legend-node-kinds");
    clear(container);
    STATE.legend.node_kinds.forEach((kind) => {
      const style = DEFAULT_KIND_STYLE[kind] || FALLBACK_KIND_STYLE;
      const row = el("div", { class: "legend-row" });
      row.appendChild(el("span", { class: "legend-swatch", style: `background:${style.color}` }));
      row.appendChild(document.createTextNode(kind));
      container.appendChild(row);
    });
  }

  async function loadFacetsAndLegend() {
    const [facets, legend] = await Promise.all([API.facets(), API.legend()]);
    STATE.facets = facets;
    STATE.legend = legend;

    populateSelect(document.getElementById("country-select"), facets.countries, "All countries");
    populateSelect(document.getElementById("region-select"), facets.regions, "All regions");
    populateChecks(document.getElementById("precision-checks"), facets.precisions);
    populateChecks(document.getElementById("ontology-checks"), facets.ontologies, legend.ontology_sources);
    populateChecks(document.getElementById("archive-checks"), facets.archives, legend.archive_sources);
    populateLegendNodeKinds();
  }

  function readCheckboxSet(container) {
    const set = new Set();
    container.querySelectorAll("input[type=checkbox]:checked").forEach((cb) => set.add(cb.value));
    return set;
  }

  function wireControls() {
    const searchInput = document.getElementById("search-input");
    searchInput.addEventListener("input", () => {
      STATE.filters.search = searchInput.value.trim().toLowerCase();
      scheduleRefresh();
    });

    const countrySelect = document.getElementById("country-select");
    countrySelect.addEventListener("change", () => {
      STATE.filters.country = countrySelect.value;
      scheduleRefresh(0);
    });

    const regionSelect = document.getElementById("region-select");
    regionSelect.addEventListener("change", () => {
      STATE.filters.region = regionSelect.value;
      scheduleRefresh(0);
    });

    document.getElementById("precision-checks").addEventListener("change", () => {
      STATE.filters.precision = readCheckboxSet(document.getElementById("precision-checks"));
      scheduleRefresh(0);
    });
    document.getElementById("ontology-checks").addEventListener("change", () => {
      STATE.filters.ontology = readCheckboxSet(document.getElementById("ontology-checks"));
      scheduleRefresh(0);
    });
    document.getElementById("archive-checks").addEventListener("change", () => {
      STATE.filters.archive = readCheckboxSet(document.getElementById("archive-checks"));
      scheduleRefresh(0);
    });

    const slider = document.getElementById("confidence-slider");
    const sliderVal = document.getElementById("confidence-value");
    slider.addEventListener("input", () => {
      STATE.filters.min_confidence = parseFloat(slider.value);
      sliderVal.textContent = STATE.filters.min_confidence.toFixed(2);
      scheduleRefresh();
    });

    const coordsToggle = document.getElementById("has-coords-toggle");
    coordsToggle.addEventListener("change", () => {
      STATE.filters.has_coords = coordsToggle.checked ? true : null;
      scheduleRefresh(0);
    });

    const archivesToggle = document.getElementById("show-archives-toggle");
    archivesToggle.addEventListener("change", () => {
      STATE.filters.show_archives = archivesToggle.checked;
      scheduleRefresh(0);
    });

    document.getElementById("btn-select-box").addEventListener("click", () => {
      MAP.enterSelectMode(STATE.selectMode === "box" ? null : "box");
    });
    document.getElementById("btn-select-radius").addEventListener("click", () => {
      MAP.enterSelectMode(STATE.selectMode === "radius" ? null : "radius");
    });

    document.getElementById("btn-reset-filters").addEventListener("click", resetFilters);
    document.getElementById("btn-clear-selection").addEventListener("click", () => MAP.clearSelection());

    document.getElementById("btn-reload").addEventListener("click", async () => {
      const statusEl = document.getElementById("load-status");
      statusEl.textContent = "Reloading…";
      try {
        const result = await API.reload();
        statusEl.textContent = result.error
          ? `Reload error: ${result.error}`
          : `Reloaded: ${result.samples} samples, ${result.nodes} nodes, ${result.edges} edges`;
        await loadFacetsAndLegend();
        await refreshData();
      } catch (err) {
        statusEl.textContent = `Reload failed: ${err.message}`;
      }
    });
  }

  function resetFilters() {
    STATE.filters.search = "";
    STATE.filters.country = "";
    STATE.filters.region = "";
    STATE.filters.precision = new Set();
    STATE.filters.ontology = new Set();
    STATE.filters.archive = new Set();
    STATE.filters.min_confidence = 0.7;
    STATE.filters.has_coords = null;
    STATE.filters.show_archives = false;
    STATE.filters.bbox = null;
    STATE.filters.center = null;
    STATE.filters.radius_km = null;

    document.getElementById("search-input").value = "";
    document.getElementById("country-select").value = "";
    document.getElementById("region-select").value = "";
    document.querySelectorAll("#precision-checks input, #ontology-checks input, #archive-checks input")
      .forEach((cb) => { cb.checked = false; });
    document.getElementById("confidence-slider").value = "0.7";
    document.getElementById("confidence-value").textContent = "0.70";
    document.getElementById("has-coords-toggle").checked = false;
    document.getElementById("show-archives-toggle").checked = false;
    document.getElementById("selection-summary").textContent = "";
    MAP.clearSelection();

    scheduleRefresh(0);
  }

  const debouncedRefresh = debounce(() => refreshData(), 250);

  function scheduleRefresh(delay) {
    if (delay === 0) {
      refreshData();
    } else {
      debouncedRefresh();
    }
  }

  return { loadFacetsAndLegend, wireControls, scheduleRefresh, resetFilters };
})();

/* ------------------------------------------------------------------------- *
 * Cross-cutting: single refresh cycle driving map + graph + stats
 * ------------------------------------------------------------------------- */

async function refreshData() {
  const signal = API.abortInFlight();
  MAP.setLoading(true);
  GRAPH.setLoading(true);
  document.getElementById("fetch-error-banner").hidden = true;

  try {
    const [geojson, graph, stats] = await Promise.all([
      API.geojson(STATE.filters, signal),
      API.graph(STATE.filters, signal),
      API.stats(STATE.filters, signal),
    ]);
    MAP.renderGeojson(geojson);
    GRAPH.update(graph);
    STATS.render(stats);
  } catch (err) {
    if (err.name === "AbortError") return; // superseded by a newer request
    document.getElementById("fetch-error-banner").hidden = false;
    document.getElementById("fetch-error-text").textContent = `Could not load data: ${err.message}`;
  } finally {
    MAP.setLoading(false);
    GRAPH.setLoading(false);
  }
}

/* ------------------------------------------------------------------------- *
 * 10. INIT
 * ------------------------------------------------------------------------- */

document.addEventListener("DOMContentLoaded", async () => {
  MAP.init();
  GRAPH.init();
  LAYOUT.init();
  FILTERS.wireControls();

  try {
    await FILTERS.loadFacetsAndLegend();
  } catch (err) {
    document.getElementById("fetch-error-banner").hidden = false;
    document.getElementById("fetch-error-text").textContent = `Could not load facets/legend: ${err.message}`;
  }

  await refreshData();
});
