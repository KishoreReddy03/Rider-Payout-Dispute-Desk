// Ops Dashboard Controller Application

document.addEventListener("DOMContentLoaded", () => {
  loadPendingOps();
});

// Load pending items from GET /ops/pending
async function loadPendingOps() {
  const tableBody = document.getElementById("pending-table-body");
  const emptyState = document.getElementById("empty-state");

  tableBody.innerHTML = `
    <tr>
      <td colspan="6" style="text-align: center; padding: 2rem; color: var(--text-muted);">
        Fetching pending queue...
      </td>
    </tr>
  `;
  emptyState.style.display = "none";

  try {
    const res = await fetch("/ops/pending");
    if (!res.ok) {
      throw new Error(`Failed to load pending queue (HTTP ${res.status})`);
    }
    const data = await res.json();
    renderPendingTable(data);
  } catch (err) {
    showToast(`Error: ${err.message}`, "error");
    tableBody.innerHTML = `
      <tr>
        <td colspan="6" style="text-align: center; padding: 2rem; color: var(--color-danger);">
          Failed to load queue. Please check server connection.
        </td>
      </tr>
    `;
  }
}

// Render Table Rows and Update Metric Counters
function renderPendingTable(items) {
  const tableBody = document.getElementById("pending-table-body");
  const emptyState = document.getElementById("empty-state");

  // Counters
  let totalCount = items.length;
  let approvalCount = 0;
  let escalationCount = 0;
  let payoutTotal = 0;

  items.forEach(item => {
    if (item.type === "approval") {
      approvalCount++;
      payoutTotal += (item.amount || 0);
    } else if (item.type === "escalation") {
      escalationCount++;
    }
  });

  // Update Metrics DOM
  document.getElementById("metric-total-count").innerText = totalCount;
  document.getElementById("badge-total-count").innerText = totalCount;
  document.getElementById("metric-approval-count").innerText = approvalCount;
  document.getElementById("metric-escalation-count").innerText = escalationCount;
  document.getElementById("metric-payout-total").innerText = `₹${payoutTotal}`;

  if (totalCount === 0) {
    tableBody.innerHTML = "";
    emptyState.style.display = "block";
    return;
  }

  emptyState.style.display = "none";

  tableBody.innerHTML = items.map(item => {
    const isApproval = item.type === "approval";
    const typeBadge = isApproval
      ? `<span class="badge badge-approval">Approval Pending</span>`
      : `<span class="badge badge-escalation">Escalation</span>`;

    const amountDisplay = isApproval
      ? `<span class="amount-text">₹${item.amount}</span>`
      : `<span style="color: var(--text-muted); font-size: 0.85rem;">N/A</span>`;

    const actions = isApproval
      ? `
        <button class="btn btn-success" id="btn-approve-${item.id}" onclick="approveDecision(${item.id})">
          Approve Payout
        </button>
        <button class="btn btn-danger" id="btn-reject-${item.id}" onclick="rejectDecision(${item.id})">
          Reject
        </button>
        <button class="btn btn-secondary" onclick="viewTrace('${item.rider_id}')">
          Trace
        </button>
      `
      : `
        <button class="btn btn-primary" id="btn-escalate-${item.id}" onclick="escalateDecision(${item.id})">
          Confirm Escalation
        </button>
        <button class="btn btn-danger" id="btn-reject-${item.id}" onclick="rejectDecision(${item.id})">
          Reject
        </button>
        <button class="btn btn-secondary" onclick="viewTrace('${item.rider_id}')">
          Trace
        </button>
      `;

    return `
      <tr>
        <td><strong>#${item.id}</strong></td>
        <td><span class="rider-badge">${item.rider_id}</span></td>
        <td>${typeBadge}</td>
        <td>${amountDisplay}</td>
        <td><div class="reason-text">${escapeHtml(item.reason || 'No description available')}</div></td>
        <td><div class="btn-group">${actions}</div></td>
      </tr>
    `;
  }).join("");
}

// Approve Decision (Calls backend POST /ops/decisions/{id}/approve)
async function approveDecision(id) {
  const btn = document.getElementById(`btn-approve-${id}`);
  if (btn) {
    btn.disabled = true;
    btn.innerText = "Processing...";
  }

  try {
    const res = await fetch(`/ops/decisions/${id}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });

    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Approval failed");
    }

    showToast(`Approved! Payout ${data.payout_id || ''} executed for ${data.rider_id} (₹${data.amount})`, "success");
    loadPendingOps();
  } catch (err) {
    showToast(`Approval Error: ${err.message}`, "error");
    if (btn) {
      btn.disabled = false;
      btn.innerText = "Approve Payout";
    }
  }
}

// Reject Decision (Calls backend POST /ops/decisions/{id}/reject)
async function rejectDecision(id) {
  const reason = prompt("Enter rejection reason (optional):") || "Rejected by Ops";
  const btn = document.getElementById(`btn-reject-${id}`);
  if (btn) {
    btn.disabled = true;
    btn.innerText = "Rejecting...";
  }

  try {
    const res = await fetch(`/ops/decisions/${id}/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    });

    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Rejection failed");
    }

    showToast(`Decision #${id} rejected. Zero money moved.`, "success");
    loadPendingOps();
  } catch (err) {
    showToast(`Rejection Error: ${err.message}`, "error");
    if (btn) {
      btn.disabled = false;
      btn.innerText = "Reject";
    }
  }
}

// Escalate Decision (Calls backend POST /ops/decisions/{id}/escalate)
async function escalateDecision(id) {
  const reason = prompt("Enter escalation note (optional):") || "Escalated to Tier-2 Support";
  const btn = document.getElementById(`btn-escalate-${id}`);
  if (btn) {
    btn.disabled = true;
    btn.innerText = "Escalating...";
  }

  try {
    const res = await fetch(`/ops/decisions/${id}/escalate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    });

    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Escalation failed");
    }

    showToast(`Decision #${id} escalated to tier-2 support.`, "success");
    loadPendingOps();
  } catch (err) {
    showToast(`Escalation Error: ${err.message}`, "error");
    if (btn) {
      btn.disabled = false;
      btn.innerText = "Confirm Escalation";
    }
  }
}

// View Trace Modal (Calls backend GET /trace/{rider_id})
async function viewTrace(riderId) {
  document.getElementById("trace-rider-id").innerText = riderId;
  const timeline = document.getElementById("trace-timeline");
  timeline.innerHTML = `<div style="color: var(--text-muted); padding: 1rem;">Loading trace audit events...</div>`;

  document.getElementById("trace-modal").classList.add("active");

  try {
    const res = await fetch(`/trace/${riderId}`);
    if (!res.ok) {
      throw new Error(`Failed to fetch trace for ${riderId}`);
    }
    const events = await res.json();
    renderTraceTimeline(events);
  } catch (err) {
    timeline.innerHTML = `<div style="color: var(--color-danger); padding: 1rem;">Error: ${err.message}</div>`;
  }
}

function closeTraceModal() {
  document.getElementById("trace-modal").classList.remove("active");
}

function renderTraceTimeline(events) {
  const timeline = document.getElementById("trace-timeline");
  if (!events || events.length === 0) {
    timeline.innerHTML = `<div style="color: var(--text-muted); padding: 1rem;">No trace events recorded for this rider.</div>`;
    return;
  }

  timeline.innerHTML = events.map(evt => {
    const eventType = evt.type || "unknown";
    const eventName = evt.name || eventType;
    const timeStr = evt.at ? new Date(evt.at).toLocaleTimeString() : "";

    const detailObj = {
      input: evt.input,
      output: evt.output
    };

    return `
      <div class="timeline-item">
        <div class="timeline-dot event-${eventType}"></div>
        <div class="timeline-content">
          <div class="timeline-header">
            <span class="timeline-name">[${eventType.toUpperCase()}] ${escapeHtml(eventName)}</span>
            <span class="timeline-time">${timeStr}</span>
          </div>
          <pre class="timeline-json">${escapeHtml(JSON.stringify(detailObj, null, 2))}</pre>
        </div>
      </div>
    `;
  }).join("");
}

// Toast Notifications
function showToast(message, type = "info") {
  const container = document.getElementById("toast-container");
  const toast = document.createElement("div");
  toast.className = `toast toast-${type}`;
  toast.innerHTML = `<span>${escapeHtml(message)}</span>`;

  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateX(100%)";
    toast.style.transition = "all 0.3s ease";
    setTimeout(() => toast.remove(), 300);
  }, 4000);
}

function escapeHtml(str) {
  if (typeof str !== "string") return str;
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
