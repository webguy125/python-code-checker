const STORAGE_KEY = "python-code-checker:last-input";
const API_ENDPOINT = "https://python-code-checker-api.webguy125.workers.dev/audit";
const SAMPLE_CODE = `import math

def area(radius):
    return math.pi * radius * radius

print(area(5))
`;
const OUTPUT_PLACEHOLDER_HTML = `
  <div class="output-placeholder">
    <div class="output-placeholder-group">
      <p class="card-kicker">Behavior</p>
      <h3>What this page does</h3>
      <ul class="output-placeholder-list">
        <li>Sends code to the hosted Worker endpoint at <code>/audit</code>.</li>
        <li>Renders cleaned code and normalized issue details.</li>
        <li>Optionally runs the repaired script in a restricted mock sandbox and shows what executed.</li>
        <li>Persists the last input locally so refreshes do not lose work.</li>
      </ul>
    </div>
    <div class="output-placeholder-group">
      <p class="card-kicker">Shortcuts</p>
      <h3>Fast interaction</h3>
      <ul class="output-placeholder-list">
        <li><code>Ctrl</code> + <code>Enter</code> runs the audit.</li>
        <li>Copy cleaned code with one click.</li>
        <li>Errors are shown inline and keep the workspace usable.</li>
      </ul>
    </div>
  </div>
`;

const elements = {
  codeInput: document.getElementById("code-input"),
  styleMode: document.getElementById("style-mode"),
  runMock: document.getElementById("run-mock"),
  runAudit: document.getElementById("run-audit"),
  loadSample: document.getElementById("load-sample"),
  clearInput: document.getElementById("clear-input"),
  copyOutput: document.getElementById("copy-output"),
  cleanedOutput: document.getElementById("cleaned-output"),
  issuesList: document.getElementById("issues-list"),
  issuesEmpty: document.getElementById("issues-empty"),
  resultSummary: document.getElementById("result-summary"),
  inputStatus: document.getElementById("input-status"),
  mockSummary: document.getElementById("mock-summary"),
  mockEmpty: document.getElementById("mock-empty"),
  mockResults: document.getElementById("mock-results"),
  mockStdout: document.getElementById("mock-stdout"),
  mockFunctions: document.getElementById("mock-functions"),
  mockCalledFunctions: document.getElementById("mock-called-functions"),
  mockSampleCalls: document.getElementById("mock-sample-calls"),
  mockErrorSection: document.getElementById("mock-error-section"),
  mockError: document.getElementById("mock-error"),
};

const state = {
  abortController: null,
  lastResultText: "",
  hasFreshResult: false,
};

function setStatus(target, text, tone = "neutral") {
  target.textContent = text;
  target.classList.remove("neutral", "ok", "warn", "error");
  target.classList.add(tone);
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function formatLocation(issue) {
  const line = issue.line ?? issue.lineno ?? issue.row ?? issue.line_number;
  const column = issue.column ?? issue.col ?? issue.column_number;
  if (line !== undefined && line !== null && column !== undefined && column !== null) return `Line ${line}, col ${column}`;
  if (line !== undefined && line !== null) return `Line ${line}`;
  if (column !== undefined && column !== null) return `Col ${column}`;
  return "";
}

function normalizeSeverity(issue) {
  const raw = String(issue.severity ?? issue.level ?? issue.kind ?? "info").toLowerCase();
  if (["error", "critical", "high"].includes(raw)) return "high";
  if (["warning", "warn", "medium", "moderate"].includes(raw)) return "medium";
  if (["info", "note", "low"].includes(raw)) return "low";
  return "low";
}

function normalizeIssue(issue, index) {
  if (typeof issue === "string") {
    return {
      title: `Issue ${index + 1}`,
      message: issue,
      severity: "low",
      location: "",
      rule: "",
      snippet: "",
    };
  }

  const message = issue.message ?? issue.detail ?? issue.description ?? issue.text ?? JSON.stringify(issue);
  const title = issue.title ?? issue.name ?? issue.rule ?? `Issue ${index + 1}`;
  const rule = issue.rule ?? issue.code ?? issue.type ?? "";
  const confidence = issue.confidence ?? "medium";
  const snippet = issue.snippet ?? issue.line_text ?? issue.source ?? "";

  return {
    title,
    message,
    severity: normalizeSeverity(issue),
    location: formatLocation(issue),
    rule,
    confidence,
    snippet,
  };
}

function unwrapPayload(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return payload;
  const nested = payload.data ?? payload.audit ?? payload.result ?? payload.payload;
  if (!nested || typeof nested !== "object" || Array.isArray(nested)) return payload;
  return { ...payload, ...nested };
}

function extractCleanedCode(payload) {
  const source = unwrapPayload(payload);
  const candidates = [
    source?.cleaned_code,
    source?.cleanedCode,
    source?.fixed_code,
    source?.fixedCode,
    source?.output,
    source?.code,
  ];
  for (const candidate of candidates) {
    if (typeof candidate === "string" && candidate.trim()) return candidate;
  }
  if (typeof source === "string" && source.trim()) return source;
  return "No cleaned code was returned.";
}

function extractIssues(payload) {
  const source = unwrapPayload(payload);
  const candidates = [
    source?.issues_found,
    source?.issues,
    source?.warnings,
    source?.findings,
    source?.errors,
    source?.messages,
  ];
  for (const candidate of candidates) {
    if (Array.isArray(candidate)) return candidate.map(normalizeIssue);
  }
  return [];
}

function extractMockTest(payload) {
  const source = unwrapPayload(payload);
  if (!source?.mock_test || typeof source.mock_test !== "object") return null;
  return source.mock_test;
}

function extractSummary(payload, issues) {
  const source = unwrapPayload(payload);
  if (source?.error?.message && typeof source.error.message === "string") return source.error.message.trim();
  if (typeof source?.summary === "string" && source.summary.trim()) return source.summary.trim();
  if (typeof source?.message === "string" && source.message.trim()) return source.message.trim();
  if (typeof source?.detail === "string" && source.detail.trim()) return source.detail.trim();
  if (typeof source?.status === "string" && source.status.trim()) return source.status.trim();
  if (issues.length === 0) return "No issues detected.";
  return `${issues.length} issue${issues.length === 1 ? "" : "s"} detected.`;
}

function extractErrorDetails(payload) {
  const source = unwrapPayload(payload);
  const error = source?.error;
  if (!error || typeof error !== "object") return null;

  const message = typeof error.message === "string" && error.message.trim()
    ? error.message.trim()
    : "The audit could not be completed.";

  const line = error.line ?? error.lineno;
  const column = error.column ?? error.offset;
  const text = typeof error.text === "string" ? error.text : "";

  return {
    message,
    line,
    column,
    text,
    type: typeof error.type === "string" ? error.type : "error",
  };
}

function renderOutputPlaceholder() {
  elements.cleanedOutput.innerHTML = OUTPUT_PLACEHOLDER_HTML;
}

function renderOutputCode(code) {
  elements.cleanedOutput.innerHTML = `<pre><code>${escapeHtml(code)}</code></pre>`;
}

function renderIssues(issues) {
  if (!issues.length) {
    elements.issuesList.hidden = true;
    elements.issuesEmpty.hidden = false;
    elements.issuesEmpty.textContent = "No issues were reported by the audit.";
    return;
  }

  elements.issuesList.hidden = false;
  elements.issuesEmpty.hidden = true;
  elements.issuesList.innerHTML = issues
    .map((issue, index) => {
      const locationText = issue.location || "No location";
      const detailParts = [
        issue.message,
        issue.rule ? `Rule: ${issue.rule}` : "",
        issue.confidence ? `Confidence: ${issue.confidence}` : "",
        issue.snippet ? `Snippet: ${issue.snippet}` : "",
      ].filter(Boolean);
      const detailText = detailParts.join(" | ");
      return `
        <article class="issue-row ${issue.severity}" data-issue-row title="${escapeHtml(detailText)}">
          <button type="button" class="issue-row-trigger" aria-expanded="false" data-issue-trigger="${index}">
            <span class="issue-row-main">
              <span class="issue-row-title">${escapeHtml(issue.title)}</span>
              <span class="issue-row-location">${escapeHtml(locationText)}</span>
            </span>
            <span class="issue-row-side">
              <span class="issue-row-rule">${escapeHtml(issue.rule || issue.severity)}</span>
            </span>
          </button>
          <div class="issue-row-detail" hidden>
            <p class="issue-row-message">${escapeHtml(issue.message)}</p>
            ${issue.snippet ? `<div class="issue-code">${escapeHtml(issue.snippet)}</div>` : ""}
          </div>
        </article>
      `;
    })
    .join("");
}

function setEmptyState(message) {
  elements.issuesList.hidden = true;
  elements.issuesEmpty.hidden = false;
  elements.issuesEmpty.textContent = message;
}

function renderResult(payload) {
  const issues = extractIssues(payload);
  const cleanedCode = extractCleanedCode(payload);
  const mockTest = extractMockTest(payload);
  const summary = extractSummary(payload, issues);

  renderOutputCode(cleanedCode);
  state.lastResultText = cleanedCode;
  state.hasFreshResult = true;
  renderIssues(issues);
  renderMockTest(mockTest);
  setStatus(elements.resultSummary, summary, issues.length ? "warn" : "ok");
  setStatus(elements.inputStatus, "Audit complete", issues.length ? "warn" : "ok");
}

function renderMockTest(mockTest) {
  if (!mockTest) {
    elements.mockResults.hidden = true;
    elements.mockEmpty.hidden = false;
    elements.mockEmpty.textContent = "Mock test not requested for this run.";
    setStatus(elements.mockSummary, "Mock test not requested", "neutral");
    return;
  }

  elements.mockResults.hidden = false;
  elements.mockEmpty.hidden = true;
  const stdout = typeof mockTest.stdout === "string" && mockTest.stdout.trim()
    ? mockTest.stdout
    : "No output was produced during the mock run.";
  elements.mockStdout.innerHTML = `<code>${escapeHtml(stdout)}</code>`;

  const functions = Array.isArray(mockTest.functions_discovered) ? mockTest.functions_discovered : [];
  const autoCalled = Array.isArray(mockTest.functions_auto_called) ? mockTest.functions_auto_called : [];
  const sampleCalls = Array.isArray(mockTest.sample_calls) ? mockTest.sample_calls : [];
  elements.mockFunctions.innerHTML = functions.length
    ? functions.map((name) => `<span class="pill">${escapeHtml(name)}</span>`).join("")
    : '<span class="pill">No user-defined functions found</span>';
  elements.mockCalledFunctions.innerHTML = autoCalled.length
    ? autoCalled.map((name) => `<span class="pill">${escapeHtml(name)}</span>`).join("")
    : '<span class="pill">No zero-argument functions were executed</span>';
  elements.mockSampleCalls.innerHTML = sampleCalls.length
    ? sampleCalls.map((call) => {
      const tone = call.status === "passed" ? "low" : "high";
      const detail = call.status === "passed"
        ? `=> ${escapeHtml(call.result ?? "None")}`
        : escapeHtml(call.error ?? "Failed");
      return `<article class="sample-call-card"><div class="issue-meta"><span class="pill ${tone}">${escapeHtml(call.status)}</span><span class="pill">${escapeHtml(call.name)}(${escapeHtml((call.args ?? []).join(", "))})</span></div><p class="issue-message">${detail}</p></article>`;
    }).join("")
    : '<span class="pill">No sample calls were generated</span>';

  if (mockTest.error && typeof mockTest.error === "object") {
    elements.mockErrorSection.hidden = false;
    const lineText = mockTest.error.line ? `Line ${mockTest.error.line}: ` : "";
    elements.mockError.textContent = `${lineText}${mockTest.error.type}: ${mockTest.error.message}`;
    setStatus(elements.mockSummary, "Mock test failed", "error");
    return;
  }

  elements.mockErrorSection.hidden = true;
  elements.mockError.textContent = "";
  setStatus(elements.mockSummary, "Mock test passed", "ok");
}

function setLoading(isLoading) {
  elements.runAudit.disabled = isLoading;
  elements.styleMode.disabled = isLoading;
  elements.runMock.disabled = isLoading;
  elements.copyOutput.disabled = isLoading;
  elements.loadSample.disabled = isLoading;
  elements.clearInput.disabled = isLoading;
  if (isLoading) {
    setStatus(elements.inputStatus, "Running audit...", "neutral");
  }
}

function readSavedInput() {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    return "";
  }
}

function saveInput(value) {
  try {
    localStorage.setItem(STORAGE_KEY, value);
  } catch {
    // Ignore storage failures in private browsing or locked-down environments.
  }
}

async function copyText(value) {
  if (!value.trim()) return;
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const temp = document.createElement("textarea");
  temp.value = value;
  temp.setAttribute("readonly", "true");
  temp.style.position = "fixed";
  temp.style.left = "-9999px";
  document.body.appendChild(temp);
  temp.select();
  document.execCommand("copy");
  temp.remove();
}

async function runAudit() {
  const code = elements.codeInput.value.trim();
  if (!code) {
    setStatus(elements.inputStatus, "Paste Python code first", "error");
    return;
  }

  if (state.abortController) {
    state.abortController.abort();
  }

  state.abortController = new AbortController();
  setLoading(true);
  setStatus(elements.resultSummary, "Submitting code...", "neutral");

  try {
    const response = await fetch(API_ENDPOINT, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ code, run_mock: elements.runMock.checked, style_mode: elements.styleMode.value }),
      signal: state.abortController.signal,
    });

    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json")
      ? await response.json()
      : await response.text();

    if (!response.ok) {
      const details = typeof payload === "string" ? null : extractErrorDetails(payload);
      const message = details?.message
        ?? (typeof payload === "string"
          ? payload
          : payload?.message ?? `Audit failed with status ${response.status}.`);
      const error = new Error(message);
      error.payload = payload;
      throw error;
    }

    renderResult(payload);
  } catch (error) {
    if (error.name === "AbortError") return;
    setStatus(elements.inputStatus, "Audit failed", "error");
    setStatus(elements.resultSummary, error.message || "The audit request could not be completed.", "error");
    const payload = error.payload;
    const details = payload && typeof payload === "object" ? extractErrorDetails(payload) : null;
    const source = payload && typeof payload === "object" ? unwrapPayload(payload) : null;
    const fallbackCode = typeof source?.cleaned_code === "string" ? source.cleaned_code : "";

    if (fallbackCode.trim()) {
      renderOutputCode(fallbackCode);
      state.lastResultText = fallbackCode;
    } else {
      elements.cleanedOutput.innerHTML = "<pre><code>Unable to load cleaned code. Check the endpoint response and try again.</code></pre>";
      state.lastResultText = "";
    }
    renderMockTest(null);

    if (details) {
      renderIssues([
        {
          title: details.type === "syntax_error" ? "Syntax error" : "Audit error",
          message: details.message,
          severity: "high",
          line: details.line,
          column: details.column,
          rule: details.type,
          snippet: details.text,
        },
      ]);
    } else {
      renderIssues([
        {
          title: "Request error",
          message: error.message || "The request failed before a usable audit result was returned.",
          severity: "high",
          line: "",
          column: "",
          rule: "network",
          snippet: "",
        },
      ]);
    }
  } finally {
    state.abortController = null;
    setLoading(false);
  }
}

function setInput(value) {
  elements.codeInput.value = value;
  saveInput(value);
}

function markResultsStale() {
  if (!state.hasFreshResult) return;
  state.hasFreshResult = false;
  setStatus(elements.resultSummary, "Input changed. Run the audit again.", "warn");
  setStatus(elements.inputStatus, "Results are stale", "warn");
}

elements.codeInput.value = readSavedInput() || SAMPLE_CODE;
setStatus(elements.inputStatus, elements.codeInput.value.trim() ? "Ready to audit" : "Paste Python code first", elements.codeInput.value.trim() ? "neutral" : "error");
renderOutputPlaceholder();

elements.codeInput.addEventListener("input", () => {
  saveInput(elements.codeInput.value);
  if (!elements.codeInput.value.trim()) {
    state.hasFreshResult = false;
    setStatus(elements.inputStatus, "Paste Python code first", "error");
    setStatus(elements.resultSummary, "Waiting for first audit", "neutral");
    return;
  }

  if (state.lastResultText) {
    markResultsStale();
    return;
  }

  setStatus(elements.inputStatus, "Ready to audit", "neutral");
});

elements.runAudit.addEventListener("click", runAudit);
elements.issuesList.addEventListener("click", (event) => {
  const trigger = event.target.closest("[data-issue-trigger]");
  if (!trigger) return;
  const row = trigger.closest("[data-issue-row]");
  if (!row) return;
  const detail = row.querySelector(".issue-row-detail");
  const expanded = trigger.getAttribute("aria-expanded") === "true";
  trigger.setAttribute("aria-expanded", expanded ? "false" : "true");
  if (detail) {
    detail.hidden = expanded;
  }
  row.classList.toggle("expanded", !expanded);
});
elements.loadSample.addEventListener("click", () => {
  setInput(SAMPLE_CODE);
  setStatus(elements.inputStatus, "Sample loaded", "ok");
});
elements.styleMode.addEventListener("change", () => {
  if (state.lastResultText) {
    markResultsStale();
    return;
  }
  setStatus(elements.inputStatus, "Ready to audit", "neutral");
});
elements.clearInput.addEventListener("click", () => {
  setInput("");
  state.lastResultText = "";
  state.hasFreshResult = false;
  elements.styleMode.value = "standard";
  elements.runMock.checked = false;
  renderOutputPlaceholder();
  setEmptyState("No audit has been run yet. Results will appear here after you submit code.");
  renderMockTest(null);
  setStatus(elements.resultSummary, "Waiting for first audit", "neutral");
  setStatus(elements.inputStatus, "Paste Python code first", "error");
});
elements.copyOutput.addEventListener("click", async () => {
  if (!state.hasFreshResult || !state.lastResultText.trim()) {
    setStatus(elements.resultSummary, "Nothing to copy yet", "warn");
    return;
  }
  const output = state.lastResultText;

  try {
    await copyText(output);
    setStatus(elements.resultSummary, "Cleaned code copied", "ok");
  } catch {
    setStatus(elements.resultSummary, "Copy failed", "error");
  }
});

document.addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    runAudit();
  }
});

setEmptyState("No audit has been run yet. Results will appear here after you submit code.");
renderMockTest(null);
