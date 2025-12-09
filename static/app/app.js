const DEFAULT_SERVER_URL = "/api/alerts/latest";

function $(id){ return document.getElementById(id); }

let lastTs = 0;
let lastSoundTs = 0;


function playTone(freq, durationMs) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.frequency.value = freq;
    osc.type = "sine";
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    gain.gain.setValueAtTime(0.3, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + durationMs / 1000);
    osc.stop(ctx.currentTime + durationMs / 1000 + 0.05);
  } catch (e) {
    // AudioContext might be blocked until user interaction
  }
}

function playAlertSound(action, mode) {
  const up = (action || "").toUpperCase();
  if (up.includes("LOCKDOWN")) {
    // repeating high-low chirp
    playTone(880, 200);
    setTimeout(() => playTone(660, 200), 220);
    setTimeout(() => playTone(880, 200), 440);
  } else if (up.includes("EVAC")) {
    playTone(600, 500);
  } else if (up.includes("SECURE")) {
    playTone(500, 400);
  } else if (up.includes("HOLD")) {
    playTone(440, 300);
  } else if (up.includes("SHELTER")) {
    playTone(350, 400);
  } else if (mode === "LIVE") {
    playTone(750, 400);
  } else {
    playTone(440, 200);
  }
}


async function fetchSettings() {
  // Station ID is stored in localStorage only (per device)
  return {
    serverUrl: DEFAULT_SERVER_URL,
    stationId: localStorage.getItem("gems_station") || ""
  };
}

function saveStationId(id) {
  localStorage.setItem("gems_station", id || "");
}

function setBadge(mode) {
  const badge = $("modeBadge");
  badge.className = "badge " + mode;
  badge.textContent = mode;
}

function formatTime(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}

async function loadAlert() {
  const statusEl = $("status");
  const card = $("alertCard");
  const ackBtn = $("ackBtn");
  statusEl.textContent = "Updating…";

  try {
    const res = await fetch(DEFAULT_SERVER_URL, { cache: "no-cache" });
    if (!res.ok) {
      statusEl.textContent = "Error " + res.status;
      return;
    }
    const data = await res.json();
    const mode = (data.mode || "IDLE").toUpperCase();
    const text = data.text || "";
    const action = data.action || "";
    const ts = data.timestamp || 0;

    setBadge(mode);
    if (mode === "IDLE") {
      $("alertAction").textContent = "No Active Alert";
      $("alertText").textContent = "—";
      $("alertTime").textContent = "—";
      ackBtn.disabled = true;
    } else {
      $("alertAction").textContent = action || text || "ALERT";
      $("alertText").textContent = text || (mode + " " + action);
      $("alertTime").textContent = formatTime(ts);
      ackBtn.disabled = false;
    }

    lastTs = ts;
    statusEl.textContent = "Connected";
  } catch (e) {
    statusEl.textContent = "Offline";
  }
}

async function sendAck() {
  const ackBtn = $("ackBtn");
  const ackStatus = $("ackStatus");
  const station = $("stationId").value.trim();
  if (!station) {
    ackStatus.textContent = "Enter your station / classroom ID first.";
    return;
  }
  saveStationId(station);

  ackBtn.disabled = true;
  ackStatus.textContent = "Sending acknowledgment…";
  try {
    const res = await fetch("/api/acknowledge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ station })
    });
    if (!res.ok) {
      ackStatus.textContent = "Server error " + res.status;
    } else {
      ackStatus.textContent = "Acknowledged.";
    }
  } catch (e) {
    ackStatus.textContent = "Failed to send acknowledgment.";
  } finally {
    ackBtn.disabled = false;
  }
}

async function sendTrigger(action, mode) {
  const code = $("adminCode").value.trim();
  if (!code) {
    alert("Admin passcode required to trigger alerts.");
    return;
  }
  const form = new FormData();
  form.set("action", action);
  form.set("mode", mode);
  form.set("admin_passcode", code);

  try {
    const res = await fetch("/trigger", {
      method: "POST",
      body: form
    });
    // We don't need response body; server will redirect if it were a page.
  } catch (e) {
    console.error("Trigger failed", e);
  }
}

function setup() {
  // restore station
  $("stationId").value = localStorage.getItem("gems_station") || "";
  $("ackBtn").addEventListener("click", sendAck);

  document.querySelectorAll("button.subtle[data-action]").forEach(btn => {
    btn.addEventListener("click", () => {
      sendTrigger(btn.dataset.action, btn.dataset.mode);
    });
  });

  loadAlert();
  setInterval(loadAlert, 5000);

  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("sw.js").catch(() => {});
  }
}

document.addEventListener("DOMContentLoaded", setup);
