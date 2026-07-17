const state = { scenarios: [], activeId: "approval", step: 0 };

const elements = {
  title: document.querySelector("#scenario-title"),
  list: document.querySelector("#step-list"),
  detail: document.querySelector("#step-detail"),
  counter: document.querySelector("#step-counter"),
  previous: document.querySelector("#previous-step"),
  next: document.querySelector("#next-step"),
  tabs: [...document.querySelectorAll("[data-scenario]")],
};

function activeScenario() {
  return state.scenarios.find((scenario) => scenario.id === state.activeId);
}

function escapeHtml(value) {
  return value.replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}

function render() {
  const scenario = activeScenario();
  if (!scenario) return;
  const step = scenario.steps[state.step];

  elements.title.textContent = scenario.title;
  elements.counter.textContent = `STEP ${state.step + 1} / ${scenario.steps.length}`;
  elements.previous.disabled = state.step === 0;
  elements.next.disabled = state.step === scenario.steps.length - 1;
  elements.next.innerHTML = state.step === scenario.steps.length - 1
    ? "场景完成 <span>✓</span>"
    : "下一步 <span>→</span>";

  elements.list.innerHTML = scenario.steps.map((item, index) => `
    <button class="step ${index === state.step ? "active" : ""} ${index < state.step ? "visited" : ""}"
      type="button" data-step="${index}" aria-current="${index === state.step ? "step" : "false"}">
      <span>${String(index + 1).padStart(2, "0")}</span>
      <span><strong>${escapeHtml(item.label)}</strong><small>${escapeHtml(item.state)}</small></span>
    </button>`).join("");

  elements.detail.innerHTML = `
    <div class="state-line"><span class="state-dot ${step.tone}"></span>${escapeHtml(step.state)}</div>
    <h3>${escapeHtml(step.summary)}</h3>
    <p>${escapeHtml(step.detail)}</p>
    <pre><code>${escapeHtml(step.code)}</code></pre>
    <div class="outcome"><span>SCENARIO OUTCOME</span><strong>${escapeHtml(scenario.outcome)}</strong></div>`;

  elements.list.querySelectorAll("[data-step]").forEach((button) => {
    button.addEventListener("click", () => {
      state.step = Number(button.dataset.step);
      render();
    });
  });
}

elements.tabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    state.activeId = tab.dataset.scenario;
    state.step = 0;
    elements.tabs.forEach((item) => item.setAttribute("aria-selected", String(item === tab)));
    render();
  });
});

elements.previous.addEventListener("click", () => {
  state.step = Math.max(0, state.step - 1);
  render();
});

elements.next.addEventListener("click", () => {
  const scenario = activeScenario();
  state.step = Math.min(scenario.steps.length - 1, state.step + 1);
  render();
});

fetch("demo-data.json")
  .then((response) => {
    if (!response.ok) throw new Error(`demo data returned ${response.status}`);
    return response.json();
  })
  .then((data) => {
    state.scenarios = data.scenarios;
    render();
  })
  .catch((error) => {
    elements.detail.innerHTML = `<p class="load-error">演示数据加载失败：${escapeHtml(error.message)}</p>`;
  });
