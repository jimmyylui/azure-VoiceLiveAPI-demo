const clientId = `web-${crypto.randomUUID()}`;
const elements = {
  agentName: document.querySelector("#agent-name"),
  statusDot: document.querySelector("#status-dot"),
  statusLabel: document.querySelector("#status-label"),
  video: document.querySelector("#video-container"),
  caption: document.querySelector("#caption"),
  session: document.querySelector("#session-button"),
  mic: document.querySelector("#mic-button"),
  captions: document.querySelector("#captions-button"),
  input: document.querySelector("#text-input"),
  send: document.querySelector("#send-button"),
  error: document.querySelector("#error"),
};

let socket;
let peerConnection;
let mediaStream;
let captureContext;
let captureNode;
let connected = false;
let microphoneOn = false;
let captionsOn = true;

document.addEventListener("DOMContentLoaded", async () => {
  lucide.createIcons();
  bindControls();
  try {
    const response = await fetch("/api/config");
    const config = await response.json();
    elements.agentName.textContent = "Ricoh Avatars";
    if (!config.ready) {
      showError(`Server configuration needed: ${config.missing.join(", ")}`);
      elements.session.disabled = true;
    }
  } catch {
    showError("Could not load the server configuration.");
  }
});

function bindControls() {
  elements.session.addEventListener("click", () => connected ? disconnect() : connect());
  elements.mic.addEventListener("click", toggleMicrophone);
  elements.captions.addEventListener("click", () => {
    captionsOn = !captionsOn;
    elements.captions.classList.toggle("active", captionsOn);
    elements.caption.classList.toggle("visible", captionsOn && Boolean(elements.caption.textContent));
  });
  elements.send.addEventListener("click", sendText);
  elements.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") sendText();
  });
}

function connect() {
  showError("");
  setStatus("Connecting", false);
  elements.session.disabled = true;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  const currentSocket = new WebSocket(`${protocol}//${location.host}/ws/${clientId}`);
  socket = currentSocket;
  currentSocket.addEventListener("open", () => {
    if (socket === currentSocket) currentSocket.send(JSON.stringify({ type: "start_session" }));
  });
  currentSocket.addEventListener("message", (event) => {
    if (socket !== currentSocket) return;
    try {
      handleMessage(JSON.parse(event.data));
    } catch {
      showError("The server returned an invalid message.");
    }
  });
  currentSocket.addEventListener("error", () => {
    if (socket === currentSocket) showError("The server connection failed.");
  });
  currentSocket.addEventListener("close", () => {
    if (socket !== currentSocket) return;
    socket = undefined;
    resetSession();
  });
}

function handleMessage(message) {
  switch (message.type) {
    case "session_started":
      connected = true;
      elements.session.disabled = false;
      elements.session.classList.add("connected");
      elements.session.querySelector("span").textContent = "End conversation";
      elements.mic.disabled = false;
      elements.input.disabled = false;
      elements.send.disabled = false;
      setStatus("Listening", true);
      break;
    case "ice_servers":
      setupWebRtc(message.iceServers);
      break;
    case "avatar_sdp_answer":
      applySdpAnswer(message.serverSdp);
      break;
    case "status":
      setStatus(statusText(message.state), true);
      break;
    case "transcript":
      showCaption(message.role === "user" ? `You: ${message.text}` : message.text);
      break;
    case "error":
      showError(message.message || "Voice Live returned an error.");
      setStatus("Error", false);
      elements.session.disabled = false;
      break;
    case "session_stopped":
      socket?.close();
      break;
  }
}

function statusText(state) {
  return { listening: microphoneOn ? "Listening" : "Ready", thinking: "Thinking", speaking: "Speaking" }[state] || state;
}

async function setupWebRtc(iceServers) {
  peerConnection?.close();
  peerConnection = new RTCPeerConnection({ iceServers });
  peerConnection.addTransceiver("video", { direction: "recvonly" });
  peerConnection.addTransceiver("audio", { direction: "recvonly" });
  peerConnection.createDataChannel("eventChannel");
  peerConnection.ontrack = ({ track, streams }) => {
    let player = elements.video.querySelector(track.kind);
    if (!player) {
      player = document.createElement(track.kind);
      player.autoplay = true;
      player.playsInline = true;
      elements.video.append(player);
    }
    player.srcObject = streams[0];
    player.play().catch(() => {});
  };

  const offer = await peerConnection.createOffer();
  await peerConnection.setLocalDescription(offer);
  await waitForIce(peerConnection);
  const encoded = btoa(JSON.stringify(peerConnection.localDescription));
  socket.send(JSON.stringify({ type: "avatar_sdp_offer", clientSdp: encoded }));
}

function waitForIce(connection) {
  if (connection.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const timeout = setTimeout(resolve, 8000);
    connection.addEventListener("icegatheringstatechange", () => {
      if (connection.iceGatheringState === "complete") {
        clearTimeout(timeout);
        resolve();
      }
    });
  });
}

async function applySdpAnswer(encodedSdp) {
  if (!peerConnection) return;
  const answer = JSON.parse(atob(encodedSdp));
  await peerConnection.setRemoteDescription(answer);
}

async function toggleMicrophone() {
  if (!microphoneOn) {
    try {
      await startCapture();
      microphoneOn = true;
      setMicIcon("mic", "Turn microphone off");
      setStatus("Listening", true);
    } catch {
      showError("Microphone permission was denied or no microphone is available.");
    }
  } else {
    stopCapture();
    microphoneOn = false;
    setMicIcon("mic-off", "Turn microphone on");
    setStatus("Ready", true);
  }
}

async function startCapture() {
  mediaStream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
  });
  captureContext = new AudioContext();
  await captureContext.audioWorklet.addModule("/pcm-capture-worklet.js");
  const source = captureContext.createMediaStreamSource(mediaStream);
  captureNode = new AudioWorkletNode(captureContext, "pcm-capture");
  const silentGain = captureContext.createGain();
  silentGain.gain.value = 0;
  source.connect(captureNode).connect(silentGain).connect(captureContext.destination);
  captureNode.port.onmessage = ({ data }) => {
    if (socket?.readyState === WebSocket.OPEN && microphoneOn) {
      socket.send(JSON.stringify({ type: "audio_chunk", data: bufferToBase64(data) }));
    }
  };
}

function stopCapture() {
  captureNode?.disconnect();
  captureNode = undefined;
  captureContext?.close();
  captureContext = undefined;
  mediaStream?.getTracks().forEach((track) => track.stop());
  mediaStream = undefined;
}

function bufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function sendText() {
  const text = elements.input.value.trim();
  if (!text || socket?.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify({ type: "send_text", text }));
  showCaption(`You: ${text}`);
  elements.input.value = "";
}

function disconnect() {
  const currentSocket = socket;
  socket = undefined;
  currentSocket?.send(JSON.stringify({ type: "stop_session" }));
  currentSocket?.close();
  resetSession();
}

function resetSession() {
  connected = false;
  microphoneOn = false;
  stopCapture();
  peerConnection?.close();
  peerConnection = undefined;
  elements.video.replaceChildren();
  elements.session.disabled = false;
  elements.session.classList.remove("connected");
  elements.session.querySelector("span").textContent = "Start conversation";
  elements.mic.disabled = true;
  elements.input.disabled = true;
  elements.send.disabled = true;
  setMicIcon("mic-off", "Turn microphone on");
  setStatus("Offline", false);
}

function setMicIcon(icon, label) {
  elements.mic.innerHTML = `<i data-lucide="${icon}"></i>`;
  elements.mic.setAttribute("aria-label", label);
  elements.mic.title = label;
  elements.mic.classList.toggle("active", microphoneOn);
  lucide.createIcons({ attrs: { "stroke-width": 2 } });
}

function setStatus(label, online) {
  elements.statusLabel.textContent = label;
  elements.statusDot.classList.toggle("online", online);
}

function showCaption(text) {
  elements.caption.textContent = text;
  elements.caption.classList.toggle("visible", captionsOn && Boolean(text));
}

function showError(message) {
  elements.error.textContent = message;
}