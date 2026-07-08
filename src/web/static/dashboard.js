/**
 * SCREEN-1 controller — vanilla JS, no framework, no build step (the design
 * export *is* plain HTML/CSS/JS, so this is the closest 1:1 port; plan
 * §"Stack notes"). One data path serves both the initial load and every
 * filter change: setState -> Loading -> fetch the BFF -> Populated/Empty/Error.
 *
 * XSS-safe rendering (the load-bearing frontend security control, AC13):
 * every dynamic value is written via `textContent` / `createElement` —
 * never `innerHTML`, never an inline event handler, never `eval`. This is
 * the DOM-sink defense behind the page's strict CSP (`script-src 'self'`,
 * no `unsafe-inline`/`unsafe-eval`).
 */
(function () {
  "use strict";

  const SKELETON_ROW_COUNT = 10;

  /** @type {{customer: string, metric: string, granularity: string}} */
  const state = { customer: null, metric: null, granularity: "hour" };

  const elements = {
    envBadge: document.getElementById("env-badge"),
    envBadgeLabel: document.getElementById("env-badge-label"),
    customerSelect: document.getElementById("customer-select"),
    metricSelect: document.getElementById("metric-select"),
    segmentedControl: document.getElementById("window-segmented-control"),
    loadingState: document.getElementById("loading-state"),
    populatedState: document.getElementById("populated-state"),
    emptyState: document.getElementById("empty-state"),
    errorState: document.getElementById("error-state"),
    skeletonRows: document.getElementById("skeleton-rows"),
    statNumber: document.getElementById("stat-number"),
    statDeltaPill: document.getElementById("stat-delta-pill"),
    statDeltaText: document.getElementById("stat-delta-text"),
    statMetricChip: document.getElementById("stat-metric-chip"),
    statWindowNoun: document.getElementById("stat-window-noun"),
    usageTableBody: document.getElementById("usage-table-body"),
    emptyStateTitle: document.getElementById("empty-state-title"),
    retryButton: document.getElementById("retry-button"),
  };

  /** Clear every child of `node` without ever touching `innerHTML`. */
  function clearChildren(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  /** Show exactly one of the four state panels, hiding the rest. */
  function showPanel(panelToShow) {
    for (const panel of [
      elements.loadingState,
      elements.populatedState,
      elements.emptyState,
      elements.errorState,
    ]) {
      panel.hidden = panel !== panelToShow;
    }
  }

  /** Render the layout-mirroring skeleton: a greyed stat block + exactly
   * `SKELETON_ROW_COUNT` greyed rows, matching the populated table so the
   * layout never shifts once data arrives (design-spec §5, plan AC6). */
  function renderSkeleton() {
    clearChildren(elements.skeletonRows);
    for (let index = 0; index < SKELETON_ROW_COUNT; index += 1) {
      const row = document.createElement("div");
      row.className = "skeleton-row";
      const block = document.createElement("div");
      block.className = "skeleton-block";
      row.appendChild(block);
      elements.skeletonRows.appendChild(row);
    }
  }

  /** Apply a delta pill's direction-driven class + text (shared by the stat
   * card and every table row) — text is always written via `textContent`. */
  function applyDeltaPill(pillElement, textElement, deltaText, deltaDirection) {
    pillElement.classList.remove("delta-pill--up", "delta-pill--down", "delta-pill--neutral");
    pillElement.classList.add(`delta-pill--${deltaDirection}`);
    textElement.textContent = deltaText;
  }

  /** Render the populated state: the CMP-5 stat card + the CMP-7 table. */
  function renderPopulated(series) {
    elements.statNumber.textContent = series.current.quantity;
    applyDeltaPill(elements.statDeltaPill, elements.statDeltaText, series.current.delta_text, series.current.delta_direction);
    elements.statMetricChip.textContent = series.current.metric;
    elements.statWindowNoun.textContent = series.current.window_label;

    clearChildren(elements.usageTableBody);
    for (const row of series.rows) {
      const tableRow = document.createElement("tr");

      const windowStartCell = document.createElement("td");
      windowStartCell.className = "window-start-cell";
      windowStartCell.textContent = row.window_start;

      const metricCell = document.createElement("td");
      metricCell.className = "metric-cell";
      metricCell.textContent = row.metric;

      const quantityCell = document.createElement("td");
      quantityCell.className = "quantity-cell";
      quantityCell.textContent = row.quantity;

      const deltaCell = document.createElement("td");
      deltaCell.className = "delta-cell";
      const deltaPill = document.createElement("span");
      deltaPill.className = "delta-pill delta-pill--row";
      const deltaText = document.createElement("span");
      applyDeltaPill(deltaPill, deltaText, row.delta_text, row.delta_direction);
      deltaPill.appendChild(deltaText);
      deltaCell.appendChild(deltaPill);

      tableRow.appendChild(windowStartCell);
      tableRow.appendChild(metricCell);
      tableRow.appendChild(quantityCell);
      tableRow.appendChild(deltaCell);
      elements.usageTableBody.appendChild(tableRow);
    }

    showPanel(elements.populatedState);
  }

  /** Render the empty state (CMP-9): triggered by "no usage rows for this
   * selection" (all 11 windows zero), never a hardcoded customer. */
  function renderEmpty() {
    elements.emptyStateTitle.textContent = `No events yet for ${state.customer}`;
    showPanel(elements.emptyState);
  }

  /** Render the generic error card (reuses CMP-9's structure). No raw
   * error/stack/SQL text is ever shown — only this fixed, generic copy. */
  function renderError() {
    showPanel(elements.errorState);
  }

  /** Build the CMP-2 environment badge from real deployment config, not a
   * hardcoded design-time prop (AC20). */
  function renderEnvironmentBadge(environment) {
    const isProd = environment === "prod";
    elements.envBadge.classList.remove("env-badge--prod", "env-badge--staging");
    elements.envBadge.classList.add(isProd ? "env-badge--prod" : "env-badge--staging");
    elements.envBadgeLabel.textContent = environment;
  }

  /** Populate a <select>'s options from the config allowlist, defaulting to
   * the first entry so the BFF's validation allowlist and the dropdown can
   * never drift apart (both are read from the same GET /dashboard/api/config). */
  function populateSelectOptions(selectElement, values) {
    clearChildren(selectElement);
    for (const value of values) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      selectElement.appendChild(option);
    }
  }

  /** Mark the segment matching `state.granularity` as active; leaves the
   * disabled `month` segment's affordance untouched. */
  function syncActiveSegment() {
    const segmentButtons = elements.segmentedControl.querySelectorAll(".segment-button");
    for (const button of segmentButtons) {
      const isActive = button.dataset.granularity === state.granularity;
      button.classList.toggle("segment-button--active", isActive);
    }
  }

  /** Fetch the current selection's series from the same-origin BFF and
   * render the resulting state — the one data path for load + every filter
   * change (plan §"Client controller"). */
  async function loadSeries() {
    showPanel(elements.loadingState);
    renderSkeleton();

    const params = new URLSearchParams({
      customer_id: state.customer,
      metric: state.metric,
      granularity: state.granularity,
    });

    try {
      const response = await fetch(`/dashboard/api/usage-series?${params.toString()}`, {
        method: "GET",
        credentials: "same-origin",
      });
      if (!response.ok) {
        renderError();
        return;
      }
      const series = await response.json();
      if (series.state === "empty") {
        renderEmpty();
      } else {
        renderPopulated(series);
      }
    } catch (error) {
      renderError();
    }
  }

  function onCustomerChange(event) {
    state.customer = event.target.value;
    loadSeries();
  }

  function onMetricChange(event) {
    state.metric = event.target.value;
    loadSeries();
  }

  function onSegmentClick(event) {
    const button = event.target.closest(".segment-button");
    if (!button || button.getAttribute("aria-disabled") === "true") {
      return; // the disabled `month` segment renders for visual fidelity only (Q1)
    }
    state.granularity = button.dataset.granularity;
    syncActiveSegment();
    loadSeries();
  }

  function onRetryClick() {
    loadSeries();
  }

  /** Fetch the page's single source of truth (allowlists + environment),
   * build the dropdowns/badge from it, then load the initial series. */
  async function initialize() {
    elements.customerSelect.addEventListener("change", onCustomerChange);
    elements.metricSelect.addEventListener("change", onMetricChange);
    elements.segmentedControl.addEventListener("click", onSegmentClick);
    elements.retryButton.addEventListener("click", onRetryClick);

    try {
      const configResponse = await fetch("/dashboard/api/config", { credentials: "same-origin" });
      if (!configResponse.ok) {
        renderError();
        return;
      }
      const config = await configResponse.json();

      renderEnvironmentBadge(config.environment);
      populateSelectOptions(elements.customerSelect, config.customers);
      populateSelectOptions(elements.metricSelect, config.metrics);

      state.customer = config.customers[0];
      state.metric = config.metrics[0];
      syncActiveSegment();

      await loadSeries();
    } catch (error) {
      renderError();
    }
  }

  document.addEventListener("DOMContentLoaded", initialize);
})();
